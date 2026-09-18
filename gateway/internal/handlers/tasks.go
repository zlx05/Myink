// 网关任务路由：建章 / 建批次 → 三层闸门入队；任务详情 → 转发 Python API（§17.2）。
package handlers

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"aiink/gateway/internal/config"
	"aiink/gateway/internal/pyapi"
	"aiink/gateway/internal/queue"
	"aiink/gateway/internal/redis"
	streaming "aiink/gateway/internal/sse"
)

type TaskHandler struct {
	cfg config.Config
	r   *redis.Client
	rmq *queue.AMQP
	py  *pyapi.Client
}

func NewTaskHandler(cfg config.Config, r *redis.Client, rmq *queue.AMQP, py *pyapi.Client) *TaskHandler {
	return &TaskHandler{cfg: cfg, r: r, rmq: rmq, py: py}
}

// 建单章生成任务（三层闸门扣 1）。
// POST /api/v1/projects/:project_id/chapters/:chapter_id/generate
// body: {"seq": N, "user_instruction": "...", "rewrite": false}
// rewrite=true 显式重写已确认章（§7.3 失效重建，worker _guard_write_order 放行）。
func (h *TaskHandler) CreateChapter(c *gin.Context) {
	projectID := c.Param("project_id")
	chapterID := c.Param("chapter_id")
	var req struct {
		Seq             int    `json:"seq"`
		UserInstruction string `json:"user_instruction"`
		Rewrite         bool   `json:"rewrite"`
		Mode            string `json:"mode"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_body"})
		return
	}
	payload := map[string]any{
		"chapter_id":       chapterID,
		"seq":              req.Seq,
		"user_instruction": req.UserInstruction,
	}
	if req.Mode == "" {
		req.Mode = "auto"
	}
	if req.Mode != "auto" && req.Mode != "manual" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_writing_mode"})
		return
	}
	payload["mode"] = req.Mode
	if req.Rewrite {
		payload["rewrite"] = true
	}
	h.enqueue(c, projectID, "chapter_generate", payload, 1, h.cfg.CostPerChapter)
}

// 建批次生成任务（三层闸门扣 size）。
// POST /api/v1/projects/:project_id/batches/generate
// body: {"size": N, "start": M}
func (h *TaskHandler) CreateBatch(c *gin.Context) {
	projectID := c.Param("project_id")
	var req struct {
		Size  int `json:"size"`
		Start int `json:"start"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_body"})
		return
	}
	// 批次上限（§6.11 成本熔断第一道闸）：前端可调 ≤ 默认，超硬上限 clamp（不拒绝，
	// 与 Python 侧 batch_max_hard 对齐；worker 消费时同 clamp 双保险）
	if req.Size < 1 {
		req.Size = 1
	}
	if req.Size > h.cfg.BatchMaxHard {
		req.Size = h.cfg.BatchMaxHard
	}
	// start 缺省/非法 → 1（复查 B1）：Go 零值 0 会穿透到 worker，让批次从第 0 章写起；
	// 权威写序校验在 worker（_guard_write_order，首章必须 = max_seq+1），网关只做语法层。
	if req.Start < 1 {
		req.Start = 1
	}
	payload := map[string]any{"size": req.Size, "start": req.Start}
	h.enqueue(c, projectID, "batch_generate", payload, req.Size, h.cfg.CostPerChapter*float64(req.Size))
}

// enqueue 三层闸门 → 入队 → 202 + task_id；闸门拒绝转对应状态码。
func (h *TaskHandler) enqueue(c *gin.Context, projectID, taskType string, payload map[string]any, quotaN int, costEst float64) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()

	// VIP 优先：JWT tier claim → RabbitMQ priority（vip→9 / 其他→0）
	priority := h.cfg.NormalPriority
	if GetUserTier(c) == "vip" {
		priority = h.cfg.VIPPriority
	}
	res, err := queue.Enqueue(ctx, h.r, h.rmq, h.cfg, GetUserID(c), projectID, taskType, payload, quotaN, costEst, priority)
	if err != nil {
		var ge *queue.GateError
		if errors.As(err, &ge) {
			// 三层闸门拒绝：配额/并发/成本超限
			c.JSON(http.StatusTooManyRequests, gin.H{"error": ge.Code})
			return
		}
		log.Printf("[gateway] enqueue_failed project=%s type=%s: %v", projectID, taskType, err)
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "enqueue_failed"})
		return
	}
	// 在返回 task_id 前建立 SSE 流，消除“浏览器已订阅、Worker 尚未取到 RabbitMQ
	// 消息”窗口。否则 Subscribe 会把尚不存在的流误判为历史流过期，前端只能等刷新。
	streamKey := streaming.StreamKey(res.TaskID)
	if _, streamErr := h.r.XAdd(ctx, streamKey, map[string]any{
		"event": "status", "task_id": res.TaskID, "status": "queued",
	}); streamErr == nil {
		_ = h.r.Expire(ctx, streamKey, streaming.TTL)
	}
	c.JSON(http.StatusAccepted, gin.H{
		"task_id":  res.TaskID,
		"trace_id": res.TraceID,
		"status":   "queued",
	})
}

// 任务详情：转发 Python API（任务状态 + 批次进度 i/N）。
// GET /api/v1/tasks/:task_id
func (h *TaskHandler) GetTask(c *gin.Context) {
	taskID := c.Param("task_id")
	body, status, err := h.py.GetTaskDetail(c.Request.Context(), taskID)
	if err != nil {
		// 上游明确"任务不存在"（入队→DB 物化的异步窗口内正常）→ 透传 404，
		// 前端轮询视为"在途"继续等，而非报错；其余 5xx/连接失败才按不可达 502。
		if status == http.StatusNotFound {
			c.JSON(http.StatusNotFound, gin.H{"error": "task_not_found"})
			return
		}
		c.JSON(http.StatusBadGateway, gin.H{"error": "python_api_unreachable"})
		return
	}
	c.Data(http.StatusOK, "application/json; charset=utf-8", body)
}

// 确认手动模式章节计划：转发 Python API，由后者校验版本并发布断点恢复消息。
// POST /api/v1/tasks/:task_id/plan/confirm
func (h *TaskHandler) ConfirmTaskPlan(c *gin.Context) {
	taskID := c.Param("task_id")
	body, err := io.ReadAll(c.Request.Body)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid_body"})
		return
	}
	h.forwardToPy(c, "/internal/v1/tasks/"+taskID+"/plan/confirm", body)
}

// 单章任务控制。目前手动 Plan 等待页只开放取消；转发到 Python 的统一任务端点。
func (h *TaskHandler) TaskControl(c *gin.Context) {
	taskID := c.Param("task_id")
	h.forwardToPy(c, "/internal/v1/tasks/"+taskID+"/cancel", nil)
}

// 批次控制：pause / resume / cancel，转发 Python API。
// POST /api/v1/batches/:batch_id/:action  (action: pause|resume|cancel)
// 转发到 /internal/v1/tasks/{id}/{action}（Python API 任务控制端点；批次 id = batch 任务 id）。
func (h *TaskHandler) BatchControl(c *gin.Context) {
	batchID := c.Param("batch_id")
	action := c.Param("action")
	h.forwardToPy(c, "/internal/v1/tasks/"+batchID+"/"+action, nil)
}

// 项目列表：转发 Python API（多本书展示前端侧边栏，唯一入口仍走网关）。
// GET /api/v1/projects
func (h *TaskHandler) ListProjects(c *gin.Context) {
	h.forwardToPy(c, "/internal/v1/projects", nil)
}

// 项目任务历史（阶段 4 任务视图）：转发 Python API（切书后展示该书过往任务，
// 点开任一条再由前端走 GET /tasks/:task_id 拉节点流转）。
// GET /api/v1/projects/:project_id/tasks
func (h *TaskHandler) ListProjectTasks(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/tasks", nil)
}

// 记忆候选：待确认池（§6.11 确认分流）。GET /api/v1/projects/:project_id/candidates
func (h *TaskHandler) ListCandidates(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/candidates", nil)
}

// 候选确认/拒绝（§7.3 事实生命周期，编排层写库）。
// POST /api/v1/projects/:project_id/candidates/:candidate_id/:action  (action: confirm|reject)
func (h *TaskHandler) CandidateAction(c *gin.Context) {
	pid := c.Param("project_id")
	cid := c.Param("candidate_id")
	action := c.Param("action")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/candidates/"+cid+"/"+action, nil)
}

// 写作经验列表（§8.9 reflexion：经验池展示）。
// GET /api/v1/projects/:project_id/lessons
func (h *TaskHandler) ListLessons(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/lessons", nil)
}

// 经验确认/拒绝（§8.9 reflexion 确认分流，编排层写库）。
// POST /api/v1/projects/:project_id/lessons/:lesson_id/:action  (action: confirm|reject)
func (h *TaskHandler) LessonAction(c *gin.Context) {
	pid := c.Param("project_id")
	lid := c.Param("lesson_id")
	action := c.Param("action")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/lessons/"+lid+"/"+action, nil)
}

// 章节列表：转发 Python API（RLS 由 Python 侧 tenant_session 过滤）。
// GET /api/v1/projects/:project_id/chapters
func (h *TaskHandler) ListChapters(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/chapters", nil)
}

// 章节详情（含正文/summary）：转发 Python API（get_chapter，main.py:117）。
// 阶段 4 前端章节编辑器读取正文；Python 侧 require_owner 归属断言。
// GET /api/v1/projects/:project_id/chapters/:chapter_id
func (h *TaskHandler) GetChapter(c *gin.Context) {
	pid := c.Param("project_id")
	cid := c.Param("chapter_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/chapters/"+cid, nil)
}

// 编辑章节正文（阶段 3 轻编辑：只更新正文不触记忆，零 LLM）。
// PUT /api/v1/projects/:project_id/chapters/:chapter_id/content
func (h *TaskHandler) UpdateChapterContent(c *gin.Context) {
	pid := c.Param("project_id")
	cid := c.Param("chapter_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/chapters/"+cid+"/content", body)
}

// 显式校正记忆（阶段 3：编辑后正文重新抽取 → 与该章已落库记忆 diff → 变更集进待确认池）。
// 同步 LLM 调用（一次 extract），网关超时 30s；超时属正常，前端可提示重试。
// POST /api/v1/projects/:project_id/chapters/:chapter_id/correct-memory
func (h *TaskHandler) CorrectMemory(c *gin.Context) {
	pid := c.Param("project_id")
	cid := c.Param("chapter_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/chapters/"+cid+"/correct-memory", body)
}

// 级联删除章节（阶段 3：删除该章及其后全部章节正文 + 记忆 + 池候选，进度回退）。
// DELETE /api/v1/projects/:project_id/chapters/:chapter_id
func (h *TaskHandler) DeleteChapter(c *gin.Context) {
	pid := c.Param("project_id")
	cid := c.Param("chapter_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/chapters/"+cid, nil)
}

// 章节历史版本列表（阶段 4 版本表：降序含正文，供前端预览/比对/回退）。
// GET /api/v1/projects/:project_id/chapters/:chapter_id/versions
func (h *TaskHandler) ListChapterVersions(c *gin.Context) {
	pid := c.Param("project_id")
	cid := c.Param("chapter_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/chapters/"+cid+"/versions", nil)
}

// 回退到历史版本（阶段 4：快照当前 → 覆盖回目标版本 → 版本 +1，转发 Python API）。
// POST /api/v1/projects/:project_id/chapters/:chapter_id/versions/:version/restore
func (h *TaskHandler) RestoreChapterVersion(c *gin.Context) {
	pid := c.Param("project_id")
	cid := c.Param("chapter_id")
	version := c.Param("version")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/chapters/"+cid+"/versions/"+version+"/restore", body)
}

// 全局审计（阶段 3 长线治理：手动触发抽样人设漂移 L2，转发 Python API）。
// 同步 LLM 调用（一次判定），网关超时 30s；超时属正常，前端可提示重试（同 CorrectMemory）。
// POST /api/v1/projects/:project_id/global-audit
func (h *TaskHandler) GlobalAudit(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/global-audit", nil)
}

// 创作设置读取：文风档案 / 题材 Skill / 模型连接 / 路由表 / 版本号，转发 Python API。
// GET /api/v1/projects/:project_id/settings
func (h *TaskHandler) ListSettings(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/settings", nil)
}

// 更新模型连接与路由（自定义 API Key 由 Python 层加密，网关只透传请求体）。
// PUT /api/v1/projects/:project_id/settings  body: {"model_routes": {...}, "model_connections": [...]}
func (h *TaskHandler) UpdateSettings(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/settings", body)
}

// 拉取连接可用模型列表（探针：GET /models；body 含 protocol/base_url/api_key|connection_id，转发 Python API）。
// POST /api/v1/projects/:project_id/settings/models
func (h *TaskHandler) ListConnectionModels(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/settings/models", body)
}

// 连接联通测试（探针：发一条 max_tokens=16 的 ping，转发 Python API）。
// POST /api/v1/projects/:project_id/settings/test-connection
func (h *TaskHandler) TestModelConnection(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/settings/test-connection", body)
}

// 账号级环境配置读取（模型连接 / 路由 / MCP 扫榜），转发 Python API。
// GET /api/v1/environment
func (h *TaskHandler) GetEnvironment(c *gin.Context) {
	h.forwardToPy(c, "/internal/v1/environment", nil)
}

// 更新账号级模型连接、路由与扫榜覆盖（自定义 API Key 由 Python 层加密）。
// PUT /api/v1/environment
func (h *TaskHandler) UpdateEnvironment(c *gin.Context) {
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/environment", body)
}

// 拉取连接可用模型列表（账号级探针）。
// POST /api/v1/environment/models
func (h *TaskHandler) ListEnvironmentModels(c *gin.Context) {
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/environment/models", body)
}

// 模型连接联通测试（账号级探针）。
// POST /api/v1/environment/test-connection
func (h *TaskHandler) TestEnvironmentConnection(c *gin.Context) {
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/environment/test-connection", body)
}

// MCP 扫榜联通测试（list_tools）。
// POST /api/v1/environment/test-rankings
func (h *TaskHandler) TestRankingsConnection(c *gin.Context) {
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/environment/test-rankings", body)
}

// 题材 Skill 预设列表（§7.12 预设包 = 4 本种子书文风档案），转发 Python API。
// GET /api/v1/skill-presets
func (h *TaskHandler) SkillPresets(c *gin.Context) {
	h.forwardToPy(c, "/internal/v1/skill-presets", nil)
}

// GET /api/v1/genre-packs
func (h *TaskHandler) GenrePacks(c *gin.Context) {
	h.forwardToPy(c, "/internal/v1/genre-packs", nil)
}

// PUT /api/v1/projects/:project_id/genre-pack
func (h *TaskHandler) PutGenrePack(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/genre-pack", body)
}

// POST /api/v1/projects/:project_id/genre-pack/restore
func (h *TaskHandler) RestoreGenrePack(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/genre-pack/restore", nil)
}

// 文风样本提取（§7.12 闭环：作者样本 → 统计层 + LLM 提炼 → 草稿，转发 Python API）。
// POST /api/v1/projects/:project_id/style-samples  body: {"samples": ["..."]}
func (h *TaskHandler) StyleSamples(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/style-samples", body)
}

// 文风档案确认落库（§7.12；预设导入可带 skill_pack 原子写 profile+marker，转发 Python API）。
// PUT /api/v1/projects/:project_id/style-profile  body: {"profile": {...}, "skill_pack": "..."}
func (h *TaskHandler) PutStyleProfile(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/style-profile", body)
}

// 建书（§7.11 建书向导）：创建 Project + 空 ProjectSettings（不调 LLM，转发 Python API）。
// POST /api/v1/projects  body: {"title": "...", "genre": "...", "target_words": 3000}
func (h *TaskHandler) CreateProject(c *gin.Context) {
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects", body)
}

// 作品基本信息更新（§6.9 每章目标字数可配，转发 Python API）。
// PUT /api/v1/projects/:project_id  body: {"target_words": 3500}
func (h *TaskHandler) UpdateProject(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid, body)
}

// 整本书删除（硬删：守卫进行中任务 → 级联清业务表/向量/checkpoint/Redis 残留，转发 Python API）。
// DELETE /api/v1/projects/:project_id
func (h *TaskHandler) DeleteProject(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid, nil)
}

// 设定骨架草稿（§7.11 ②：一句话梗概 → Planner 提案，可反复重新生成，转发 Python API）。
// POST /api/v1/projects/:project_id/setup-draft  body: {"premise": "..."}
func (h *TaskHandler) SetupDraft(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPyLong(c, "/internal/v1/projects/"+pid+"/setup-draft", body)
}

// 设定确认落库（§7.11 ③ append-only：用户确认 = 编排层写库入口，转发 Python API）。
// PUT /api/v1/projects/:project_id/setup  body: {"world_rules":{...}, ...}
func (h *TaskHandler) Setup(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/setup", body)
}

// 整书大纲草稿（§11 建书 ③：梗概 + 大致章节数 + 大致故事线 → Planner 提案，不落库，转发 Python API）。
// POST /api/v1/projects/:project_id/outline-draft  body: {"premise": "...", "chapter_count": 20, "storyline": "..."}
func (h *TaskHandler) OutlineDraft(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPyLong(c, "/internal/v1/projects/"+pid+"/outline-draft", body)
}

// 整书大纲确认落库（§11 ③：arc + 逐章目标整体替换 volume_outlines 单行，转发 Python API）。
// PUT /api/v1/projects/:project_id/outline  body: {"arc": [...], "chapters": [...], ...}
func (h *TaskHandler) PutOutline(c *gin.Context) {
	pid := c.Param("project_id")
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/outline", body)
}

// 整书大纲读取（§11 前端展示 / 重新生成输入，转发 Python API）。
// GET /api/v1/projects/:project_id/outline
func (h *TaskHandler) GetOutline(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/outline", nil)
}

// 世界观浏览（world_rules/hard_constraints + 势力/地点，转发 Python API）。
// GET /api/v1/projects/:project_id/world
func (h *TaskHandler) GetWorld(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/world", nil)
}

// 人物卡片浏览（静态基底 + 当前状态台账 §7.7，转发 Python API）。
// GET /api/v1/projects/:project_id/characters
func (h *TaskHandler) GetCharacters(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/characters", nil)
}

// 设定实体浏览（§7.11 ④ 自动建档：武器/功法/技能/地点，转发 Python API）。
// GET /api/v1/projects/:project_id/entities
func (h *TaskHandler) ListEntities(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/entities", nil)
}

// 事件台账（§7.4 中期记忆全量，可按 from_chapter/to_chapter 过滤，转发 Python API）。
// GET /api/v1/projects/:project_id/events
func (h *TaskHandler) ListEvents(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/events", nil)
}

// 人物状态变化历史（§7.7 追加式台账全量，含已失效行，转发 Python API）。
// GET /api/v1/projects/:project_id/characters/:character_id/state-history
func (h *TaskHandler) ListCharacterStates(c *gin.Context) {
	pid := c.Param("project_id")
	cid := c.Param("character_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/characters/"+cid+"/state-history", nil)
}

// 世界拓扑全量（§9 图谱：4 类节点 + 人物关系/地点层级边，转发 Python API）。
// GET /api/v1/projects/:project_id/graph
func (h *TaskHandler) GetGraph(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/graph", nil)
}

// 伏笔池台账（§7.9 状态机全量：planted/developing/resolved/dropped，转发 Python API）。
// GET /api/v1/projects/:project_id/foreshadows
func (h *TaskHandler) ListForeshadows(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/foreshadows", nil)
}

// 扫榜（§10 MCP Client 拉取外部榜单，只作建书前的灵感工具；全局无项目端点，转发 Python API）。
// GET /api/v1/rankings
func (h *TaskHandler) ListRankings(c *gin.Context) {
	h.forwardToPy(c, "/internal/v1/rankings", nil)
}

// 全局审计报告列表（阶段 4 审计视图导航）：最新在前，转发 Python API。
// GET /api/v1/projects/:project_id/global-audit
func (h *TaskHandler) ListGlobalAudits(c *gin.Context) {
	pid := c.Param("project_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/global-audit", nil)
}

// 全局审计报告详情（阶段 4 审计视图：findings 明细 + 抽样角色，转发 Python API）。
// GET /api/v1/projects/:project_id/global-audit/:report_id
func (h *TaskHandler) GetGlobalAudit(c *gin.Context) {
	pid := c.Param("project_id")
	rid := c.Param("report_id")
	h.forwardToPy(c, "/internal/v1/projects/"+pid+"/global-audit/"+rid, nil)
}

// 签发 JWT（§14.1 ③ 身份断言第一道门）：转发 Python API 签发端点。
// POST /api/v1/auth/token  body: {"username": "..."}  →  {token, user_id, expires_in}
// 唯一不挂 JWTMiddleware 的业务路由（否则无法登录）；网关不直连 DB，签发真源在 Python。
func (h *TaskHandler) AuthToken(c *gin.Context) {
	body, _ := io.ReadAll(c.Request.Body)
	h.forwardToPy(c, "/internal/v1/auth/token", body)
}

// forwardToPyLong 同 forwardToPy，但超时放宽到 180s（> Python 侧 LLM 120s 上限）。
// 仅长耗时同步生成端点使用：真实 Planner 生成整书大纲（几十上百章逐章目标）或设定骨架草稿
// 常超 30s，若用统一 30s 转发会在 LLM 完成前掐断 → 502。
func (h *TaskHandler) forwardToPyLong(c *gin.Context, path string, body []byte) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 180*time.Second)
	defer cancel()

	header := c.Request.Header.Clone()
	header.Set(HeaderUser, GetUserID(c))
	resp, err := h.py.ForwardLong(ctx, c.Request.Method, path, c.Request.URL.Query(), header, bytes.NewReader(body))
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "python_api_unreachable"})
		return
	}
	defer resp.Body.Close()
	c.Status(resp.StatusCode)
	c.Header("Content-Type", resp.Header.Get("Content-Type"))
	if _, err := io.Copy(c.Writer, resp.Body); err != nil {
		c.Abort()
	}
}

// forwardToPy 把请求体原样转发 Python API 并透传响应。
func (h *TaskHandler) forwardToPy(c *gin.Context, path string, body []byte) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
	defer cancel()

	header := c.Request.Header.Clone()
	header.Set(HeaderUser, GetUserID(c))
	resp, err := h.py.Forward(ctx, c.Request.Method, path, c.Request.URL.Query(), header, bytes.NewReader(body))
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "python_api_unreachable"})
		return
	}
	defer resp.Body.Close()
	c.Status(resp.StatusCode)
	c.Header("Content-Type", resp.Header.Get("Content-Type"))
	if _, err := io.Copy(c.Writer, resp.Body); err != nil {
		c.Abort()
	}
}

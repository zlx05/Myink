// 网关路由装配：唯一公网入口（鉴权占位 + 限流 + 三层闸门 + SSE + 转发）。
package handlers

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/gin-gonic/gin"
	"golang.org/x/time/rate"

	"myink/gateway/internal/config"
	"myink/gateway/internal/limiter"
	"myink/gateway/internal/pyapi"
	"myink/gateway/internal/queue"
	"myink/gateway/internal/redis"
	"myink/gateway/internal/trace"
)

// NewRouter 装配全部路由与中间件。
func NewRouter(cfg config.Config, r *redis.Client, rmq *queue.AMQP, py *pyapi.Client) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.Use(AdminNoStore())
	router.Use(gin.Recovery())
	router.Use(trace.Middleware())
	// 进程内令牌桶：粗粒度限流（防外部刷接口；细粒度配额走 gates.lua §13）
	router.Use(limiter.Middleware(rate.Limit(cfg.RatePerSec), cfg.RateBurst))

	taskH := NewTaskHandler(cfg, r, rmq, py)
	sseH := NewSSEHandler(r, py)
	healthH := NewHealthHandler(cfg, r, py)

	api := router.Group("/api/v1")
	// 签发端点不挂 JWT（否则无法登录）；业务路由一律 Bearer（§14.1 ③）
	api.POST("/auth/token", AuthRateLimit(r), taskH.AuthAction)
	api.POST("/auth/register", AuthRateLimit(r), taskH.AuthAction)

	secured := api.Group("", JWTMiddleware([]byte(cfg.JWTSecret)), SessionMiddleware(py))
	for _, path := range []string{
		"/overview", "/users", "/projects", "/projects/:project_id/chapters",
		"/projects/:project_id/chapters/:chapter_id", "/projects/:project_id/context",
		"/tasks", "/tasks/:task_id", "/tasks/:task_id/runs", "/runs", "/runs/:run_id", "/access-logs",
	} {
		secured.GET("/admin"+path, taskH.AdminRead)
	}
	{
		secured.GET("/auth/session", taskH.AuthAction)
		secured.POST("/auth/password", AuthRateLimit(r), taskH.AuthAction)
		secured.POST("/auth/logout", taskH.AuthAction)
		// 项目/章节读（多书展示前端，转发 Python API）
		secured.GET("/projects", taskH.ListProjects)
		secured.GET("/projects/:project_id/creation", taskH.GetCreation)
		secured.POST("/projects", taskH.CreateProject)
		// 作品信息更新（§6.9 每章目标字数可配）
		secured.PUT("/projects/:project_id", taskH.UpdateProject)
		// 整本书删除（硬删：守卫进行中任务 → 级联清业务表/向量/checkpoint/Redis 残留，转发 Python API）
		secured.DELETE("/projects/:project_id", taskH.DeleteProject)
		secured.GET("/projects/:project_id/chapters", taskH.ListChapters)
		// 章节详情（含正文/summary，阶段 4 前端章节编辑器读取正文；转发 Python API）
		secured.GET("/projects/:project_id/chapters/:chapter_id", taskH.GetChapter)
		// 章节编辑 / 记忆校正 / 级联删除 / 全局审计（阶段 3：正文轻编辑 + 增量记忆校正 + 长线治理，转发 Python API）
		secured.PUT("/projects/:project_id/chapters/:chapter_id/content", taskH.UpdateChapterContent)
		secured.POST("/projects/:project_id/chapters/:chapter_id/correct-memory", taskH.CorrectMemory)
		secured.DELETE("/projects/:project_id/chapters/:chapter_id", taskH.DeleteChapter)
		// 章节历史版本（阶段 4 版本表：列表/回退，转发 Python API）
		secured.GET("/projects/:project_id/chapters/:chapter_id/versions", taskH.ListChapterVersions)
		secured.POST("/projects/:project_id/chapters/:chapter_id/versions/:version/restore", taskH.RestoreChapterVersion)
		secured.POST("/projects/:project_id/global-audit", taskH.GlobalAudit)
		// 全局审计报告读（阶段 4 审计视图：列表/详情，转发 Python API）
		secured.GET("/projects/:project_id/global-audit", taskH.ListGlobalAudits)
		secured.GET("/projects/:project_id/global-audit/:report_id", taskH.GetGlobalAudit)
		// 创作设置（阶段 4：文风档案/样本提取/预设导入 + 每 Agent 模型路由，转发 Python API）
		secured.GET("/projects/:project_id/settings", taskH.ListSettings)
		secured.PUT("/projects/:project_id/settings", taskH.UpdateSettings)
		// 模型连接探针（设置页「添加网络模型」闭环：拉取可用模型 / 联通测试，转发 Python API）
		secured.POST("/projects/:project_id/settings/models", taskH.ListConnectionModels)
		secured.POST("/projects/:project_id/settings/test-connection", taskH.TestModelConnection)
		// 账号级环境配置（作品库即可进入：模型连接/路由 + MCP 扫榜，转发 Python API）
		secured.GET("/environment", taskH.GetEnvironment)
		secured.PUT("/environment", taskH.UpdateEnvironment)
		secured.POST("/environment/models", taskH.ListEnvironmentModels)
		secured.POST("/environment/test-connection", taskH.TestEnvironmentConnection)
		secured.POST("/environment/test-rankings", taskH.TestRankingsConnection)
		secured.GET("/skill-presets", taskH.SkillPresets)
		secured.GET("/genre-packs", taskH.GenrePacks)
		secured.PUT("/projects/:project_id/genre-pack", taskH.PutGenrePack)
		secured.POST("/projects/:project_id/genre-pack/restore", taskH.RestoreGenrePack)
		secured.POST("/projects/:project_id/style-samples", taskH.StyleSamples)
		secured.PUT("/projects/:project_id/style-profile", taskH.PutStyleProfile)
		// 建书向导 + 设定浏览（§7.11：创建作品 / 设定草稿 / 确认落库 / 世界观 / 人物卡片，转发 Python API）
		secured.POST("/projects/:project_id/setup-draft", taskH.SetupDraft)
		secured.PUT("/projects/:project_id/setup", taskH.Setup)
		// 整书大纲（§11 建书 ③：草稿 / 确认落库 / 读取，写作时注入逐章目标）
		secured.POST("/projects/:project_id/outline-draft", taskH.OutlineDraft)
		secured.PUT("/projects/:project_id/outline", taskH.PutOutline)
		secured.GET("/projects/:project_id/outline", taskH.GetOutline)
		secured.GET("/projects/:project_id/world", taskH.GetWorld)
		secured.GET("/projects/:project_id/characters", taskH.GetCharacters)
		// 设定实体浏览（§7.11 ④ 自动建档：武器/功法/技能/地点）
		secured.GET("/projects/:project_id/entities", taskH.ListEntities)
		// 事件台账（§7.4 中期记忆全量：此前事件只写不读，此路由把台账读出来）
		secured.GET("/projects/:project_id/events", taskH.ListEvents)
		// 人物状态变化历史（§7.7 追加式台账全量：old_value → new_value 时间线）
		secured.GET("/projects/:project_id/characters/:character_id/state-history", taskH.ListCharacterStates)
		// 世界拓扑全量（§9 图谱：4 类节点 + 人物关系/地点层级边）
		secured.GET("/projects/:project_id/graph", taskH.GetGraph)
		// 伏笔池台账（§7.9 状态机全量，前端设定页伏笔池区块）
		secured.GET("/projects/:project_id/foreshadows", taskH.ListForeshadows)
		// 扫榜灵感（§10 MCP Client 拉取外部榜单，只作建书前的灵感工具；全局无项目端点）
		secured.GET("/rankings", taskH.ListRankings)
		// 建单章生成任务
		secured.POST("/projects/:project_id/chapters/:chapter_id/generate", taskH.CreateChapter)
		// 建批次生成任务
		secured.POST("/projects/:project_id/batches/generate", taskH.CreateBatch)
		// 项目任务历史（阶段 4 任务视图：切书后展示该书过往任务，转发 Python API）
		secured.GET("/projects/:project_id/tasks", taskH.ListProjectTasks)
		// 任务详情（转发 Python API）
		secured.GET("/tasks/:task_id", taskH.GetTask)
		secured.POST("/tasks/:task_id/plan/confirm", taskH.ConfirmTaskPlan)
		secured.POST("/tasks/:task_id/cancel", taskH.TaskControl)
		// 批次控制 pause/resume/cancel（转发 Python API）
		secured.POST("/batches/:batch_id/:action", taskH.BatchControl)
		// 记忆候选：待确认池 / 确认 / 拒绝（§6.11 确认分流，转发 Python API）
		secured.GET("/projects/:project_id/candidates", taskH.ListCandidates)
		secured.POST("/projects/:project_id/candidates/:candidate_id/:action", taskH.CandidateAction)
		secured.GET("/projects/:project_id/lessons", taskH.ListLessons)
		secured.POST("/projects/:project_id/lessons/:lesson_id/:action", taskH.LessonAction)
		// SSE 进度事件
		secured.GET("/tasks/:task_id/events", sseH.Stream)
	}

	// 探针
	router.GET("/healthz", healthH.Live)
	router.GET("/readyz", healthH.Ready)

	// 阶段 5：前端静态托管（唯一入口 8080 同源出页面 + API）。
	// /assets 静态产物走 gin.Static；其余未匹配路径：/api、探针保持 JSON 404，
	// 前端 BrowserRouter 深链（/projects/:pid 刷新等）回退 index.html（SPA fallback）。
	dist := cfg.WebDistDir
	router.Use(func(c *gin.Context) {
		if strings.HasPrefix(c.Request.URL.Path, "/assets/") {
			c.Header("Cache-Control", "public, max-age=31536000, immutable")
		}
		c.Next()
	})
	router.Static("/assets", filepath.Join(dist, "assets"))
	serveIndex := func(c *gin.Context) {
		c.Header("Cache-Control", "no-store, max-age=0")
		c.Header("Pragma", "no-cache")
		c.Header("Expires", "0")
		c.File(filepath.Join(dist, "index.html"))
	}
	router.NoRoute(func(c *gin.Context) {
		p := c.Request.URL.Path
		if strings.HasPrefix(p, "/api/") || p == "/healthz" || p == "/readyz" {
			c.JSON(http.StatusNotFound, gin.H{"error": "not_found"})
			return
		}
		rel := strings.TrimPrefix(p, "/")
		// 防路径穿越（段级判断）：统一把反斜杠归一为正斜杠，再按段判断，
		// 兼容 Windows 本地运行时（filepath 以 \ 为分隔符，/foo\..\secret 也会逃出 dist）
		rel = strings.ReplaceAll(rel, "\\", "/")
		if strings.HasPrefix(rel, "..") || strings.Contains(rel, "/..") {
			serveIndex(c)
			return
		}
		f := filepath.Join(dist, filepath.Clean(filepath.FromSlash(rel)))
		if info, err := os.Stat(f); err == nil && !info.IsDir() {
			c.File(f)
			return
		}
		serveIndex(c)
	})

	return router
}

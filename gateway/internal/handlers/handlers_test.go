// 网关 HTTP 层测试（httptest 直打 router）：建章/建批次、闸门拒绝、转发、SSE、探针。
// 活 Redis :6380 + RabbitMQ :5672（入队断言走 -h-t- 观察队列）；Python API 用内存
// httptest 假服务替代（测转发，不依赖 8100）。
// 对 compose 起的 aiink-rabbitmq 需 AMQP_URL=amqp://aiink:aiink@localhost:5672/ 否则 dial 403 静默 skip。
package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	amqp091 "github.com/rabbitmq/amqp091-go"

	"aiink/gateway/internal/config"
	"aiink/gateway/internal/pyapi"
	"aiink/gateway/internal/queue"
	"aiink/gateway/internal/redis"
)

const testQueuePrefix = "-h-t-" // RabbitMQ 拓扑隔离：不碰运行中网关/worker 的真实队列

// bearer 生成 JWT Bearer 头（§14.1 ③：业务路由一律要求已签名 token）。
// 密钥与 newRouter 的 cfg.JWTSecret 同源（config.Load：默认 DevJWTSecret，环境显式
// 设 JWT_SECRET 时两边读同一值），验签一致。tier 默认 normal（老 token 无 claim 回落 0）。
func bearer(t *testing.T, sub string) string {
	t.Helper()
	return bearerTier(t, sub, "normal")
}

// bearerTier 同 bearer 但带 tier claim（VIP → 网关入队高优先级）。
func bearerTier(t *testing.T, sub, tier string) string {
	t.Helper()
	tok := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub":  sub,
		"tier": tier,
		"iss":  "aiink",
		"exp":  time.Now().Add(time.Hour).Unix(),
	})
	s, err := tok.SignedString([]byte(config.Load().JWTSecret))
	if err != nil {
		t.Fatalf("签 JWT 失败: %v", err)
	}
	return "Bearer " + s
}

func newRouter(t *testing.T, r *redis.Client, py *pyapi.Client) *gin.Engine {
	t.Helper()
	cfg := config.Load()
	cfg.QuotaDaily = 2
	cfg.ConcurrencyLimit = 1
	cfg.DailyBudget = 1.0
	cfg.RatePerSec = 1000 // 测试不触发限流
	cfg.RateBurst = 1000
	cfg.QueuePrefix = testQueuePrefix
	return NewRouter(cfg, r, newTestRMQ(t, cfg), py)
}

// newTestRMQ 连 RabbitMQ（skip-if-unreachable :5672），声明 -h-t- 前缀的测试拓扑。
func newTestRMQ(t *testing.T, cfg config.Config) *queue.AMQP {
	t.Helper()
	conn, err := amqp091.Dial(cfg.AmqpURL)
	if err != nil {
		t.Skipf("aiink-rabbitmq 不可达 %s: %v", cfg.AmqpURL, err)
	}
	_ = conn.Close()
	rmq, err := queue.DialAMQP(cfg)
	if err != nil {
		t.Fatalf("DialAMQP: %v", err)
	}
	t.Cleanup(func() { _ = rmq.Close() })
	return rmq
}

// bindObserver 建独占 auto-delete 观察队列，绑定 -h-t- 交换机的 rk=tasks —— 之后入队发布的
// 每条消息都被复制一份（读后即弃，测试隔离）。返回队列名 + 连接（readObserver 复用同一连接）。
func bindObserver(t *testing.T) (string, *amqp091.Connection) {
	t.Helper()
	conn, err := amqp091.Dial(config.Load().AmqpURL)
	if err != nil {
		t.Fatalf("observer dial: %v", err)
	}
	ch, err := conn.Channel()
	if err != nil {
		t.Fatalf("observer channel: %v", err)
	}
	q, err := ch.QueueDeclare("", false, true, true, false, nil)
	if err != nil {
		t.Fatalf("observer declare: %v", err)
	}
	if err := ch.QueueBind(q.Name, queue.KeyTasks, queue.ExchangeTasks+testQueuePrefix, false, nil); err != nil {
		t.Fatalf("observer bind: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return q.Name, conn
}

// readObserver 读观察队列当前全部消息（非阻塞，读到空为止）。
func readObserver(t *testing.T, conn *amqp091.Connection, q string) []amqp091.Delivery {
	t.Helper()
	ch, err := conn.Channel()
	if err != nil {
		t.Fatalf("read channel: %v", err)
	}
	defer ch.Close()
	var out []amqp091.Delivery
	for {
		d, ok, err := ch.Get(q, true)
		if err != nil {
			t.Fatalf("read get: %v", err)
		}
		if !ok {
			return out
		}
		out = append(out, d)
	}
}

// purgeTestQueues 清空 -h-t- 前缀的持久队列（入队残留不跨测试累积）。
func purgeTestQueues(t *testing.T) {
	t.Helper()
	conn, err := amqp091.Dial(config.Load().AmqpURL)
	if err != nil {
		return
	}
	defer conn.Close()
	ch, err := conn.Channel()
	if err != nil {
		return
	}
	defer ch.Close()
	for _, q := range []string{queue.MainQueue(testQueuePrefix), queue.DelayQueue(testQueuePrefix), queue.DlqQueue(testQueuePrefix)} {
		_, _ = ch.QueuePurge(q, false)
	}
}

func newTestRedis(t *testing.T) *redis.Client {
	t.Helper()
	r := redis.New(config.Load().RedisAddr, "")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := r.Ping(ctx); err != nil {
		t.Skipf("aiink-redis 不可达: %v", err)
	}
	return r
}

// fakePy 内存假 Python API：GET 详情 / POST 控制 / 项目与章节读返回固定 JSON。
func fakePy() *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasSuffix(req.URL.Path, "/tasks/detail-test"):
			// 对齐契约形状（spec/api-openapi.json TaskDetailOut；progress 用 current/total）
			fmt.Fprint(w, `{"task_id":"detail-test","task_type":"batch_generate","status":"done",`+
				`"payload":{},"retry_count":0,"runs":[],"progress":{"current":1,"total":2}}`)
		case strings.HasSuffix(req.URL.Path, "/tasks/missing-task"):
			// 模拟任务尚未物化（入队→DB 异步窗口内）→ 上游明确 404
			http.Error(w, `{"detail":"任务不存在: missing-task"}`, http.StatusNotFound)
		case strings.HasSuffix(req.URL.Path, "/projects"):
			fmt.Fprint(w, `[{"id":"p1","title":"书A","genre":"仙侠","current_chapter":5},`+
				`{"id":"p2","title":"书B","genre":"科幻","current_chapter":0}]`)
		case strings.Contains(req.URL.Path, "/chapters"):
			fmt.Fprint(w, `[{"id":"ch-1","chapter_seq":1,"title":"第1章","status":"confirmed"},`+
				`{"id":"ch-2","chapter_seq":2,"title":"第2章","status":"draft"}]`)
		case strings.Contains(req.URL.Path, "/pause"):
			fmt.Fprint(w, `{"task_id":"batch-x","status":"paused"}`)
		case strings.HasSuffix(req.URL.Path, "/rankings"):
			// 对齐契约 RankingsOut（source 必填；样例数据降级路径）
			fmt.Fprint(w, `{"source":"sample","tool":"","fetched_at":null,`+
				`"error":"RANKINGS_ENABLED=0 已禁用扫榜","items":[{"rank":1,`+
				`"title":"【示例】九天剑帝","author":"青竹","tags":["仙侠","无敌流"],"hot":"热榜 1"}]}`)
		case strings.HasSuffix(req.URL.Path, "/graph"):
			// 对齐契约 WorldGraphOut（4 类节点 + 活跃/失效关系 + 层级边）
			fmt.Fprint(w, `{"nodes":[{"id":"n1","name":"林晚","type":"character","realm_cap":"金丹"},`+
				`{"id":"n2","name":"沈岳","type":"character","realm_cap":"元婴"},`+
				`{"id":"n3","name":"青云宗","type":"faction","stance":"正道"},`+
				`{"id":"n4","name":"青云山","type":"location","parent_id":"n5"},`+
				`{"id":"n5","name":"青云州","type":"location","parent_id":null},`+
				`{"id":"n6","name":"焚天剑","type":"entity","entity_type":"item"}],`+
				`"edges":[{"source_id":"n1","target_id":"n2","edge_type":"hostile",`+
				`"confidence":0.9,"expired":false,"source_chapter":3},`+
				`{"source_id":"n2","target_id":"n1","edge_type":"knows",`+
				`"confidence":0.7,"expired":true,"source_chapter":8},`+
				`{"source_id":"n4","target_id":"n5","edge_type":"hierarchy","expired":false}]}`)
		case strings.HasSuffix(req.URL.Path, "/outline"):
			// 对齐契约 BookOutlineOut（Objective + 卷 + 逐章目标；GET 无大纲时为 null）
			fmt.Fprint(w, `{"outline":{"premise":"少年得玉佩追寻真相","chapter_count":20,`+
				`"storyline":"前期宗门、中期追查、后期决战",`+
				`"objective":"从杂役修士成为宗门长老并公开父辈冤案真相",`+
				`"volumes":[{"volume_seq":1,"title":"第一卷 · 青云山下","theme":"立身",`+
				`"goal":"入宗立足并发现玉佩疑点","key_results":["通过入门试炼","拜入长老座下"],`+
				`"end_event":"主角被迫离开青云宗",`+
				`"chapters":[{"seq":1,"title":"第一章 玉佩","goal":"得玉佩、初入青云宗",`+
				`"beats":["得玉佩","遇苏瑶"]}]}]}}`)
		case strings.HasSuffix(req.URL.Path, "/auth/token"):
			fmt.Fprint(w, `{"token":"t-jwt","user_id":"u-1","expires_in":1800}`)
		default:
			fmt.Fprint(w, `{"error":"not_found"}`)
		}
	}))
}

// recordingPy 记录所有收到的转发路径，用于断言转发目标（BatchControl 曾转发错路径 batches→tasks）。
func recordingPy(paths *[]string) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		*paths = append(*paths, req.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"ok":true}`)
	}))
}

func TestCreateChapter202(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)
	defer purgeTestQueues(t)

	// 清理用户闸门键，隔离（含 inflight 残留）
	uid := "web-test-user"
	ctx := context.Background()
	keys, _ := r.Raw().Keys(ctx, "rate:quota:"+uid+":*").Result()
	bk, _ := r.Raw().Keys(ctx, "rate:bookquota:"+uid+":*").Result()
	bkc, _ := r.Raw().Keys(ctx, "rate:bookcnt:"+uid+":*").Result()
	bk = append(bk, bkc...)
	keys = append(keys, bk...)
	_ = r.Raw().Del(ctx, append(keys, "rate:inflight:"+uid+":proj-1", "rate:quota:"+uid+":"+time.Now().Format("2006-01-02"))...).Err()
	// 入队会真实 SADD inflight + 扣配额（用户 + 每书，活 Redis），注册末尾清理防污染其他用户/测试
	defer func() {
		_ = r.Raw().Del(ctx, "rate:inflight:"+uid+":proj-1", "rate:quota:"+uid+":"+time.Now().Format("2006-01-02"), "rate:bookquota:"+uid+":proj-1:"+time.Now().Format("2006-01-02"), "rate:bookcnt:"+uid+":"+time.Now().Format("2006-01-02")).Err()
	}()

	body := `{"seq":1,"user_instruction":"写第一章"}`
	req := httptest.NewRequest(http.MethodPost,
		"/api/v1/projects/proj-1/chapters/ch-1/generate", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", bearer(t, uid))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("应 202，实际 %d body=%s", w.Code, w.Body.String())
	}
	var resp struct {
		TaskID string `json:"task_id"`
		Status string `json:"status"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败: %v", err)
	}
	if resp.TaskID == "" || resp.Status != "queued" {
		t.Fatalf("应返回 task_id + queued，实际 %+v", resp)
	}
	streamKey := "queue:sse:" + resp.TaskID
	defer r.Raw().Del(context.Background(), streamKey)
	queued, err := r.Raw().XRange(context.Background(), streamKey, "-", "+").Result()
	if err != nil || len(queued) == 0 || queued[0].Values["status"] != "queued" {
		t.Fatalf("返回 202 前应建立 queued SSE 流，实际 events=%v err=%v", queued, err)
	}
	// 应透传 X-Trace-ID 头（§17.2 全链路）
	if w.Header().Get("X-Request-ID") == "" {
		t.Fatal("应带 X-Request-ID 响应头")
	}
}

func TestCreateChapterPriorityFromJWT(t *testing.T) {
	// VIP 优先：JWT tier=vip → 网关把 RabbitMQ 消息 priority 置 VIPPriority（主队列 x-max-priority=10）。
	// 观察队列读到 priority 属性（非排序断言——优先级只影响消费排序，不影响发布）。
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)
	obs, conn := bindObserver(t)
	defer purgeTestQueues(t)

	uid := "web-vip-user"
	ctx := context.Background()
	today := time.Now().Format("2006-01-02")
	defer func() {
		_ = r.Raw().Del(ctx, "rate:inflight:"+uid+":proj-1", "rate:quota:"+uid+":"+today,
			"rate:bookquota:"+uid+":proj-1:"+today, "rate:bookcnt:"+uid+":"+today).Err()
	}()

	req := httptest.NewRequest(http.MethodPost,
		"/api/v1/projects/proj-1/chapters/ch-1/generate", strings.NewReader(`{"seq":1}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", bearerTier(t, uid, "vip"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("应 202，实际 %d body=%s", w.Code, w.Body.String())
	}
	msgs := readObserver(t, conn, obs)
	if len(msgs) != 1 {
		t.Fatalf("应收到 1 条消息，实际 %d", len(msgs))
	}
	if want := uint8(config.Load().VIPPriority); msgs[0].Priority != want {
		t.Fatalf("VIP 消息 priority 应为 %d，实际 %d", want, msgs[0].Priority)
	}
}

func TestCreateChapterQuotaRejected(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	uid := "web-test-quota"
	ctx := context.Background()
	today := time.Now().Format("2006-01-02")
	// 清残留后占满配额；go-redis Del 调用即执行，defer 必须包闭包
	keys, _ := r.Raw().Keys(ctx, "rate:quota:"+uid+":*").Result()
	bk, _ := r.Raw().Keys(ctx, "rate:bookquota:"+uid+":*").Result()
	bkc, _ := r.Raw().Keys(ctx, "rate:bookcnt:"+uid+":*").Result()
	bk = append(bk, bkc...)
	keys = append(keys, bk...)
	_ = r.Raw().Del(ctx, append(keys, "rate:inflight:"+uid+":proj-1", "rate:quota:"+uid+":"+today)...).Err()
	_ = r.Raw().Set(ctx, "rate:quota:"+uid+":"+today, "2", 0).Err() // 占满配额
	defer func() {
		_ = r.Raw().Del(ctx, append(keys, "rate:inflight:"+uid+":proj-1", "rate:quota:"+uid+":"+today)...).Err()
	}()

	req := httptest.NewRequest(http.MethodPost,
		"/api/v1/projects/proj-1/chapters/ch-1/generate", strings.NewReader(`{"seq":1}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", bearer(t, uid))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusTooManyRequests {
		t.Fatalf("配额超限应 429，实际 %d body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "QUOTA_EXCEEDED") {
		t.Fatalf("错误体应含 QUOTA_EXCEEDED，实际 %s", w.Body.String())
	}
}

func TestCreateBatchDeductN(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	uid := "web-test-batch"
	ctx := context.Background()
	today := time.Now().Format("2006-01-02")
	// 先清残留闸门键（前次运行可能遗留），再注册末尾清理
	keys, _ := r.Raw().Keys(ctx, "rate:quota:"+uid+":*").Result()
	bk, _ := r.Raw().Keys(ctx, "rate:bookquota:"+uid+":*").Result()
	bkc, _ := r.Raw().Keys(ctx, "rate:bookcnt:"+uid+":*").Result()
	bk = append(bk, bkc...)
	keys = append(keys, bk...)
	_ = r.Raw().Del(ctx, append(keys, "rate:inflight:"+uid+":proj-1", "rate:quota:"+uid+":"+today)...).Err()
	// go-redis Del 立即执行，defer 必须用运行时实际键名（快照不含运行期新增的 bookquota）
	defer func() {
		_ = r.Raw().Del(ctx, "rate:inflight:"+uid+":proj-1", "rate:quota:"+uid+":"+today, "rate:bookquota:"+uid+":proj-1:"+today, "rate:bookcnt:"+uid+":"+today).Err()
	}()

	body := `{"size":2,"start":1}`
	req := httptest.NewRequest(http.MethodPost,
		"/api/v1/projects/proj-1/batches/generate", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", bearer(t, uid))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("应 202，实际 %d body=%s", w.Code, w.Body.String())
	}
	quota := r.Raw().Get(ctx, "rate:quota:"+uid+":"+today).Val()
	if quota != "2" {
		t.Fatalf("批次应扣 2 配额，实际 %q", quota)
	}
}

func TestCreateBatchStartDefaultsOne(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)
	obs, conn := bindObserver(t)
	defer purgeTestQueues(t)

	uid := "web-batch-start-default"
	ctx := context.Background()
	today := time.Now().Format("2006-01-02")
	defer func() {
		_ = r.Raw().Del(ctx, "rate:inflight:"+uid+":proj-1", "rate:quota:"+uid+":"+today,
			"rate:bookquota:"+uid+":proj-1:"+today, "rate:bookcnt:"+uid+":"+today).Err()
	}()

	// 缺省 start（复查 B1）：Go 零值 0 原会穿透到 worker 让批次从第 0 章写起；修后应默认 1。
	// 权威写序校验在 worker（_guard_write_order：首章必须 = max_seq+1），网关只做语法层。
	body := `{"size":2}`
	req := httptest.NewRequest(http.MethodPost,
		"/api/v1/projects/proj-1/batches/generate", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", bearer(t, uid))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("应 202，实际 %d body=%s", w.Code, w.Body.String())
	}
	var lastStart *int
	for _, d := range readObserver(t, conn, obs) {
		var msg struct {
			ProjectID string `json:"project_id"`
			TaskType  string `json:"task_type"`
			Payload   struct {
				Size  int `json:"size"`
				Start int `json:"start"`
			} `json:"payload"`
		}
		if json.Unmarshal(d.Body, &msg) != nil || msg.ProjectID != "proj-1" {
			continue
		}
		if msg.TaskType == "batch_generate" {
			v := msg.Payload.Start
			lastStart = &v
		}
	}
	if lastStart == nil || *lastStart != 1 {
		t.Fatalf("缺省 start 应为 1，实际 %v", lastStart)
	}
}

func TestGetTaskForwards(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/detail-test", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d", w.Code)
	}
	if !strings.Contains(w.Body.String(), `"progress":{"current":1,"total":2}`) {
		t.Fatalf("应透传 Python API 详情，实际 %s", w.Body.String())
	}
}

func TestGetTaskNotFoundPassesThrough(t *testing.T) {
	// 回归：任务尚未物化（入队→DB 落行的异步窗口内）时 Python API 返回 404，
	// 网关曾误转 502 python_api_unreachable → 前端 raise_for_status 崩溃。
	// 应透传 404，前端轮询视为"在途"继续等。
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/missing-task", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Fatalf("应透传 404，实际 %d body=%s", w.Code, w.Body.String())
	}
}

func TestBatchPauseForwards(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/batches/batch-x/pause", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"status":"paused"`) {
		t.Fatalf("应透传暂停结果，实际 %s", w.Body.String())
	}
}

func TestBatchControlForwardsToTasksPath(t *testing.T) {
	// 回归：BatchControl 曾转发到 /internal/v1/batches/{id}/{action}，但 Python API
	// 实际路由是 /internal/v1/tasks/{id}/{action} —— 路径不匹配导致 pause/resume/cancel 全 404。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/batches/batch-x/resume", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if len(paths) != 1 || paths[0] != "/internal/v1/tasks/batch-x/resume" {
		t.Fatalf("应转发 /internal/v1/tasks/batch-x/resume，实际 %v", paths)
	}
}

func TestTaskCancelForwardsToTasksPath(t *testing.T) {
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/tasks/manual-task/cancel", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if len(paths) != 1 || paths[0] != "/internal/v1/tasks/manual-task/cancel" {
		t.Fatalf("应转发单章取消路径，实际 %v", paths)
	}
}

func TestListProjectTasksForwards(t *testing.T) {
	// 阶段 4 任务视图：GET /api/v1/projects/:pid/tasks 应转发 Python API 任务历史路径。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/tasks", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if len(paths) != 1 || paths[0] != "/internal/v1/projects/p1/tasks" {
		t.Fatalf("应转发 /internal/v1/projects/p1/tasks，实际 %v", paths)
	}
}

func TestListProjectsForwards(t *testing.T) {
	// 多书展示前端：GET /api/v1/projects 应透传 Python API 项目列表。
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"title":"书B"`) {
		t.Fatalf("应透传项目列表，实际 %s", w.Body.String())
	}
}

func TestDeleteProjectForwards(t *testing.T) {
	// 整本书删除（阶段 6 硬删）：DELETE /api/v1/projects/:pid 应转发 Python API 删除路径。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodDelete, "/api/v1/projects/p1", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if len(paths) != 1 || paths[0] != "/internal/v1/projects/p1" {
		t.Fatalf("应转发 /internal/v1/projects/p1，实际 %v", paths)
	}
}

func TestListChaptersForwards(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/chapters", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"status":"draft"`) {
		t.Fatalf("应透传章节列表，实际 %s", w.Body.String())
	}
}

func TestRankingsForwards(t *testing.T) {
	// 扫榜（§10，建书前灵感工具，全局无项目端点）：应转发 Python API 内部路径 + 透传 RankingsOut 响应。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/rankings", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if len(paths) != 1 || paths[0] != "/internal/v1/rankings" {
		t.Fatalf("应转发 /internal/v1/rankings，实际 %v", paths)
	}
}

func TestGraphForwards(t *testing.T) {
	// 关系图谱（§9 世界拓扑）：GET /api/v1/projects/:pid/graph 应转发 Python API 路径 + 透传拓扑响应。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/graph", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if len(paths) != 1 || paths[0] != "/internal/v1/projects/p1/graph" {
		t.Fatalf("应转发 /internal/v1/projects/p1/graph，实际 %v", paths)
	}
}

func TestGetChapterForwards(t *testing.T) {
	// 阶段 4 前端章节编辑器：GET 单章详情（含正文/summary）应转发 Python API 路径。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/chapters/ch-1", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if len(paths) != 1 || paths[0] != "/internal/v1/projects/p1/chapters/ch-1" {
		t.Fatalf("应转发 /internal/v1/projects/p1/chapters/ch-1，实际 %v", paths)
	}
}

func TestCreateChapterRewritePassthrough(t *testing.T) {
	// 显式重写（§7.3 失效重建触发点）：body rewrite=true 应透传到 payload 入队。
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)
	obs, conn := bindObserver(t)
	defer purgeTestQueues(t)

	uid := "web-rewrite-test"
	ctx := context.Background()
	today := time.Now().Format("2006-01-02")
	defer func() {
		_ = r.Raw().Del(ctx, "rate:inflight:"+uid+":proj-rewrite", "rate:quota:"+uid+":"+today,
			"rate:bookquota:"+uid+":proj-rewrite:"+today, "rate:bookcnt:"+uid+":"+today).Err()
	}()

	req := httptest.NewRequest(http.MethodPost,
		"/api/v1/projects/proj-rewrite/chapters/ch-rw/generate", strings.NewReader(`{"seq":1,"rewrite":true}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", bearer(t, uid))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusAccepted {
		t.Fatalf("应 202，实际 %d body=%s", w.Code, w.Body.String())
	}
	var got *bool
	for _, d := range readObserver(t, conn, obs) {
		var msg struct {
			ProjectID string `json:"project_id"`
			TaskType  string `json:"task_type"`
			Payload   struct {
				Seq     int  `json:"seq"`
				Rewrite bool `json:"rewrite"`
			} `json:"payload"`
		}
		if json.Unmarshal(d.Body, &msg) != nil || msg.ProjectID != "proj-rewrite" {
			continue
		}
		if msg.TaskType == "chapter_generate" {
			v := msg.Payload.Rewrite
			got = &v
		}
	}
	if got == nil || !*got {
		t.Fatalf("rewrite=true 应透传到 payload，实际 %v", got)
	}
}

func TestChapterEditCorrectDeleteForwards(t *testing.T) {
	// 阶段 3 三端点（编辑正文 / 记忆校正 / 级联删除）均为转发路由，应把方法与路径
	// 原样转给 Python API（与 BatchControl 同款回归：曾转发错路径导致 404）。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	cases := []struct {
		name, method, path, body string
	}{
		{"编辑正文", http.MethodPut, "/api/v1/projects/p1/chapters/ch-1/content", `{"content":"改标点后的正文。"}`},
		{"校正记忆", http.MethodPost, "/api/v1/projects/p1/chapters/ch-1/correct-memory", ``},
		{"级联删除", http.MethodDelete, "/api/v1/projects/p1/chapters/ch-1", ``},
		{"全局审计", http.MethodPost, "/api/v1/projects/p1/global-audit", ``},
	}
	want := []string{
		"/internal/v1/projects/p1/chapters/ch-1/content",
		"/internal/v1/projects/p1/chapters/ch-1/correct-memory",
		"/internal/v1/projects/p1/chapters/ch-1",
		"/internal/v1/projects/p1/global-audit",
	}
	for i, c := range cases {
		var rd io.Reader
		if c.body != "" {
			rd = strings.NewReader(c.body)
		}
		req := httptest.NewRequest(c.method, c.path, rd)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", bearer(t, "dev"))
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("case %d(%s) 应 200，实际 %d body=%s", i, c.name, w.Code, w.Body.String())
		}
		if len(paths) != i+1 {
			t.Fatalf("case %d(%s) 应转发 %d 次，实际 %v", i, c.name, i+1, paths)
		}
		if paths[i] != want[i] {
			t.Fatalf("case %d(%s) 应转发 %s，实际 %s", i, c.name, want[i], paths[i])
		}
	}
}

func TestChapterVersionForwards(t *testing.T) {
	// 阶段 4 版本表两端点（版本列表 / 回退）均为转发路由，应把方法与路径原样转给 Python API。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	cases := []struct {
		name, method, path, body string
	}{
		{"版本列表", http.MethodGet, "/api/v1/projects/p1/chapters/ch-1/versions", ``},
		{"回退到 v2", http.MethodPost, "/api/v1/projects/p1/chapters/ch-1/versions/2/restore", ``},
	}
	want := []string{
		"/internal/v1/projects/p1/chapters/ch-1/versions",
		"/internal/v1/projects/p1/chapters/ch-1/versions/2/restore",
	}
	for i, c := range cases {
		var rd io.Reader
		if c.body != "" {
			rd = strings.NewReader(c.body)
		}
		req := httptest.NewRequest(c.method, c.path, rd)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", bearer(t, "dev"))
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("case %d(%s) 应 200，实际 %d body=%s", i, c.name, w.Code, w.Body.String())
		}
		if len(paths) != i+1 {
			t.Fatalf("case %d(%s) 应转发 %d 次，实际 %v", i, c.name, i+1, paths)
		}
		if paths[i] != want[i] {
			t.Fatalf("case %d(%s) 应转发 %s，实际 %s", i, c.name, want[i], paths[i])
		}
	}
}

func TestSettingsAndStyleForwards(t *testing.T) {
	// 阶段 4 设置页 + 账号级环境配置转发路由，应把方法与路径原样转给 Python API。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	cases := []struct {
		name, method, path, body string
	}{
		{"读设置", http.MethodGet, "/api/v1/projects/p1/settings", ``},
		{"写模型路由", http.MethodPut, "/api/v1/projects/p1/settings", `{"model_routes":{"planner":"deepseek-v4-pro"}}`},
		{"读环境配置", http.MethodGet, "/api/v1/environment", ``},
		{"写环境配置", http.MethodPut, "/api/v1/environment", `{"model_routes":{"planner":"deepseek-v4-pro"}}`},
		{"预设列表", http.MethodGet, "/api/v1/skill-presets", ``},
		{"题材目录", http.MethodGet, "/api/v1/genre-packs", ``},
		{"本书题材", http.MethodPut, "/api/v1/projects/p1/genre-pack", `{"pacing":"三章一反馈"}`},
		{"恢复题材", http.MethodPost, "/api/v1/projects/p1/genre-pack/restore", ``},
		{"样本提取", http.MethodPost, "/api/v1/projects/p1/style-samples", `{"samples":["第一章正文。"]}`},
		{"文风档案确认", http.MethodPut, "/api/v1/projects/p1/style-profile", `{"profile":{"pov":"限知"}}`},
	}
	want := []string{
		"/internal/v1/projects/p1/settings",
		"/internal/v1/projects/p1/settings",
		"/internal/v1/environment",
		"/internal/v1/environment",
		"/internal/v1/skill-presets",
		"/internal/v1/genre-packs",
		"/internal/v1/projects/p1/genre-pack",
		"/internal/v1/projects/p1/genre-pack/restore",
		"/internal/v1/projects/p1/style-samples",
		"/internal/v1/projects/p1/style-profile",
	}
	for i, c := range cases {
		var rd io.Reader
		if c.body != "" {
			rd = strings.NewReader(c.body)
		}
		req := httptest.NewRequest(c.method, c.path, rd)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", bearer(t, "dev"))
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("case %d(%s) 应 200，实际 %d body=%s", i, c.name, w.Code, w.Body.String())
		}
		if len(paths) != i+1 {
			t.Fatalf("case %d(%s) 应转发 %d 次，实际 %v", i, c.name, i+1, paths)
		}
		if paths[i] != want[i] {
			t.Fatalf("case %d(%s) 应转发 %s，实际 %s", i, c.name, want[i], paths[i])
		}
	}
}

func TestBookSetupAndWorldForwards(t *testing.T) {
	// §7.11 建书向导 + 设定浏览 5 端点（建书、设定草稿、确认落库、世界观、人物卡片）
	// 均为转发路由，应把方法与路径原样转给 Python API（同 BatchControl 回归约定）。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	cases := []struct {
		name, method, path, body string
	}{
		{"建书", http.MethodPost, "/api/v1/projects", `{"title":"破晓录","genre":"历史悬疑","target_words":3000}`},
		{"更新作品", http.MethodPut, "/api/v1/projects/p1", `{"target_words":3500}`},
		{"设定草稿", http.MethodPost, "/api/v1/projects/p1/setup-draft", `{"premise":"少年闯仙途。"}`},
		{"确认落库", http.MethodPut, "/api/v1/projects/p1/setup", `{"hard_constraints":["凡人不可御剑"]}`},
		{"大纲草稿", http.MethodPost, "/api/v1/projects/p1/outline-draft", `{"premise":"少年得玉佩追寻真相","chapter_count":20,"storyline":"前期宗门"}`},
		{"确认大纲", http.MethodPut, "/api/v1/projects/p1/outline", `{"objective":"成为宗门长老并公开真相","volumes":[{"title":"第一卷","goal":"入宗立足","chapters":[{"title":"第一章","goal":"入宗"}]}]}`},
		{"大纲读取", http.MethodGet, "/api/v1/projects/p1/outline", ``},
		{"世界观", http.MethodGet, "/api/v1/projects/p1/world", ``},
		{"人物卡片", http.MethodGet, "/api/v1/projects/p1/characters", ``},
		{"设定实体", http.MethodGet, "/api/v1/projects/p1/entities", ``},
	}
	want := []string{
		"/internal/v1/projects",
		"/internal/v1/projects/p1",
		"/internal/v1/projects/p1/setup-draft",
		"/internal/v1/projects/p1/setup",
		"/internal/v1/projects/p1/outline-draft",
		"/internal/v1/projects/p1/outline",
		"/internal/v1/projects/p1/outline",
		"/internal/v1/projects/p1/world",
		"/internal/v1/projects/p1/characters",
		"/internal/v1/projects/p1/entities",
	}
	for i, c := range cases {
		var rd io.Reader
		if c.body != "" {
			rd = strings.NewReader(c.body)
		}
		req := httptest.NewRequest(c.method, c.path, rd)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", bearer(t, "dev"))
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("case %d(%s) 应 200，实际 %d body=%s", i, c.name, w.Code, w.Body.String())
		}
		if len(paths) != i+1 {
			t.Fatalf("case %d(%s) 应转发 %d 次，实际 %v", i, c.name, i+1, paths)
		}
		if paths[i] != want[i] {
			t.Fatalf("case %d(%s) 应转发 %s，实际 %s", i, c.name, want[i], paths[i])
		}
	}
}

func TestGlobalAuditReadForwards(t *testing.T) {
	// 阶段 4 审计视图：报告列表 / 详情两个读端点应转发 Python API 路径。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	cases := []struct {
		name, method, path string
	}{
		{"报告列表", http.MethodGet, "/api/v1/projects/p1/global-audit"},
		{"报告详情", http.MethodGet, "/api/v1/projects/p1/global-audit/rpt-1"},
	}
	want := []string{
		"/internal/v1/projects/p1/global-audit",
		"/internal/v1/projects/p1/global-audit/rpt-1",
	}
	for i, c := range cases {
		req := httptest.NewRequest(c.method, c.path, nil)
		req.Header.Set("Authorization", bearer(t, "dev"))
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("case %d(%s) 应 200，实际 %d body=%s", i, c.name, w.Code, w.Body.String())
		}
		if len(paths) != i+1 || paths[i] != want[i] {
			t.Fatalf("case %d(%s) 应转发 %s，实际 %v", i, c.name, want[i], paths)
		}
	}
}

func TestHealthzReadyz(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	// /healthz 存活
	w := httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("healthz 应 200，实际 %d", w.Code)
	}

	// /readyz：redis ok + 假 Python API ok，但 worker 心跳无 → degraded 503（阶段 2 单 worker）。
	// 开发环境可能有真 worker 在写心跳（5s 写一次，TTL 15s），先删心跳再断言；若恰逢 worker 重写则删后重试。
	ctx := context.Background()
	heartbeats := func() []string {
		ks, _ := r.Raw().Keys(ctx, "queue:heartbeat:*").Result()
		return ks
	}
	var w2 *httptest.ResponseRecorder
	for attempt := 0; attempt < 3; attempt++ {
		_ = r.Raw().Del(ctx, heartbeats()...).Err()
		w2 = httptest.NewRecorder()
		router.ServeHTTP(w2, httptest.NewRequest(http.MethodGet, "/readyz", nil))
		if w2.Code == http.StatusServiceUnavailable {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if w2.Code != http.StatusServiceUnavailable {
		t.Fatalf("无 worker 心跳应 degraded 503，实际 %d body=%s", w2.Code, w2.Body.String())
	}
	if !strings.Contains(w2.Body.String(), "worker") {
		t.Fatalf("应指出 worker 降级，实际 %s", w2.Body.String())
	}
	// 还原：删掉本次测试写的心跳（若有），避免干扰后续测试/真 worker 探活
	_ = r.Raw().Del(ctx, heartbeats()...).Err()
}

func TestSSEFrameForward(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	// 预写 running + done 事件到通道（模拟 worker 完整链路；done 触发 handler 收尾）
	// 与 worker 侧 XADD 扁平字段对齐：event=status, status=done
	taskID := "sse-test-task"
	ctx := context.Background()
	// 清理：测试直写不经 worker（无 TTL），不留键会泄漏（曾留一个永不过期的 queue:sse:sse-test-task）
	defer r.Raw().Del(ctx, "queue:sse:"+taskID)
	_, _ = r.XAdd(ctx, "queue:sse:"+taskID, map[string]any{
		"event": "status", "task_id": taskID, "status": "running",
	})
	_, _ = r.XAdd(ctx, "queue:sse:"+taskID, map[string]any{
		"event": "status", "task_id": taskID, "status": "done",
	})

	req := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/"+taskID+"/events", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("SSE 应 200，实际 %d", w.Code)
	}
	if ct := w.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/event-stream") {
		t.Fatalf("应 text/event-stream，实际 %q", ct)
	}
	if buffering := w.Header().Get("X-Accel-Buffering"); buffering != "no" {
		t.Fatalf("SSE 必须显式关闭代理缓冲，实际 %q", buffering)
	}
	out := w.Body.String()
	if !strings.Contains(out, `"status":"running"`) {
		t.Fatalf("应转发 running 帧，实际 %s", out)
	}
	if !strings.Contains(out, `"status":"done"`) {
		t.Fatalf("应转发 done 帧，实际 %s", out)
	}
}

func TestSSEReplayContinuesPastHistoricalTerminal(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	taskID := "sse-resumed-task"
	ctx := context.Background()
	key := "queue:sse:" + taskID
	defer r.Raw().Del(ctx, key)
	events := []map[string]any{
		{"event": "status", "task_id": taskID, "status": "running"},
		{"event": "status", "task_id": taskID, "status": "awaiting_plan"},
		{"event": "status", "task_id": taskID, "status": "queued"},
		{"event": "status", "task_id": taskID, "status": "running"},
		{"event": "artifact_reset", "task_id": taskID, "stage": "write", "chapter_seq": 15, "artifact_id": "write-1"},
		{"event": "artifact_delta", "task_id": taskID, "stage": "write", "chapter_seq": 15, "artifact_id": "write-1", "content": "正文已在生成"},
		{"event": "artifact_complete", "task_id": taskID, "stage": "write", "chapter_seq": 15, "artifact_id": "write-1", "offset": 6},
		{"event": "status", "task_id": taskID, "status": "awaiting_review"},
	}
	for _, event := range events {
		if _, err := r.XAdd(ctx, key, event); err != nil {
			t.Fatal(err)
		}
	}

	req := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/"+taskID+"/events", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	out := w.Body.String()
	if strings.Contains(out, `"status":"awaiting_plan"`) {
		t.Fatalf("续跑后的回放不应发送历史 awaiting_plan 终态: %s", out)
	}
	if !strings.Contains(out, `"type":"artifact_delta"`) || !strings.Contains(out, "正文已在生成") {
		t.Fatalf("应越过历史终态并回放正文片段: %s", out)
	}
	if !strings.Contains(out, `"status":"awaiting_review"`) {
		t.Fatalf("应以当前最终状态结束回放: %s", out)
	}
}

func TestSSEStreamExpiredReturns410(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	// 任务终态且流已过期（键不存在）。必须在写响应头之前判定：响应头一旦 Flush 就
	// 提交了 200，此后的 410 只能落进 body，浏览器读成 eof 后无限重连 → 写按钮永久禁用。
	taskID := "sse-expired-task"
	ctx := context.Background()
	_ = r.Raw().Del(ctx, "queue:sse:"+taskID).Err()

	req := httptest.NewRequest(http.MethodGet, "/api/v1/tasks/"+taskID+"/events", nil)
	req.Header.Set("Authorization", bearer(t, "dev"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusGone {
		t.Fatalf("流已过期应 410，实际 %d body=%s", w.Code, w.Body.String())
	}
	// Content-Type 是 JSON 而非 text/event-stream，正好证明响应头未被提前提交
	if ct := w.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("410 应为 JSON（响应头未提交），实际 %q", ct)
	}
	if !strings.Contains(w.Body.String(), "sse_stream_expired") {
		t.Fatalf("应提示流过期回退快照，实际 %s", w.Body.String())
	}
}

// ---- JWT 身份断言（§14.1 ③：网关验签第一道门，替换 X-AiInk-User 占位）----

func TestAuthTokenForwardsToPython(t *testing.T) {
	// 签发端点不挂 JWT（否则无法登录）：转发 Python /internal/v1/auth/token。
	r := newTestRedis(t)
	var paths []string
	py := pyapi.New(recordingPy(&paths).URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/token",
		strings.NewReader(`{"username":"demo"}`))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if len(paths) != 1 || paths[0] != "/internal/v1/auth/token" {
		t.Fatalf("应转发 /internal/v1/auth/token，实际 %v", paths)
	}
}

func TestJWTRejectsMissingToken(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	w := httptest.NewRecorder()
	router.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil))
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("缺 token 应 401，实际 %d body=%s", w.Code, w.Body.String())
	}
}

func TestJWTRejectsBadToken(t *testing.T) {
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req.Header.Set("Authorization", "Bearer not-a-real-token")
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("坏 token 应 401，实际 %d body=%s", w.Code, w.Body.String())
	}
}

func TestJWTGoodTokenSetsTrustedHeader(t *testing.T) {
	// 验签通过 → 透传 X-AiInk-User = 可信 sub（Python 侧归属断言依赖此头，§14.1 ③）。
	r := newTestRedis(t)
	py := pyapi.New(fakePy().URL, 3*time.Second)
	router := newRouter(t, r, py)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req.Header.Set("Authorization", bearer(t, "user-123"))
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("好 token 应 200，实际 %d body=%s", w.Code, w.Body.String())
	}
	if got := w.Header().Get(HeaderUser); got != "user-123" {
		t.Fatalf("应透传 X-AiInk-User=user-123，实际 %q", got)
	}
}

func TestStaticSPAFallback(t *testing.T) {
	// 阶段 5 静态托管：/assets 命中、根/深链回退 index.html、/api 保持 JSON 404、
	// dist 外路径穿越不回退也不泄露。构造独立 router（dist 指向临时目录）。
	dist := t.TempDir()
	if err := os.WriteFile(filepath.Join(dist, "index.html"), []byte("<html>index</html>"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(dist, "assets"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dist, "assets", "app.js"), []byte("console.log(1)"), 0o644); err != nil {
		t.Fatal(err)
	}
	// dist 外放一个敏感文件，验证穿越不可达
	secret := filepath.Join(filepath.Dir(dist), "secret.txt")
	if err := os.WriteFile(secret, []byte("topsecret"), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg := config.Load()
	cfg.RatePerSec = 1000
	cfg.RateBurst = 1000
	cfg.WebDistDir = dist
	router := NewRouter(cfg, nil, nil, nil)

	cases := []struct {
		name  string
		path  string
		want  int
		body  string // 非空则断言 body 包含
		cache string
	}{
		{"root", "/", http.StatusOK, "<html>index</html>", "no-store, max-age=0"},
		{"spa deep link", "/projects/p1/audit", http.StatusOK, "<html>index</html>", "no-store, max-age=0"},
		{"asset", "/assets/app.js", http.StatusOK, "console.log(1)", "public, max-age=31536000, immutable"},
		{"api miss keeps json 404", "/api/v1/unknown", http.StatusNotFound, `"not_found"`, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, tc.path, nil)
			w := httptest.NewRecorder()
			router.ServeHTTP(w, req)
			if w.Code != tc.want {
				t.Fatalf("%s 应 %d，实际 %d body=%s", tc.path, tc.want, w.Code, w.Body.String())
			}
			if tc.body != "" && !strings.Contains(w.Body.String(), tc.body) {
				t.Fatalf("%s body 应含 %q，实际 %q", tc.path, tc.body, w.Body.String())
			}
			if got := w.Header().Get("Cache-Control"); got != tc.cache {
				t.Fatalf("%s Cache-Control 应为 %q，实际 %q", tc.path, tc.cache, got)
			}
		})
	}

	// 路径穿越：含 .. 的请求被 Go net/http 层直接 400 拒（handler 前）——安全属性是
	// dist 外 secret.txt 绝不泄露（防护是双保险，net/http 已挡第一道）。
	t.Run("traversal never leaks dist-outer file", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet,
			"/../"+filepath.Base(filepath.Dir(dist))+"/secret.txt", nil)
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if strings.Contains(w.Body.String(), "topsecret") {
			t.Fatalf("穿越请求泄露了 dist 外文件内容: %q", w.Body.String())
		}
	})
	// 反斜杠穿越（Windows 本地运行时）：net/http 只清理 / 段，\.. 会原样到 handler；
	// handler 已把 \ 归一为 / 再判段，逃出 dist 的文件绝不泄露。
	t.Run("backslash traversal never leaks dist-outer file", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet,
			"/foo%5c..%5c..%5c"+filepath.Base(filepath.Dir(dist))+"%5csecret.txt", nil)
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if strings.Contains(w.Body.String(), "topsecret") {
			t.Fatalf("反斜杠穿越请求泄露了 dist 外文件内容: %q", w.Body.String())
		}
	})
}

func TestStaticNoDistDirNoPanic(t *testing.T) {
	// dist 目录不存在 → 静态托管自动降级 404，不 panic（本地 dev 跑网关未 build 前端时）。
	cfg := config.Load()
	cfg.RatePerSec = 1000
	cfg.RateBurst = 1000
	cfg.WebDistDir = filepath.Join(t.TempDir(), "no-such-dir")
	router := NewRouter(cfg, nil, nil, nil)

	for _, path := range []string{"/", "/projects/p1", "/assets/x.js"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		w := httptest.NewRecorder()
		router.ServeHTTP(w, req)
		if w.Code != http.StatusNotFound {
			t.Fatalf("%s dist 缺失应 404，实际 %d body=%s", path, w.Code, w.Body.String())
		}
	}
}

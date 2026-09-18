// 三层闸门 + RabbitMQ 发布测试（活 Redis :6380 + RabbitMQ :5672；键/拓扑用前缀隔离）。
// 对 compose 起的 myink-rabbitmq（非 loopback guest）需 AMQP_URL=amqp://myink:myink@localhost:5672/
// 否则 dial 403 会静默 skip（skip-if-unreachable 也吃鉴权失败）。
package queue

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	amqp091 "github.com/rabbitmq/amqp091-go"

	"myink/gateway/internal/config"
	"myink/gateway/internal/redis"
)

const testQueuePrefix = "-gtest-"

func newTestRedis(t *testing.T) *redis.Client {
	t.Helper()
	r := redis.New(config.Load().RedisAddr, "")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := r.Ping(ctx); err != nil {
		t.Skipf("myink-redis 不可达: %v", err)
	}
	return r
}

func testConfig() config.Config {
	cfg := config.Load()
	cfg.QuotaDaily = 2
	cfg.ConcurrencyLimit = 1
	cfg.DailyBudget = 1.0
	cfg.CostPerChapter = 0.05
	cfg.QueuePrefix = testQueuePrefix // RabbitMQ 拓扑隔离（不碰运行中网关/worker 的真实队列）
	return cfg
}

// newTestRMQ 连 RabbitMQ（skip-if-unreachable :5672），声明 -gtest- 前缀的测试拓扑。
func newTestRMQ(t *testing.T, cfg config.Config) *AMQP {
	t.Helper()
	conn, err := amqp091.Dial(cfg.AmqpURL)
	if err != nil {
		t.Skipf("myink-rabbitmq 不可达 %s: %v", cfg.AmqpURL, err)
	}
	_ = conn.Close()
	rmq, err := DialAMQP(cfg)
	if err != nil {
		t.Fatalf("DialAMQP: %v", err)
	}
	t.Cleanup(func() { _ = rmq.Close() })
	return rmq
}

// 清理闸门键：避免测试相互污染（每用户独立键 + 每书并发键 + 全局日成本键）。
func cleanupGates(t *testing.T, r *redis.Client, uid string) {
	t.Helper()
	ctx := context.Background()
	today := time.Now().Format("2006-01-02")
	keys, _ := r.Raw().Keys(ctx, "rate:quota:"+uid+":*").Result()
	inflights, _ := r.Raw().Keys(ctx, "rate:inflight:"+uid+":*").Result()
	bookquotas, _ := r.Raw().Keys(ctx, "rate:bookquota:"+uid+":*").Result()
	bookcnts, _ := r.Raw().Keys(ctx, "rate:bookcnt:"+uid+":*").Result()
	keys = append(keys, inflights...)
	keys = append(keys, bookquotas...)
	keys = append(keys, bookcnts...)
	keys = append(keys, "rate:cost:"+today)
	if len(keys) > 0 {
		_ = r.Raw().Del(ctx, keys...).Err()
	}
}

// purgeTestQueues 清空 -gtest- 前缀的持久队列（入队残留不跨测试累积）。
func purgeTestQueues(t *testing.T, rmq *AMQP) {
	t.Helper()
	ch, err := rmq.conn.Channel()
	if err != nil {
		return
	}
	defer ch.Close()
	for _, q := range []string{MainQueue(testQueuePrefix), DelayQueue(testQueuePrefix), DlqQueue(testQueuePrefix)} {
		_, _ = ch.QueuePurge(q, false)
	}
}

// bindObserver 建独占 auto-delete 观察队列，绑定测试交换机 rk=tasks —— 之后 Enqueue 发布的
// 每条消息都被复制一份（读后即弃，测试隔离）。
func bindObserver(t *testing.T, rmq *AMQP) string {
	t.Helper()
	ch, err := rmq.conn.Channel()
	if err != nil {
		t.Fatalf("observer channel: %v", err)
	}
	q, err := ch.QueueDeclare("", false, true, true, false, nil)
	if err != nil {
		t.Fatalf("observer declare: %v", err)
	}
	if err := ch.QueueBind(q.Name, KeyTasks, rmq.exchange, false, nil); err != nil {
		t.Fatalf("observer bind: %v", err)
	}
	t.Cleanup(func() { _ = ch.Close() })
	return q.Name
}

func drainObserver(t *testing.T, rmq *AMQP, q string) []amqp091.Delivery {
	t.Helper()
	ch, err := rmq.conn.Channel()
	if err != nil {
		t.Fatalf("drain channel: %v", err)
	}
	defer ch.Close()
	var out []amqp091.Delivery
	for {
		d, ok, err := ch.Get(q, true)
		if err != nil {
			t.Fatalf("drain get: %v", err)
		}
		if !ok {
			return out
		}
		out = append(out, d)
	}
}

// costKeyToday 与 gates 侧 costKey 同规则（全局日成本键）。
func costKeyToday() string {
	return "rate:cost:" + time.Now().Format("2006-01-02")
}

func TestEnqueueOK(t *testing.T) {
	r := newTestRedis(t)
	cfg := testConfig()
	cfg.QuotaDaily = 10 // 并发用例需多次入队，配额不能先撞闸
	rmq := newTestRMQ(t, cfg)
	uid := "dev-test-user-ok"
	cleanupGates(t, r, uid)
	defer cleanupGates(t, r, uid)
	defer purgeTestQueues(t, rmq)
	obs := bindObserver(t, rmq)

	ctx := context.Background()
	res, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "chapter_generate",
		map[string]any{"seq": 1, "user_instruction": "写"}, 1, cfg.CostPerChapter, cfg.NormalPriority)
	if err != nil {
		t.Fatalf("enqueue 应成功: %v", err)
	}
	if res.TaskID == "" {
		t.Fatal("应返回 task_id")
	}
	// 入队后：配额 +1、并发占 1
	quota := r.Raw().Get(ctx, "rate:quota:"+uid+":"+time.Now().Format("2006-01-02")).Val()
	if quota != "1" {
		t.Fatalf("配额应扣 1，实际 %q", quota)
	}
	inflight, _ := r.Raw().SCard(ctx, "rate:inflight:"+uid+":proj-1").Result()
	if inflight != 1 {
		t.Fatalf("并发应占 1，实际 %d", inflight)
	}
	// BYOK 多书：异书应各自独立并发（同 uid 不同 pid 不同 key，不互斥）
	if _, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-2", "chapter_generate",
		map[string]any{"seq": 1}, 1, cfg.CostPerChapter, cfg.NormalPriority); err != nil {
		t.Fatalf("异书入队应不受同书并发闸门影响: %v", err)
	}
	inflight2, _ := r.Raw().SCard(ctx, "rate:inflight:"+uid+":proj-2").Result()
	if inflight2 != 1 {
		t.Fatalf("异书并发应各自占 1，实际 %d", inflight2)
	}
	// 同书第二次入队仍被并发闸门拒绝（同书串行）
	if _, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "chapter_generate",
		map[string]any{"seq": 2}, 1, cfg.CostPerChapter, cfg.NormalPriority); err == nil {
		t.Fatal("同书第二次入队应被并发闸门拒绝")
	} else if ge, ok := err.(*GateError); !ok || ge.Code != "CONCURRENCY_LIMIT" {
		t.Fatalf("应返回 CONCURRENCY_LIMIT，实际 %v", err)
	}
	// 观察队列应收到本次入队的两条消息（proj-1、proj-2；同书第二次被闸门拒，不发布）
	msgs := drainObserver(t, rmq, obs)
	if len(msgs) != 2 {
		t.Fatalf("应收到 2 条入队消息，实际 %d", len(msgs))
	}
	// 协议回归防护：RabbitMQ 消息体 = 任务 JSON 本身（不包 {"body":...}），worker 单次 json.loads
	last := msgs[len(msgs)-1]
	var body struct {
		TaskID   string `json:"task_id"`
		TaskType string `json:"task_type"`
		Project  string `json:"project_id"`
		User     string `json:"user_id"`
		Retry    int    `json:"retry_count"`
	}
	if err := json.Unmarshal(last.Body, &body); err != nil {
		t.Fatalf("body 应为合法 JSON: %v", err)
	}
	if body.TaskID == "" || body.TaskType != "chapter_generate" ||
		body.Project != "proj-2" || body.User == "" || body.Retry != 0 {
		t.Fatalf("body 字段应完整对齐，实际 %+v", body)
	}
}

func TestEnqueueQuotaExceeded(t *testing.T) {
	r := newTestRedis(t)
	cfg := testConfig()
	rmq := newTestRMQ(t, cfg)
	uid := "dev-test-user-quota"
	cleanupGates(t, r, uid)
	defer cleanupGates(t, r, uid)
	defer purgeTestQueues(t, rmq)

	ctx := context.Background()
	// 用满配额：先手动把 quota 键设到上限（模拟已有消耗）
	_ = r.Raw().Set(ctx, "rate:quota:"+uid+":"+time.Now().Format("2006-01-02"), "2", 0).Err()

	_, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "chapter_generate",
		map[string]any{"seq": 1}, 1, cfg.CostPerChapter, cfg.NormalPriority)
	if err == nil {
		t.Fatal("配额已满应拒绝")
	}
	ge, ok := err.(*GateError)
	if !ok || ge.Code != "QUOTA_EXCEEDED" {
		t.Fatalf("应返回 GateError QUOTA_EXCEEDED，实际 %v", err)
	}
}

func TestEnqueueBookQuotaExceeded(t *testing.T) {
	// 每书日配额（多书写书 5 本×5 章）：单书配额用满应拒绝，即使总配额未满。
	r := newTestRedis(t)
	cfg := testConfig()
	cfg.BookQuotaDaily = 3 // 单测收紧便于构造
	rmq := newTestRMQ(t, cfg)
	uid := "dev-test-user-bookq"
	cleanupGates(t, r, uid)
	defer cleanupGates(t, r, uid)
	defer purgeTestQueues(t, rmq)

	ctx := context.Background()
	// 手动把这本书的配额键设到上限（模拟已有消耗）
	_ = r.Raw().Set(ctx, "rate:bookquota:"+uid+":proj-1:"+time.Now().Format("2006-01-02"), "3", 0).Err()

	_, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "chapter_generate",
		map[string]any{"seq": 1}, 1, cfg.CostPerChapter, cfg.NormalPriority)
	if err == nil {
		t.Fatal("每书配额已满应拒绝")
	}
	ge, ok := err.(*GateError)
	if !ok || ge.Code != "BOOK_QUOTA_EXCEEDED" {
		t.Fatalf("应返回 BOOK_QUOTA_EXCEEDED，实际 %v", err)
	}
}

func TestEnqueueBookCntExceeded(t *testing.T) {
	// 每天最多 N 本不同书（多书写书：一天最多 10 本书）：书数达到上限后，第 N+1 本
	// 新书即使每本配额未满也应拒绝；已碰过的书重复写不受书数闸门影响。
	r := newTestRedis(t)
	cfg := testConfig()
	cfg.BooksPerDay = 3
	cfg.QuotaDaily = 100 // 排除总量干扰
	rmq := newTestRMQ(t, cfg)
	uid := "dev-test-user-bookcnt"
	cleanupGates(t, r, uid)
	defer cleanupGates(t, r, uid)
	defer purgeTestQueues(t, rmq)

	ctx := context.Background()
	// 前 3 本书各入队 1 章 → 全部通过（书数 1→3）
	for i := 1; i <= 3; i++ {
		pid := fmt.Sprintf("proj-bc-%d", i)
		if _, err := Enqueue(ctx, r, rmq, cfg, uid, pid, "chapter_generate",
			map[string]any{"seq": 1}, 1, cfg.CostPerChapter, cfg.NormalPriority); err != nil {
			t.Fatalf("第 %d 本书应通过: %v", i, err)
		}
	}
	// 第 4 本新书 → BOOK_CNT_EXCEEDED
	if _, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-bc-4", "chapter_generate",
		map[string]any{"seq": 1}, 1, cfg.CostPerChapter, cfg.NormalPriority); err == nil {
		t.Fatal("第 4 本新书应被书数闸门拒绝")
	} else if ge, ok := err.(*GateError); !ok || ge.Code != "BOOK_CNT_EXCEEDED" {
		t.Fatalf("应返回 BOOK_CNT_EXCEEDED，实际 %v", err)
	}
	// 已碰过的书重复入队 → 仍应通过（书数去重，不累加）。
	// 模拟 worker 终态 SREM 释放 proj-bc-1 的并发占位（否则同书第二任务被并发闸门挡，
	// 那不是书数闸门语义），再对同一本书重复入队 → 书数去重应放行。
	members, _ := r.Raw().SMembers(ctx, "rate:inflight:"+uid+":proj-bc-1").Result()
	if len(members) > 0 {
		args := make([]interface{}, len(members))
		for i, m := range members {
			args[i] = m
		}
		_ = r.Raw().SRem(ctx, "rate:inflight:"+uid+":proj-bc-1", args...).Err()
	}
	if _, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-bc-1", "chapter_generate",
		map[string]any{"seq": 2}, 1, cfg.CostPerChapter, cfg.NormalPriority); err != nil {
		t.Fatalf("已碰过的书重复写应通过: %v", err)
	}
}

func TestEnqueueConcurrencyLimit(t *testing.T) {
	r := newTestRedis(t)
	cfg := testConfig()
	rmq := newTestRMQ(t, cfg)
	uid := "dev-test-user-conc"
	cleanupGates(t, r, uid)
	defer cleanupGates(t, r, uid)
	defer purgeTestQueues(t, rmq)

	ctx := context.Background()
	// 手动占用本书并发闸门（模拟已有 running 任务）
	_ = r.Raw().SAdd(ctx, "rate:inflight:"+uid+":proj-1", "existing-task").Err()

	_, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "chapter_generate",
		map[string]any{"seq": 1}, 1, cfg.CostPerChapter, cfg.NormalPriority)
	if err == nil {
		t.Fatal("并发闸门被占应拒绝")
	}
	ge, ok := err.(*GateError)
	if !ok || ge.Code != "CONCURRENCY_LIMIT" {
		t.Fatalf("应返回 CONCURRENCY_LIMIT，实际 %v", err)
	}
}

func TestEnqueueDailyBudgetExceeded(t *testing.T) {
	r := newTestRedis(t)
	cfg := testConfig()
	rmq := newTestRMQ(t, cfg)
	uid := "dev-test-user-budget"
	cleanupGates(t, r, uid)
	defer cleanupGates(t, r, uid)
	defer purgeTestQueues(t, rmq)

	ctx := context.Background()
	// 成本键已用满（日预算 1.0）
	_ = r.Raw().Set(ctx, costKeyToday(), "1.0", 0).Err()

	_, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "batch_generate",
		map[string]any{"size": 2, "start": 1}, 2, cfg.CostPerChapter*2, cfg.NormalPriority)
	if err == nil {
		t.Fatal("日成本超限应拒绝")
	}
	ge, ok := err.(*GateError)
	if !ok || ge.Code != "DAILY_BUDGET_EXCEEDED" {
		t.Fatalf("应返回 DAILY_BUDGET_EXCEEDED，实际 %v", err)
	}
}

func TestEnqueueBatchDeductN(t *testing.T) {
	r := newTestRedis(t)
	cfg := testConfig()
	cfg.QuotaDaily = 10 // 批次 size=3 要能通过配额
	rmq := newTestRMQ(t, cfg)
	uid := "dev-test-user-batch"
	cleanupGates(t, r, uid)
	defer cleanupGates(t, r, uid)
	defer purgeTestQueues(t, rmq)

	ctx := context.Background()
	_, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "batch_generate",
		map[string]any{"size": 3, "start": 1}, 3, cfg.CostPerChapter*3, cfg.NormalPriority)
	if err != nil {
		t.Fatalf("批次入队应成功: %v", err)
	}
	quota := r.Raw().Get(ctx, "rate:quota:"+uid+":"+time.Now().Format("2006-01-02")).Val()
	if quota != "3" {
		t.Fatalf("批次应一次性扣 N=3，实际 %q", quota)
	}
}

func TestEnqueuePublishPriority(t *testing.T) {
	// VIP 优先：priority 参数透传到 RabbitMQ 消息属性（主队列 x-max-priority=10）。
	r := newTestRedis(t)
	cfg := testConfig()
	cfg.QuotaDaily = 10 // VIP + normal 各入队一次
	rmq := newTestRMQ(t, cfg)
	uid := "dev-test-user-pri"
	cleanupGates(t, r, uid)
	defer cleanupGates(t, r, uid)
	defer purgeTestQueues(t, rmq)
	obs := bindObserver(t, rmq)

	ctx := context.Background()
	// VIP（priority=9）
	if _, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "chapter_generate",
		map[string]any{"seq": 1}, 1, cfg.CostPerChapter, cfg.VIPPriority); err != nil {
		t.Fatalf("VIP 入队应成功: %v", err)
	}
	// 释放同书并发占位再入 normal（priority=0）
	members, _ := r.Raw().SMembers(ctx, "rate:inflight:"+uid+":proj-1").Result()
	if len(members) > 0 {
		args := make([]interface{}, len(members))
		for i, m := range members {
			args[i] = m
		}
		_ = r.Raw().SRem(ctx, "rate:inflight:"+uid+":proj-1", args...).Err()
	}
	if _, err := Enqueue(ctx, r, rmq, cfg, uid, "proj-1", "chapter_generate",
		map[string]any{"seq": 2}, 1, cfg.CostPerChapter, cfg.NormalPriority); err != nil {
		t.Fatalf("normal 入队应成功: %v", err)
	}
	msgs := drainObserver(t, rmq, obs)
	if len(msgs) != 2 {
		t.Fatalf("应收到 2 条消息，实际 %d", len(msgs))
	}
	got := map[uint8]bool{}
	for _, m := range msgs {
		got[m.Priority] = true
	}
	if !got[uint8(cfg.VIPPriority)] || !got[uint8(cfg.NormalPriority)] {
		t.Fatalf("消息应同时含 VIP priority=%d 与 normal priority=%d，实际 %+v",
			cfg.VIPPriority, cfg.NormalPriority, msgs)
	}
}

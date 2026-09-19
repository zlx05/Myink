// Package queue 任务入队：三层闸门（§13，gates.lua）+ RabbitMQ 发布（publisher confirm，失败补偿）。
package queue

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"

	"myink/gateway/internal/config"
	"myink/gateway/internal/redis"
)

//go:embed gates.lua
var gatesScript string

//go:embed compensate.lua
var compensateScript string

type EnqueueResult struct {
	TaskID  string `json:"task_id"`
	TraceID string `json:"trace_id"`
}

type GateError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *GateError) Error() string { return e.Message }

// Enqueue 三层闸门（gates.lua，消灭 TOCTOU）+ RabbitMQ 发布（publisher confirm）。
// userID/projectID/taskType/payload/quotaDeductN/costEst 语义同前（taskType: chapter_generate|batch_generate）；
// priority 为 RabbitMQ 消息优先级（VIP=9 / normal=0，主队列 x-max-priority=10）。
// 失败语义：
//   - GateError：闸门拒绝（配额/并发/成本超限），无副作用
//   - 发布确定失败（发送前错误 / broker nack）：跑 compensate.lua 回滚闸门副作用
//   - 发布结果不确定（confirm 超时）：不补偿（消息可能已入队，worker 幂等兜底），按失败返回
func Enqueue(ctx context.Context, r *redis.Client, rmq *AMQP, cfg config.Config,
	userID, projectID, taskType string, payload map[string]any, quotaDeductN int, costEst float64, priority int) (*EnqueueResult, error) {

	now := time.Now()
	taskID := uuid.NewString()
	quotaKey := fmt.Sprintf("rate:quota:%s:%s", userID, now.Format("2006-01-02"))
	costKey := fmt.Sprintf("rate:cost:%s", now.Format("2006-01-02"))
	inflightKey := fmt.Sprintf("rate:inflight:%s:%s", userID, projectID)
	bookQuotaKey := fmt.Sprintf("rate:bookquota:%s:%s:%s", userID, projectID, now.Format("2006-01-02"))
	bookCntKey := fmt.Sprintf("rate:bookcnt:%s:%s", userID, now.Format("2006-01-02"))

	// 消息体 = 任务 JSON 本身（协议不变量：worker 直接 json.loads，不再包 {"body":...}）。
	bodyJSON, err := json.Marshal(map[string]any{
		"task_id":     taskID,
		"task_type":   taskType,
		"project_id":  projectID,
		"user_id":     userID,
		"payload":     payload,
		"trace_id":    taskID,
		"request_id":  taskID,
		"retry_count": 0,
		"created_at":  now.UnixMilli(),
	})
	if err != nil {
		return nil, err
	}

	// 三层闸门：KEYS 布局与 gates.lua 头注释一致（入队职责已迁 RabbitMQ，无 StreamTasks）
	res, err := r.Eval(ctx, gatesScript,
		[]string{quotaKey, inflightKey, costKey, bookQuotaKey, bookCntKey},
		taskID,
		fmt.Sprint(quotaDeductN), fmt.Sprint(costEst),
		fmt.Sprint(cfg.QuotaDaily), fmt.Sprint(cfg.DailyBudget), fmt.Sprint(cfg.BookQuotaDaily),
		fmt.Sprint(cfg.BooksPerDay), projectID)
	if err != nil {
		return nil, err
	}
	// Lua 返回 []any{int64 code, string reason, ...}
	arr, ok := res.([]any)
	if !ok || len(arr) == 0 {
		return nil, fmt.Errorf("gates.lua 异常返回: %v", res)
	}
	code := arr[0].(int64)
	if code != 1 {
		reason, _ := arr[1].(string)
		return nil, &GateError{Code: reason, Message: reason}
	}

	// Persist trusted ownership before publishing; SSE may connect before DB materialization.
	ownerJSON, _ := json.Marshal(map[string]string{"user_id": userID, "project_id": projectID})
	if err := r.Raw().Set(ctx, "queue:task-owner:"+taskID, string(ownerJSON), 24*time.Hour).Err(); err != nil {
		_, _ = r.Eval(ctx, compensateScript, []string{quotaKey, bookQuotaKey, inflightKey}, fmt.Sprint(quotaDeductN), taskID)
		return nil, err
	}
	// RabbitMQ 发布（publisher confirm）
	if err := rmq.PublishTask(ctx, bodyJSON, priority); err != nil {
		if errors.Is(err, ErrPublishAmbiguous) {
			// 结果不确定：不补偿（消息可能已入队，worker 幂等兜底）
			return nil, fmt.Errorf("enqueue: %w", err)
		}
		// 确定失败：回滚闸门副作用（配额 + 并发占位；bookcnt 不反悔，罕见多计可接受）
		if _, cerr := r.Eval(ctx, compensateScript,
			[]string{quotaKey, bookQuotaKey, inflightKey},
			fmt.Sprint(quotaDeductN), taskID); cerr != nil {
			return nil, fmt.Errorf("enqueue: publish: %w (compensate: %v)", err, cerr)
		}
		return nil, fmt.Errorf("enqueue: publish: %w", err)
	}
	return &EnqueueResult{TaskID: taskID, TraceID: taskID}, nil
}

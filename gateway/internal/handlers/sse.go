// 网关 SSE 路由：浏览器订阅 queue:sse:{task_id}，断线 Last-Event-ID 重放。
package handlers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"

	"myink/gateway/internal/pyapi"
	"myink/gateway/internal/redis"
	"myink/gateway/internal/sse"
)

// 硬超时兜底：防止终态事件缺失时连接挂死（正常由 done/failed 事件收尾）
const sseMaxDuration = 30 * time.Minute

// errTerminal 内部哨兵：SSE 收到终态帧后停止订阅
var errTerminal = errors.New("terminal_event")

type SSEHandler struct {
	r  *redis.Client
	py *pyapi.Client
}

func NewSSEHandler(r *redis.Client, py *pyapi.Client) *SSEHandler { return &SSEHandler{r: r, py: py} }

// Stream 订阅任务进度事件并转发 SSE 帧。
// GET /api/v1/tasks/:task_id/events?last_event_id=xxx
func (h *SSEHandler) Stream(c *gin.Context) {
	taskID := c.Param("task_id")
	if !checkAccess(c, h.py, "tasks", taskID) {
		return
	}
	after := c.Query("last_event_id")
	if after == "" {
		after = c.GetHeader("Last-Event-ID")
	}
	after = sse.ParseLastEventID(after)

	// 先探测通道再决定响应。响应头一旦 Flush 即提交 200，此后的 410 只会落进 body
	// （浏览器读成 eof → 无限重连 → 写按钮永久禁用）。预检自身出错（Redis 抖动）
	// 不阻断，照常走 Subscribe，避免把抖动谎报成「流已过期」。
	if exists, err := sse.StreamExists(c.Request.Context(), h.r, taskID); err == nil && !exists {
		c.AbortWithStatusJSON(http.StatusGone, gin.H{"error": "sse_stream_expired", "hint": "fallback_get_snapshot"})
		return
	}

	// 强制响应头：SSE 必须 text/event-stream + 关缓冲
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-store")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")
	c.Writer.Flush()

	deadline := time.Now().Add(sseMaxDuration)
	if value, exists := c.Get("auth_expires"); exists {
		if expires, ok := value.(time.Time); ok && expires.Before(deadline) {
			deadline = expires
		}
	}
	ctx, cancel := context.WithDeadline(c.Request.Context(), deadline)
	authorization, userID := c.GetHeader("Authorization"), GetUserID(c)

	// ResponseWriter 不允许并发写。心跳和模型片段共用同一把锁，避免生成超过 15 秒时
	// 两个 goroutine 交叉写坏 SSE 帧，造成浏览器只能等重连后一次性看到正文。
	var writeMu sync.Mutex
	writeFrame := func(frame string) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		if ctx.Err() != nil {
			return ctx.Err()
		}
		_, err := fmt.Fprint(c.Writer, frame)
		c.Writer.Flush()
		return err
	}

	// 15s 心跳注释帧保中间代理连接（终态帧触发收尾前一直发）
	ticker := time.NewTicker(15 * time.Second)
	heartbeatDone := make(chan struct{})
	go func() {
		defer close(heartbeatDone)
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if !h.validSession(ctx, authorization, userID) {
					cancel()
					return
				}
				_ = writeFrame(": keepalive\n\n")
			}
		}
	}()
	defer func() {
		ticker.Stop()
		cancel()
		<-heartbeatDone
	}()

	err := sse.Subscribe(ctx, h.r, taskID, after, func(ev sse.Event) error {
		werr := writeFrame(sse.WriteEvent(ev))
		if isTerminal(ev) {
			return errTerminal // 终态帧 → 停止订阅收尾
		}
		return werr
	})
	switch {
	case ctx.Err() != nil:
		return // No JSON error frames after SSE headers have been committed.
	case err == nil || errors.Is(err, errTerminal):
		// 正常结束（终态帧已发 / 客户端断开）
		return
	case errors.Is(err, sse.ErrStreamGone):
		// 通道不存在：任务终态且流已过期 → 客户端回退 GET 快照
		c.AbortWithStatusJSON(http.StatusGone, gin.H{"error": "sse_stream_expired", "hint": "fallback_get_snapshot"})
	default:
		c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{"error": "sse_read_failed"})
	}
}

func (h *SSEHandler) validSession(ctx context.Context, authorization, userID string) bool {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	header := http.Header{"Authorization": []string{authorization}}
	resp, err := h.py.Forward(ctx, http.MethodGet, "/internal/v1/auth/session", nil, header, nil)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	var session struct {
		UserID string `json:"user_id"`
	}
	return resp.StatusCode == 200 && json.NewDecoder(io.LimitReader(resp.Body, 65536)).Decode(&session) == nil && session.UserID == userID
}

// isTerminal 终态判定：worker 终态事件是 event=status + status∈{done,failed,awaiting_review}。
func isTerminal(ev sse.Event) bool {
	switch ev.Status {
	case "done", "failed", "awaiting_plan", "awaiting_review", "cancelled":
		return true
	}
	return false
}

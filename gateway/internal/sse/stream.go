// Package sse 网关侧 SSE 转发：把 worker 写入 Redis Stream 的进度帧转发给浏览器。
// 断线重连支持 Last-Event-ID 重放（XRange 追平），流被 MAXLEN/TTL 裁剪则客户端回退 GET 快照。
package sse

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	goredis "github.com/redis/go-redis/v9"

	"myink/gateway/internal/redis"
)

const (
	// StreamPrefix SSE 通道 key 前缀（§14：queue:）
	StreamPrefix = "queue:sse:"
	// MaxLen 每条 SSE 通道最大帧数（worker 侧 MAXLEN 一致）
	MaxLen = 1000
	// TTL 通道存活时间（任务终态后 1h 内可重放）
	TTL = 3600 * time.Second
)

// StreamKey 返回指定任务的 SSE 通道 key。
func StreamKey(taskID string) string { return StreamPrefix + taskID }

// StreamExists 判断任务的 SSE 通道当前是否存在。网关必须在写任何响应头之前预检：
// 响应头一旦 Flush 就提交了 200，此后的 410 只能落进 body（浏览器读成 eof 后无限重连）。
func StreamExists(ctx context.Context, r *redis.Client, taskID string) (bool, error) {
	n, err := r.Raw().Exists(ctx, StreamKey(taskID)).Result()
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

// Event 是 SSE 帧的载体（对应 worker 侧 XADD 的 fields）。
type Event struct {
	ID         string `json:"id"`
	Type       string `json:"type"` // status | node | heartbeat | done
	TaskID     string `json:"task_id,omitempty"`
	Node       string `json:"node,omitempty"`
	Status     string `json:"status,omitempty"`
	Message    string `json:"message,omitempty"`
	Stage      string `json:"stage,omitempty"`
	Chapter    int    `json:"chapter_seq,omitempty"`
	Attempt    int    `json:"attempt,omitempty"`
	Offset     int    `json:"offset,omitempty"`
	ArtifactID string `json:"artifact_id,omitempty"`
	Content    string `json:"content,omitempty"`
	Artifact   string `json:"artifact,omitempty"`
}

// ErrStreamGone 通道不存在（任务终态且流已过期）→ 客户端应回退 GET 快照。
var ErrStreamGone = fmt.Errorf("sse 通道不存在")

// Replay 通过 ctx 逐帧转发通道中 afterID 之后的已有帧，返回最后已读取的流 ID
// （供 Subscribe 的 XREAD 从该 ID 之后继续，避免已有帧被重复读取）。同一 task_id
// 会跨 awaiting_plan / awaiting_review 多次续跑；历史终态后已有新事件时必须跳过该
// 历史终态，否则浏览器会在 Plan 或首次评审处提前关流，看不到后续正文。
// 返回 ErrStreamGone 表示通道已不存在。
func Replay(ctx context.Context, r *redis.Client, taskID, afterID string, send func(Event) error) (string, error) {
	key := StreamKey(taskID)
	exists, err := StreamExists(ctx, r, taskID)
	if err != nil {
		return afterID, err
	}
	if !exists {
		return afterID, ErrStreamGone
	}
	// XRANGE 起点默认包含自身；用 '(' 做严格大于，避免重连时重复上一帧。
	msgs, err := r.Raw().XRange(ctx, key, "("+afterID, "+").Result()
	if err != nil {
		return afterID, err
	}
	type decodedMessage struct {
		id string
		ev Event
	}
	decoded := make([]decodedMessage, 0, len(msgs))
	for _, m := range msgs {
		ev, ok := decodeEvent(m)
		if ok {
			decoded = append(decoded, decodedMessage{id: m.ID, ev: ev})
		}
	}
	last := afterID
	for i, item := range decoded {
		last = item.id
		if terminalStatus(item.ev.Status) && i < len(decoded)-1 {
			continue
		}
		if err := send(item.ev); err != nil {
			return last, err
		}
	}
	return last, nil
}

func terminalStatus(status string) bool {
	switch status {
	case "done", "failed", "awaiting_plan", "awaiting_review", "cancelled":
		return true
	}
	return false
}

// Subscribe 阻塞式把通道事件持续转发给 send，直到 ctx 取消或 send 返回错误。
// afterID 为断线重放起点（Last-Event-ID），0-0 表示从头。
func Subscribe(ctx context.Context, r *redis.Client, taskID, afterID string, send func(Event) error) error {
	key := StreamKey(taskID)
	lastID := afterID
	// 追平已有帧（断线 Last-Event-ID 重放）；XREAD 从最后已转发帧之后继续，防重复
	replayed, err := Replay(ctx, r, taskID, lastID, send)
	if err != nil {
		return err
	}
	lastID = replayed
	// XREAD 阻塞等待新帧（空转 5s 无帧则检查 ctx 后继续）
	for {
		res, err := r.Raw().XRead(ctx, &goredis.XReadArgs{
			Streams: []string{key, lastID},
			Count:   100,
			Block:   5 * time.Second,
		}).Result()
		if err != nil {
			if err == goredis.Nil {
				select {
				case <-ctx.Done():
					return nil
				default:
					continue
				}
			}
			return err
		}
		for _, s := range res {
			for _, m := range s.Messages {
				ev, ok := decodeEvent(m)
				if !ok {
					continue
				}
				if err := send(ev); err != nil {
					return err
				}
				lastID = m.ID
			}
		}
		select {
		case <-ctx.Done():
			return nil
		default:
		}
	}
}

// decodeEvent 把 Redis Stream 消息解码成 Event。字段与 worker 侧 XADD 扁平结构对齐：
// event=status|node，status=queued/running/done/...，node=节点名（status 事件时为空）。
// ID 用 Redis 流 ID 保证单调递增（断线重放一致）。
func decodeEvent(m goredis.XMessage) (Event, bool) {
	var ev Event
	typ, ok := m.Values["event"].(string)
	if !ok || typ == "" {
		return ev, false
	}
	ev.Type = typ
	ev.ID = m.ID
	ev.TaskID, _ = m.Values["task_id"].(string)
	ev.Node, _ = m.Values["node"].(string)
	ev.Status, _ = m.Values["status"].(string)
	ev.Message, _ = m.Values["message"].(string)
	ev.Stage, _ = m.Values["stage"].(string)
	ev.ArtifactID, _ = m.Values["artifact_id"].(string)
	ev.Content, _ = m.Values["content"].(string)
	ev.Artifact, _ = m.Values["artifact"].(string)
	ev.Chapter = intField(m.Values["chapter_seq"])
	ev.Attempt = intField(m.Values["attempt"])
	ev.Offset = intField(m.Values["offset"])
	return ev, true
}

func intField(value any) int {
	switch v := value.(type) {
	case string:
		n, _ := strconv.Atoi(v)
		return n
	case int:
		return v
	case int64:
		return int(v)
	}
	return 0
}

// WriteEvent 把 Event 序列化为 SSE 帧文本（data: + 可选 event: 行）。
func WriteEvent(ev Event) string {
	data, _ := json.Marshal(map[string]any{
		"type":        ev.Type,
		"task_id":     ev.TaskID,
		"node":        ev.Node,
		"status":      ev.Status,
		"message":     ev.Message,
		"stage":       ev.Stage,
		"chapter_seq": ev.Chapter,
		"attempt":     ev.Attempt,
		"offset":      ev.Offset,
		"artifact_id": ev.ArtifactID,
		"content":     ev.Content,
		"artifact":    ev.Artifact,
	})
	var b []byte
	if ev.Type != "" && ev.Type != "message" {
		b = append(b, []byte("event: "+ev.Type+"\n")...)
	}
	b = append(b, []byte("id: "+ev.ID+"\n")...)
	b = append(b, []byte("data: "+string(data)+"\n\n")...)
	return string(b)
}

// ParseLastEventID 解析 Last-Event-ID 头（Redis 流 ID 格式如 "1700000000000-0"）。
func ParseLastEventID(h string) string {
	if h == "" {
		return "0-0"
	}
	parts := strings.SplitN(h, "-", 2)
	if len(parts) != 2 {
		return "0-0"
	}
	if _, err := strconv.ParseInt(parts[0], 10, 64); err != nil {
		return "0-0"
	}
	seq, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil || seq < 0 {
		seq = 0
	}
	return fmt.Sprintf("%s-%d", parts[0], seq)
}

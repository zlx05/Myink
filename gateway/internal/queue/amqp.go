// RabbitMQ 发布端：连接 + 幂等声明拓扑 + publisher confirm 发布。
// 与 Python 侧 worker/amqp.py、API 发布端用同一套拓扑名（前缀仅测试隔离用）。
package queue

import (
	"context"
	"errors"
	"fmt"
	"sync"

	amqp091 "github.com/rabbitmq/amqp091-go"

	"myink/gateway/internal/config"
)

// RabbitMQ 拓扑名（三端声明必须完全一致，参数不一致会 PRECONDITION_FAILED）。
const (
	ExchangeTasks = "myink.tasks"
	ExchangeDlx   = "myink.dlx"
	KeyTasks      = "tasks" // 主队列路由键
	KeyDelay      = "delay" // 延迟队列路由键
	KeyDlq        = "dlq"   // 死信路由键
)

// MainQueue / DelayQueue / DlqQueue 返回带前缀的队列名。QUEUE_PREFIX 只用于测试隔离，
// 生产为空串（exchange/queue 名与 Python 端一致）。
func MainQueue(prefix string) string  { return "queue:tasks" + prefix }
func DelayQueue(prefix string) string { return "queue:delay" + prefix }
func DlqQueue(prefix string) string   { return "queue:dlq" + prefix }

// ErrPublishAmbiguous 发布结果不确定（confirm 超时/连接中断）：消息可能已入队，不能补偿
// （worker 幂等兜底），但按失败返回给调用方。
var ErrPublishAmbiguous = fmt.Errorf("publish ambiguous")

// AMQP RabbitMQ 发布端。单 channel + mutex 串行发布（单用户项目量级远不需多 channel），
// channel 开 confirm 模式，每次发布等 broker 确认。TCP 被对端掐掉后 Publish 失败会重拨一次。
type AMQP struct {
	conn       *amqp091.Connection
	ch         *amqp091.Channel
	exchange   string
	routingKey string
	url        string
	prefix     string
	mu         sync.Mutex
}

// DialAMQP 连接 RabbitMQ 并幂等声明 5 项拓扑：
//
//	myink.tasks (direct)
//	  ├─ key "tasks" → queue:tasks  (x-max-priority=10, DLX→myink.dlx/dlq)
//	  └─ key "delay" → queue:delay  (DLX→myink.tasks/tasks；无固定 TTL，逐条 expiration 退避)
//	myink.dlx (direct) → key "dlq" → queue:dlq
//
// 失败返回 error（连接/声明/confirm 任一失败）。
func DialAMQP(cfg config.Config) (*AMQP, error) {
	a := &AMQP{
		exchange:   ExchangeTasks + cfg.QueuePrefix,
		routingKey: KeyTasks,
		url:        cfg.AmqpURL,
		prefix:     cfg.QueuePrefix,
	}
	if err := a.connect(); err != nil {
		return nil, err
	}
	return a, nil
}

func (a *AMQP) connect() error {
	if a.ch != nil {
		_ = a.ch.Close()
		a.ch = nil
	}
	if a.conn != nil {
		_ = a.conn.Close()
		a.conn = nil
	}
	conn, err := amqp091.Dial(a.url)
	if err != nil {
		return fmt.Errorf("amqp dial: %w", err)
	}
	ch, err := conn.Channel()
	if err != nil {
		_ = conn.Close()
		return fmt.Errorf("amqp channel: %w", err)
	}
	if err := ch.Confirm(false); err != nil {
		_ = ch.Close()
		_ = conn.Close()
		return fmt.Errorf("amqp confirm: %w", err)
	}
	a.conn = conn
	a.ch = ch
	if err := a.declare(a.prefix); err != nil {
		_ = a.Close()
		return err
	}
	return nil
}

// declare 幂等声明 5 项拓扑（重复声明安全；参数变更需清 RabbitMQ 数据卷）。
func (a *AMQP) declare(prefix string) error {
	exTasks := ExchangeTasks + prefix
	exDlx := ExchangeDlx + prefix
	mainQ, delayQ, dlqQ := MainQueue(prefix), DelayQueue(prefix), DlqQueue(prefix)

	if err := a.ch.ExchangeDeclare(exTasks, "direct", true, false, false, false, nil); err != nil {
		return fmt.Errorf("exchange %s: %w", exTasks, err)
	}
	// 主队列：优先级 + 死信 → myink.dlx/dlq
	if _, err := a.ch.QueueDeclare(mainQ, true, false, false, false, amqp091.Table{
		"x-max-priority":            int32(10),
		"x-dead-letter-exchange":    exDlx,
		"x-dead-letter-routing-key": KeyDlq,
	}); err != nil {
		return fmt.Errorf("queue %s: %w", mainQ, err)
	}
	if err := a.ch.QueueBind(mainQ, KeyTasks, exTasks, false, nil); err != nil {
		return fmt.Errorf("bind %s: %w", mainQ, err)
	}
	// 延迟队列：无固定 TTL，消息逐条带 expiration 死信回主队列实现退避（零插件）
	if _, err := a.ch.QueueDeclare(delayQ, true, false, false, false, amqp091.Table{
		"x-dead-letter-exchange":    exTasks,
		"x-dead-letter-routing-key": KeyTasks,
	}); err != nil {
		return fmt.Errorf("queue %s: %w", delayQ, err)
	}
	if err := a.ch.QueueBind(delayQ, KeyDelay, exTasks, false, nil); err != nil {
		return fmt.Errorf("bind %s: %w", delayQ, err)
	}
	// 死信交换机 + 死信队列
	if err := a.ch.ExchangeDeclare(exDlx, "direct", true, false, false, false, nil); err != nil {
		return fmt.Errorf("exchange %s: %w", exDlx, err)
	}
	if _, err := a.ch.QueueDeclare(dlqQ, true, false, false, false, nil); err != nil {
		return fmt.Errorf("queue %s: %w", dlqQ, err)
	}
	if err := a.ch.QueueBind(dlqQ, KeyDlq, exDlx, false, nil); err != nil {
		return fmt.Errorf("bind %s: %w", dlqQ, err)
	}
	return nil
}

// PublishTask 发布任务到主队列（持久化 + 优先级）。返回语义：
//   - nil：broker 已确认
//   - ErrPublishAmbiguous：confirm 超时/连接中断，结果不确定（消息可能已入队）
//   - 其他：确定失败（发送前错误 / broker nack），可安全补偿
func (a *AMQP) PublishTask(ctx context.Context, body []byte, priority int) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	err := a.publishLocked(ctx, body, priority)
	if err == nil || errors.Is(err, ErrPublishAmbiguous) {
		return err
	}
	if rerr := a.connect(); rerr != nil {
		return fmt.Errorf("amqp reconnect: %w (publish: %v)", rerr, err)
	}
	return a.publishLocked(ctx, body, priority)
}

func (a *AMQP) publishLocked(ctx context.Context, body []byte, priority int) error {
	if a.ch == nil {
		return fmt.Errorf("amqp publish: channel closed")
	}
	dcf, err := a.ch.PublishWithDeferredConfirmWithContext(ctx, a.exchange, a.routingKey, false, false,
		amqp091.Publishing{
			ContentType:  "application/json",
			DeliveryMode: amqp091.Persistent,
			Priority:     uint8(priority),
			Body:         body,
		})
	if err != nil {
		return fmt.Errorf("amqp publish: %w", err)
	}
	ok, err := dcf.WaitContext(ctx)
	if err != nil {
		return ErrPublishAmbiguous
	}
	if !ok {
		return fmt.Errorf("amqp publish: broker nack")
	}
	return nil
}

// Close 关闭连接（通道随连接一起释放）。
func (a *AMQP) Close() error {
	if a.ch != nil {
		_ = a.ch.Close()
	}
	if a.conn != nil {
		return a.conn.Close()
	}
	return nil
}

"""RabbitMQ 连接与拓扑（worker 消费 + API/worker 发布共用）。

拓扑与 Go 网关 `gateway/internal/queue/amqp.go` 完全一致（producer/consumer/API 三端
幂等声明；参数不一致会 PRECONDITION_FAILED）：

    myink.tasks (direct)
      ├─ key "tasks" → queue:tasks  (x-max-priority=10, DLX→myink.dlx/dlq)
      └─ key "delay" → queue:delay  (DLX→myink.tasks/tasks；无固定 TTL，逐条 expiration 退避)
    myink.dlx (direct) → key "dlq" → queue:dlq

协议不变量（最高风险）：消息体 = 任务 JSON 本身（Go 端发布即任务字典，worker 单次
json.loads，不再包 {"body":...}）。

QUEUE_PREFIX 只用于测试隔离：对 exchange 与 queue 名统一加前缀（生产空串）。
"""

from __future__ import annotations

import threading

import pika

from myink.config import settings

EXCHANGE_TASKS = "myink.tasks"
EXCHANGE_DLX = "myink.dlx"
KEY_TASKS = "tasks"
KEY_DELAY = "delay"
KEY_DLQ = "dlq"


def _px() -> str:
    return settings.queue_prefix


def exchange() -> str:
    return EXCHANGE_TASKS + _px()


def dlx_exchange() -> str:
    return EXCHANGE_DLX + _px()


def main_queue() -> str:
    return "queue:tasks" + _px()


def delay_queue() -> str:
    return "queue:delay" + _px()


def dlq_queue() -> str:
    return "queue:dlq" + _px()


def connect() -> pika.BlockingConnection:
    """worker 消费连接：heartbeat=1800（批次任务可超 30 分钟默认看门狗，防通道被服务端杀掉）。

    返回 BlockingConnection（worker 单线程消费，非 asyncio 场景专用）。
    heartbeat/blocked_connection_timeout 是连接参数（非 URL 参数），URLParameters 只收 url。
    """
    params = pika.URLParameters(settings.amqp_url)
    params.heartbeat = 1800
    params.blocked_connection_timeout = None
    return pika.BlockingConnection(params)


def declare_topology(ch: pika.channel.Channel) -> None:
    """幂等声明 5 项拓扑（重复声明安全；参数变更需清 RabbitMQ 数据卷）。"""
    ch.exchange_declare(exchange(), exchange_type="direct", durable=True)
    # 主队列：优先级 + 死信 → myink.dlx/dlq
    ch.queue_declare(
        main_queue(),
        durable=True,
        arguments={
            "x-max-priority": 10,
            "x-dead-letter-exchange": dlx_exchange(),
            "x-dead-letter-routing-key": KEY_DLQ,
        },
    )
    ch.queue_bind(main_queue(), exchange(), KEY_TASKS)
    # 延迟队列：无固定 TTL，消息逐条带 expiration 死信回主队列实现退避（零插件）
    ch.queue_declare(
        delay_queue(),
        durable=True,
        arguments={
            "x-dead-letter-exchange": exchange(),
            "x-dead-letter-routing-key": KEY_TASKS,
        },
    )
    ch.queue_bind(delay_queue(), exchange(), KEY_DELAY)
    # 死信交换机 + 死信队列
    ch.exchange_declare(dlx_exchange(), exchange_type="direct", durable=True)
    ch.queue_declare(dlq_queue(), durable=True)
    ch.queue_bind(dlq_queue(), dlx_exchange(), KEY_DLQ)


# 模块级发布连接（publisher confirm）：API 路由线程池 / worker 重投共用，锁串行。
_publish_lock = threading.Lock()
_publish_conn: pika.BlockingConnection | None = None


def publish(body: str, routing_key: str, priority: int = 0, expiration_ms: int | None = None) -> None:
    """发布到 myink.tasks 交换机（持久化 + 优先级 + publisher confirm）。

    routing_key: KEY_TASKS（主队列）/ KEY_DELAY（延迟退避）。expiration_ms 非空时设置
    逐条 TTL（毫秒）——延迟队列 DLX 回主队列实现退避。
    阻塞式 confirm：broker nack / 连接断开抛 pika 异常；连接失效自动重建。
    """
    global _publish_conn
    with _publish_lock:
        try:
            if _publish_conn is None or _publish_conn.is_closed:
                _publish_conn = connect()
            ch = _publish_conn.channel()
            ch.confirm_delivery()  # 阻塞式 publisher confirm
            properties = pika.BasicProperties(delivery_mode=2, priority=priority)
            if expiration_ms is not None:
                properties.expiration = str(int(expiration_ms))
            ch.basic_publish(exchange=exchange(), routing_key=routing_key, body=body, properties=properties)
        except pika.exceptions.AMQPError:
            # 连接/通道失效：重建连接重试一次；仍失败抛给调用方（API 侧 503）
            try:
                if _publish_conn is not None and not _publish_conn.is_closed:
                    _publish_conn.close()
            except Exception:
                pass
            _publish_conn = connect()
            ch = _publish_conn.channel()
            ch.confirm_delivery()
            properties = pika.BasicProperties(delivery_mode=2, priority=priority)
            if expiration_ms is not None:
                properties.expiration = str(int(expiration_ms))
            ch.basic_publish(exchange=exchange(), routing_key=routing_key, body=body, properties=properties)

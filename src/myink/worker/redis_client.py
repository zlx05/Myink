"""Redis 客户端与剩余职责的 key 约定（§阶段2；key 前缀遵守 §14：queue: / lock: / rate:）。

阶段 6 队列迁移后 Redis 不再持有任务队列（主队列/延迟/死信迁 RabbitMQ，见 worker/amqp.py），
保留职责：SSE 进度流、跨 worker 锁、三层闸门、心跳。

- queue:sse:{task} —— SSE 进度 Stream（worker 写，网关只读转发）
- lock:task:{task} —— 跨 worker 防重复执行 SETNX
- lock:book:{pid}  —— 书级租约锁（同书串行、异书并行，§13 BYOK；网关闸门的第一道防线是
                      rate:inflight，resume 直发绕过闸门时书锁为权威）
- rate:inflight:{uid}:{pid} —— 每书并发闸门（异书并行、同书串行；worker 终态 SREM）
- rate:cost:{date} —— 全局日成本累计（worker 终态 INCRBYFLOAT）
- queue:heartbeat:{wid} —— worker 存活心跳
"""

from __future__ import annotations

import redis

from myink.config import settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """进程级 Redis 客户端单例（redis-py 自带连接池）。

    socket_timeout 10s：worker 其余 Redis 操作（SSE/锁/闸门）均短命令，长命令在
    调用方（processor）层已分类重试；保留健康检查防连接池悬挂。
    """
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=10,
            health_check_interval=30,
        )
    return _client


def sse_key(task_id: str) -> str:
    return f"queue:sse:{task_id}"


def lock_key(task_id: str) -> str:
    return f"lock:task:{task_id}"


def book_key(project_id: str) -> str:
    return f"lock:book:{project_id}"


def inflight_key(user_id: str, project_id: str) -> str:
    return f"rate:inflight:{user_id}:{project_id}"


def cost_key(date: str) -> str:
    return f"rate:cost:{date}"

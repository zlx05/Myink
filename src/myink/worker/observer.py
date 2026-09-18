"""SSE 节点进度观察线程（monitor）。

设计权衡（§阶段2）：不用 LangGraph callback 而是**轮询 agent_runs**——
零侵入（graph/runner 完全不感知 Redis），SSE 延迟 ≤1s 对 10–60s 单章可忽略；
callback 方案要改 generate_* 签名 + 依赖 langgraph callback 节点钩子，留作阶段 3 优化。

worker 是单处理线程（checkpointer 进程级单连接约束），monitor 线程只读 agent_runs
（无 RLS，new_session）写 Redis，不碰图，与处理线程互不干扰。
"""

from __future__ import annotations

import logging
import threading
import time

from myink.db import new_session
from myink.models import AgentRun
from myink.worker.redis_client import get_redis, sse_key

logger = logging.getLogger(__name__)


def _poll_runs(r, task_id: str, last_id: int, stream_key: str) -> int:
    """查询 task_id 前缀下的新 agent_runs，写 SSE node 事件，返回最新 id。"""
    with new_session() as db:
        rows = (
            db.query(AgentRun)
            .filter(AgentRun.task_id.like(f"{task_id}%"), AgentRun.id > last_id)
            .order_by(AgentRun.id)
            .all()
        )
    for row in rows:
        r.xadd(
            stream_key,
            {
                "event": "node",
                "task_id": row.task_id,
                "node": row.node,
                "model_id": row.model_id or "",
                "input_tokens": str(row.input_tokens),
                "output_tokens": str(row.output_tokens),
                "cache_hit": "1" if row.cache_hit else "0",
                "duration_ms": str(row.duration_ms),
                "cost_est": str(row.cost_est),
                "retry_count": str(row.retry_count),
                "degraded": "1" if row.degraded else "0",
                "error": row.error or "",
            },
            maxlen=1000,
        )
    if rows:
        r.expire(stream_key, 3600)
        logger.debug("SSE 节点事件 %s 条 (task=%s)", len(rows), task_id)
    return rows[-1].id if rows else last_id


def observe(task_id: str, stop_event: threading.Event) -> None:
    """监控单个任务，直到终态（stop_event 触发）。调用方应预先创建 stream key。"""
    r = get_redis()
    stream_key = sse_key(task_id)
    last_id = 0
    while not stop_event.is_set():
        try:
            last_id = _poll_runs(r, task_id, last_id, stream_key)
        except Exception as exc:
            logger.warning("observer 轮询异常（可忽略）: %s", exc)
        # 终态后继续短轮询 2 次兜底（避免丢最后写入），然后退出
        stop_event.wait(1.0)

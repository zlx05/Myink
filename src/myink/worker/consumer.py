"""消费循环：RabbitMQ 主队列 → process → ack → 可重试失败进延迟队列（TTL+DLX）。

优雅停机（§17.2）：收 SIGTERM → 置 shutdown flag → 跑完当前任务（含 checkpoint）再退出；
未 ack 消息在连接断开时自动 requeue，重启后重新消费（RabbitMQ 崩溃认领，替换网关 dispatcher）。

重试（retry）：body.retry_count+1，`>= worker_max_retries` → basic_nack(requeue=false)
（主队列 DLX → queue:dlq）；否则发布到 queue:delay（expiration=退避毫秒，priority 随属性传递）。
书忙（defer）：发布到 queue:delay（expiration=固定 5s，不 bump retry_count）。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
from functools import partial

import pika

from myink.config import settings
from myink.worker import amqp
from myink.worker.observer import observe
from myink.worker.processor import process
from myink.worker.redis_client import get_redis, sse_key

logger = logging.getLogger(__name__)

_shutdown = threading.Event()


def _handle_sigterm(signum, frame):  # noqa: ARG001
    logger.info("收到 SIGTERM，跑完当前任务后退出")
    _shutdown.set()


def _heartbeat(r, worker_id: str) -> None:
    """worker 存活心跳（网关 /readyz 探活 + 运营判断崩溃）。"""
    while not _shutdown.is_set():
        try:
            r.set(f"queue:heartbeat:{worker_id}", "1", ex=settings.worker_heartbeat_interval * 3)
        except Exception:
            pass
        _shutdown.wait(settings.worker_heartbeat_interval)


def _on_message(ch, method, properties, body_raw, r, worker_id: str) -> None:
    """单条任务：SSE 首事件 + observer 线程 + process + 决策（retry/defer/terminal/skip）。"""
    try:
        body = json.loads(body_raw)
    except json.JSONDecodeError:
        logger.warning("坏消息丢弃: %s %r", method.delivery_tag, body_raw[:200])
        ch.basic_ack(method.delivery_tag)
        return

    task_id = body.get("task_id")
    logger.info("取到任务 %s (%s), delivery=%s", task_id, body.get("task_type"), method.delivery_tag)
    # 确保 SSE stream 存在（observer 只追加，首条 status 由 process 写）
    r.xadd(sse_key(task_id), {"event": "status", "task_id": task_id, "status": "queued"}, maxlen=1000)
    r.expire(sse_key(task_id), 3600)
    obs_stop = threading.Event()
    obs_thread = threading.Thread(target=observe, args=(task_id, obs_stop), daemon=True)
    obs_thread.start()
    try:
        decision = process(body, worker_id=worker_id)
    except Exception as exc:
        # 非预期异常（坏任务/校验失败）：丢弃不重试，worker 保持存活。
        # 可重试失败在 process 内部已分类返回 "retry"，到不了这里。
        logger.error("任务处理异常丢弃: %s task=%s err=%s", method.delivery_tag, task_id, exc)
        ch.basic_ack(method.delivery_tag)
        return
    finally:
        obs_stop.set()
        obs_thread.join(timeout=5)

    # 优先级随消息属性传递：延迟重投/书忙重投保留原优先级（VIP 优先贯穿退避循环）
    priority = int(properties.priority) if properties and properties.priority is not None else 0

    if decision == "retry" and int(body.get("retry_count", 0)) >= settings.worker_max_retries:
        # 重试超限 → 不 requeue → 主队列 DLX → queue:dlq（死信由 RabbitMQ 侧完成）
        ch.basic_nack(method.delivery_tag, requeue=False)
        logger.warning("重试超限进 DLQ: task=%s retry=%s", task_id, body.get("retry_count"))
        return

    ch.basic_ack(method.delivery_tag)
    if decision == "retry":
        retry_count = int(body.get("retry_count", 0))
        backoff_s = settings.worker_backoff_base * (2 ** retry_count)
        body["retry_count"] = retry_count + 1
        amqp.publish(
            json.dumps(body, ensure_ascii=False), amqp.KEY_DELAY,
            priority=priority, expiration_ms=backoff_s * 1000,
        )
        logger.info("退避重投: %s +%ss (retry=%s)", task_id, backoff_s, retry_count + 1)
    elif decision == "defer":
        # 书忙固定退避：不计 retry_count（书忙是瞬态非失败）→ 永远不过 MaxRetries，
        # 重投不经 DLQ；书锁释放后下次重投即执行。同书任务在多个 worker 间自然串行。
        amqp.publish(
            json.dumps(body, ensure_ascii=False), amqp.KEY_DELAY,
            priority=priority, expiration_ms=settings.worker_defer_backoff_s * 1000,
        )
        logger.info("书忙退避重投: %s book=%s +%ss",
                    task_id, body.get("project_id"), settings.worker_defer_backoff_s)
    elif decision == "terminal":
        logger.info("任务完成: %s", task_id)
    # skip: 无事可做


def run() -> None:
    """worker 主循环（阻塞，Ctrl+C / SIGTERM 退出；连接断开自动重连）。"""
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    r = get_redis()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    logger.info("worker 启动: %s", worker_id)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _handle_sigterm)

    hb = threading.Thread(target=_heartbeat, args=(r, worker_id), daemon=True)
    hb.start()

    while not _shutdown.is_set():
        conn = None
        try:
            conn = amqp.connect()
            ch = conn.channel()
            amqp.declare_topology(ch)
            # 同书串行、异书并行（§13）：单 worker 一次拉 1 条；多 worker 进程各自消费
            ch.basic_qos(prefetch_count=1)
            ch.basic_consume(amqp.main_queue(), partial(_on_message, r=r, worker_id=worker_id))
            logger.info("开始消费 %s", amqp.main_queue())
            while not _shutdown.is_set():
                # 阻塞处理事件（无事件时最多挂起 1s）；_on_message 内 process 长任务期间
                # 天然阻塞——prefetch=1 不拉新消息，跑完当前任务才响应停机
                conn.process_data_events(time_limit=1.0)
        except (pika.exceptions.AMQPConnectionError, pika.exceptions.ChannelWrongStateError) as exc:
            logger.warning("RabbitMQ 连接/通道异常，5s 后重连: %s", exc)
        except Exception as exc:
            logger.error("消费循环异常退出（待重连）: %s", exc)
        finally:
            if conn is not None and not conn.is_closed:
                try:
                    conn.close()
                except Exception:
                    pass
            _shutdown.wait(5)

    logger.info("worker 退出")
    hb.join(timeout=5)


if __name__ == "__main__":
    run()

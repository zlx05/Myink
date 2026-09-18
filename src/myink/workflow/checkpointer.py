"""PG Checkpointer（§6.7：开发与生产统一 PostgreSQL，不用 SQLite/Redis）。

- 持久化分级（生产标准，plan §6.7/§5.3）：checkpoint 是任务现场，丢了用户从头重跑，
  必须落有备份/PITR 的 PG；Redis 只管可重建数据；
- checkpointer 表（checkpoints/checkpoint_blobs/checkpoint_writes）是 LangGraph 内部表，
  不带 project_id/RLS——靠 task_id（thread_id）归属校验，不属于业务租户表（§14 隔离清单）。
"""

from __future__ import annotations

import logging
from typing import Iterable

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from myink.config import settings
from myink.db_url import normalize_localhost_database_url

logger = logging.getLogger(__name__)

_build: dict[str, PostgresSaver] = {}

def _psycopg_conn_str(database_url: str) -> str:
    """Convert SQLAlchemy URL and avoid threaded ``localhost`` DNS stalls on Windows."""
    value = database_url.replace("postgresql+psycopg://", "postgresql://")
    return normalize_localhost_database_url(value)


# 复用 psycopg3 连接串（去掉 SQLAlchemy 方言前缀）。
_CONN_STR = _psycopg_conn_str(settings.database_url)


def build_checkpointer() -> PostgresSaver:
    """构建并初始化 PG Checkpointer（幂等：setup() 建内部表）。

    使用 psycopg 连接池而不是进程级单连接。每次 checkout 都先检查连接，PostgreSQL
    重启后会丢弃旧连接并重连；否则缓存的 PostgresSaver 会永久持有已关闭连接，直到
    worker 自身重启，后续每个任务都会立即失败。
    checkpointer 连接是 psycopg 直连，与 SQLAlchemy 双引擎完全分离（§6.7）。
    """
    if "saver" in _build:
        return _build["saver"]
    pool = ConnectionPool(
        conninfo=_CONN_STR,
        min_size=1,
        max_size=2,
        open=True,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        check=ConnectionPool.check_connection,
    )
    pool.wait()
    saver = PostgresSaver(pool)
    saver.setup()  # 建 checkpoints / checkpoint_blobs / checkpoint_writes
    _build["saver"] = saver
    return saver


def delete_threads(thread_ids: Iterable[str]) -> None:
    """按 thread_id 清理 LangGraph checkpoint 三张内部表（整本书删除现场）。

    checkpoint 三表无 project_id、无 RLS、无 FK 级联（checkpoint_blobs/writes 对
    checkpoints 无 REFERENCES），必须显式按 thread_id 全删；batch 每章 run id 形如
    `{batch_id}:ch{seq}`（§任务）→ 用 LIKE `{tid}:ch%` 一并覆盖。
    thread_ids 集合 = 删除前从 tasks 表快照的任务 id（单章=裸 task_id，批次=裸 batch_id）。
    """
    with Connection.connect(_CONN_STR, autocommit=True, prepare_threshold=0) as conn:
        for tid in thread_ids:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                # psycopg3 原生字符串 SQL + %s 占位（like 的 % 在绑定值里，不在 SQL 文本中）
                conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = %s OR thread_id LIKE %s",
                    (tid, f"{tid}:ch%"),
                )

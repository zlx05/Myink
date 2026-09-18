"""worker 任务锁（租约锁，§6.12 崩溃恢复缺口修复，2026-08-08）。

阶段 2 实测缺口：裸 `SETNX lock:task:{id} worker-xxx` + 固定 TTL（30min）无心跳——
worker 崩溃后锁活 30 分钟，网关 XAUTOCLAIM 认领重投后新 worker SETNX 失败
→ 任务滞留 running。修法借鉴通用分布式租约锁原则（自研实现，非外部项目移植）：

- 锁值 = JSON `{token, worker_id, heartbeat_at}`：token 是所有权凭据，heartbeat 是存活证据；
- 持锁期间后台线程每 `worker_lock_heartbeat` 秒续租（刷新心跳 + 重置 TTL）；
- 崩溃 → 心跳停 → 锁在 TTL（60s）内自过期，远早于网关认领阈值（MinIdle 120s）；
- acquire 失败时读锁值，心跳过老（> 3×心跳间隔）判僵尸 → Lua CAS 删除后重试；
  活锁（心跳新鲜）→ 返回 None 跳过（他 worker 在跑，含慢批次任务被网关误认领的场景）；
- 续租 / 释放都带 CAS（token 比对）：不复活已易主的锁、不误删他人新锁。

关键：**网关不碰 lock:task:**——盲清锁会破坏「活慢 worker 防双跑」语义（长批次 >120s
被网关认领后靠锁挡住重投），心跳续租才是区分「活 worker」与「死 worker」的正解。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from myink.config import settings

logger = logging.getLogger(__name__)

# CAS：仅当锁值仍等于持有值才删除（释放 / 僵尸回收共用，不误删他人新锁）
_CAS_DEL = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

# CAS：仅当锁仍归自己才续租（保留 token、刷新心跳 + TTL；不复活已易主的锁）
_CAS_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
    return 1
else
    return 0
end
"""


def _lock_value(worker_id: str) -> str:
    return json.dumps({
        "token": str(uuid.uuid4()),
        "worker_id": worker_id,
        "heartbeat_at": int(time.time() * 1000),
    }, ensure_ascii=False)


def _parse(value: str) -> dict | None:
    try:
        meta = json.loads(value)
        return meta if isinstance(meta, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None  # 非 JSON（旧格式 worker-xxx / 脏数据）→ 无法证明存活，判僵尸


def _is_stale(meta: dict | None) -> bool:
    """心跳停止超过 3×心跳间隔 → 僵尸锁（崩溃残留），可回收。

    旧格式（非 JSON）无心跳证据 → 判僵尸（升级迁移时旧崩溃锁可被新 worker 回收）。
    """
    if not meta:
        return True
    hb = meta.get("heartbeat_at")
    if not isinstance(hb, (int, float)):
        return True
    stale_ms = settings.worker_lock_heartbeat * 3 * 1000
    return time.time() * 1000 - hb > stale_ms


class TaskLock:
    """租约锁句柄（任务锁 / 书锁共用）：acquire 后后台线程续租，release 停止续租并 CAS 删除。

    key 为原始 Redis 键（调用方传入 lock:task:{id} / lock:book:{pid}），ttl 默认取
    worker_inflight_ttl。多书并行（§13 BYOK）：书锁保证同书串行、异书并行，跨进程靠
    Redis 原子性 + 心跳续租区分「活 worker」与「崩溃残留」。
    """

    def __init__(self, r, key: str, value: str, ttl: int | None = None):
        self._r = r
        self._key = key
        self._value = value
        self._worker_id = (json.loads(value).get("worker_id") or "").__str__()  # 归属凭据（release 兜底）
        self._ttl = ttl if ttl is not None else settings.worker_inflight_ttl
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True, name=f"lock-{key[-8:]}")

    @classmethod
    def acquire(cls, r, key: str, worker_id: str, ttl: int | None = None) -> "TaskLock | None":
        """获取租约锁；被活 worker 持有返回 None（跳过），僵尸锁自动回收后重试。"""
        ttl = ttl if ttl is not None else settings.worker_inflight_ttl
        for _ in range(2):  # 一次僵尸回收重试
            value = _lock_value(worker_id)
            if r.set(key, value, nx=True, ex=ttl):
                lock = cls(r, key, value, ttl)
                lock._thread.start()
                return lock
            cur = r.get(key)
            if cur is None:
                continue  # 刚被删/过期，重试
            if not _is_stale(_parse(cur)):
                logger.info("锁被活 worker 持有，跳过: %s", key)
                return None
            # 僵尸锁：CAS 删除（期间可能已被他人接管，值匹配才删）后重试
            if r.eval(_CAS_DEL, 1, key, cur):
                logger.info("回收僵尸锁: %s", key)
                continue
            return None
        return None

    def _beat(self) -> None:
        while not self._stop.wait(settings.worker_lock_heartbeat):
            try:
                new_value = _refresh_heartbeat(self._value)
                if not self._r.eval(_CAS_RENEW, 1, self._key, self._value, new_value,
                                    self._ttl):
                    return  # 锁已易主（被僵尸回收/异常接管），停止续租
                self._value = new_value
            except Exception:
                # 续租瞬态失败（网络抖动）：不杀线程，下轮重试。若 renew 已应用到
                # 服务端但客户端没拿到成功返回，self._value 会过期——由 release 的
                # worker_id 归属兜底删除，锁不会滞留到 TTL。
                logger.warning("续租异常（下轮重试）: %s", self._key)
                continue

    def release(self) -> None:
        """停止续租 + 删除（仅删自己的租约，不误删他人新锁）。

        E2E 暴露的竞态：renew 已应用到服务端但客户端异常，self._value 停留在旧值，
        CAS_DEL 值比对失配 → 锁滞留到 TTL。修复：先精确值比对；失配时若当前锁值
        仍归属本 worker_id（token 同源，续租只改心跳不改归属），则用当前值删除。
        """
        self._stop.set()
        self._thread.join(timeout=settings.worker_lock_heartbeat * 2 + 1)
        try:
            cur = self._r.get(self._key)
            if cur is None:
                return  # 已不存在（TTL 到期 / 已被他人回收）
            if cur == self._value:
                self._r.eval(_CAS_DEL, 1, self._key, self._value)
                return
            meta = _parse(cur)
            if meta and meta.get("worker_id") == self._worker_id:
                # 仍是我们自己的租约（续租只刷心跳不改 worker_id）→ 删除当前值
                self._r.eval(_CAS_DEL, 1, self._key, cur)
        except Exception as exc:
            logger.warning("释放锁失败（TTL 到期兜底）: %s err=%s", self._key, exc)


def _refresh_heartbeat(value: str) -> str:
    """刷新锁值心跳（保留 token / worker_id，仅更新 heartbeat_at）。"""
    meta = json.loads(value)
    meta["heartbeat_at"] = int(time.time() * 1000)
    return json.dumps(meta, ensure_ascii=False)

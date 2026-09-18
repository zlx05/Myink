"""项目任务历史列表测试（GET /projects/{pid}/tasks，阶段 4 任务视图）。

范围：
- 形状：task_id/task_type/status/chapter_seq/batch_size/batch_current/error/created_at；
- 降序：created_at 新的在前（显式设 created_at 断言排序）；
- 批次进度派生：payload.size → batch_size，该批 persist 节点去重章数 → batch_current；
- 空项目 → []（不 500）；越权矩阵（伪造他人 403 / 缺失身份 403 / 项目不存在 404 / id 非法 400）。

模式：tasks 无 RLS 观测表，new_session 直插；身份头仿 test_book_setup `_h(uid)`。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from myink.api.main import app
from myink.db import new_session
from myink.models import AgentRun, Task, User

client = TestClient(app)


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `myink init`（demo 用户未建）"
        return u.id


def _h(uid: str | uuid.UUID | None) -> dict:
    return {"X-Myink-User": str(uid)} if uid is not None else {}


def _add_task(pid, *, task_type="chapter_generate", status="done", chapter_seq=None,
              payload=None, created_at=None) -> str:
    """插一条任务（观测表无 RLS，new_session 直写），返回 task_id。"""
    with new_session() as db:
        t = Task(project_id=uuid.UUID(pid), task_type=task_type, status=status,
                 payload=payload or {}, chapter_seq=chapter_seq)
        if created_at is not None:
            t.created_at = created_at
        db.add(t)
        db.commit()
        return str(t.id)


def _cleanup(tids: list[str], pid: str) -> None:
    """删任务 + 关联 agent_runs（无 FK 级联，手动清）。"""
    with new_session() as db:
        for tid in tids:
            db.execute(sa_delete(AgentRun).where(AgentRun.task_id.like(f"{tid}%")))
        db.execute(sa_delete(Task).where(Task.project_id == uuid.UUID(pid)))
        db.commit()


def test_list_tasks_shape_and_desc_order(temp_project):
    now = datetime.now(timezone.utc)
    tids = [
        _add_task(temp_project, task_type="chapter_generate", status="done",
                  chapter_seq=1, created_at=now),
        _add_task(temp_project, task_type="chapter_generate", status="failed",
                  chapter_seq=2, created_at=now - timedelta(minutes=5)),
    ]
    try:
        resp = client.get(f"/internal/v1/projects/{temp_project}/tasks", headers=_h(_demo_user_id()))
        assert resp.status_code == 200
        items = resp.json()
        assert [i["task_id"] for i in items] == [tids[0], tids[1]], "created_at 新的在前"
        first = items[0]
        assert first["task_type"] == "chapter_generate"
        assert first["status"] == "done"
        assert first["chapter_seq"] == 1
        assert first["batch_size"] is None and first["batch_current"] is None, "单章无批次进度"
        assert first["error"] is None and first["created_at"] is not None
        assert set(first) == {"task_id", "task_type", "status", "chapter_seq",
                              "batch_size", "batch_current", "cost_total",
                              "error", "created_at"}
    finally:
        _cleanup(tids, temp_project)


def test_list_tasks_empty_project_returns_empty(temp_project):
    resp = client.get(f"/internal/v1/projects/{temp_project}/tasks", headers=_h(_demo_user_id()))
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_tasks_batch_progress_derived(temp_project):
    """批次进度 = payload.size 与 persist 节点去重章数（retry 同章不重复计数）。"""
    tid = _add_task(temp_project, task_type="batch_generate", status="running",
                    payload={"size": 3})
    with new_session() as db:
        pid = uuid.UUID(temp_project)
        # ch1 persist 一次、ch2 persist 两次（重试）→ 去重 current=2
        for ch, node in [("ch1", "persist"), ("ch2", "persist"), ("ch2", "persist"), ("ch2", "recall")]:
            db.add(AgentRun(project_id=pid, task_id=f"{tid}:{ch}", node=node))
        db.commit()
    try:
        resp = client.get(f"/internal/v1/projects/{temp_project}/tasks", headers=_h(_demo_user_id()))
        item = next(i for i in resp.json() if i["task_id"] == tid)
        assert item["batch_size"] == 3
        assert item["batch_current"] == 2, "persist 去重章数，非任意 run 计数"
    finally:
        _cleanup([tid], temp_project)


def test_list_tasks_cost_total_single_and_batch(temp_project):
    """cost_total（§6.8 成本透明）：单章 = 该章全部节点 cost 和；批次 = 全批（含 :ch 前缀）。"""
    single = _add_task(temp_project, task_type="chapter_generate", status="done", chapter_seq=1)
    batch = _add_task(temp_project, task_type="batch_generate", status="done", payload={"size": 2})
    pid = uuid.UUID(temp_project)
    with new_session() as db:
        # 单章：3 节点（含 cost=0 的确定性节点）
        for i, cost in enumerate([0.012, 0.006, 0.0]):
            db.add(AgentRun(project_id=pid, task_id=single, node=f"n{i}", cost_est=cost))
        # 批次：batch_plan 裸 id + ch1/ch2 前缀各 2 节点
        for tid, costs in [(batch, [0.02]), (f"{batch}:ch1", [0.03, 0.004]), (f"{batch}:ch2", [0.01])]:
            for cost in costs:
                db.add(AgentRun(project_id=pid, task_id=tid, node="write", cost_est=cost))
        db.commit()
    try:
        resp = client.get(f"/internal/v1/projects/{temp_project}/tasks", headers=_h(_demo_user_id()))
        items = {i["task_id"]: i for i in resp.json()}
        assert round(items[single]["cost_total"], 4) == 0.018, "单章 = 全节点 cost 和"
        assert round(items[batch]["cost_total"], 4) == 0.064, "批次 = 裸 id + 全 :ch 前缀聚合"
    finally:
        _cleanup([single, batch], temp_project)


def test_get_task_cost_total(temp_project):
    """详情 cost_total = 该任务 runs 的 cost 和（GET /tasks/{id}）；runs 带 task_id 供前端按章切。"""
    tid = _add_task(temp_project, task_type="chapter_generate", status="done", chapter_seq=1)
    pid = uuid.UUID(temp_project)
    with new_session() as db:
        db.add(AgentRun(project_id=pid, task_id=tid, node="write", cost_est=0.021))
        db.add(AgentRun(project_id=pid, task_id=tid, node="extract", cost_est=0.007))
        db.add(AgentRun(project_id=pid, task_id=tid, node="persist", cost_est=0.0))
        db.commit()
    try:
        resp = client.get(f"/internal/v1/tasks/{tid}", headers=_h(_demo_user_id()))
        assert resp.status_code == 200
        detail = resp.json()
        assert round(detail["cost_total"], 4) == 0.028, "详情总花费 = runs cost 和"
        assert len(detail["runs"]) == 3
        assert all(r["task_id"] == tid for r in detail["runs"]), "run 应带所属子线程 task_id"
    finally:
        _cleanup([tid], temp_project)


def test_list_tasks_chapter_filter(temp_project):
    """chapter_seq 查询参数（§11 右栏按章过滤）：只列覆盖该章的任务，cost 收窄为该章切片。

    - 单章任务：chapter_seq 精确匹配，cost = 全任务；
    - 批次任务：payload.start..start+size 范围覆盖，cost = 只计 {batch}:ch{seq} 行
      （book 级 run 如 batch_plan 不计入章成本）；
    - 不覆盖该章的任务（另一批）不出现在结果里。
    """
    single = _add_task(temp_project, task_type="chapter_generate", status="done", chapter_seq=2)
    batch2 = _add_task(temp_project, task_type="batch_generate", status="done",
                       payload={"start": 2, "size": 3})  # 覆盖 2..4
    batch5 = _add_task(temp_project, task_type="batch_generate", status="done",
                       payload={"start": 5, "size": 2})  # 覆盖 5..6
    pid = uuid.UUID(temp_project)
    with new_session() as db:
        # 单章 ch2：全任务 cost
        db.add(AgentRun(project_id=pid, task_id=single, node="write", cost_est=0.01))
        # batch2：book 级 run（batch_plan 裸 id）+ ch2/ch3 章 run
        db.add(AgentRun(project_id=pid, task_id=batch2, node="batch_plan", cost_est=0.05))
        db.add(AgentRun(project_id=pid, task_id=f"{batch2}:ch2", node="write", cost_est=0.02))
        db.add(AgentRun(project_id=pid, task_id=f"{batch2}:ch2", node="persist", cost_est=0.0))
        db.add(AgentRun(project_id=pid, task_id=f"{batch2}:ch3", node="write", cost_est=0.03))
        # batch5：ch5 章 run
        db.add(AgentRun(project_id=pid, task_id=f"{batch5}:ch5", node="write", cost_est=0.07))
        db.commit()
    try:
        # 过滤 ch2 → single + batch2（batch5 不覆盖，剔除）
        resp = client.get(f"/internal/v1/projects/{temp_project}/tasks?chapter_seq=2",
                          headers=_h(_demo_user_id()))
        assert resp.status_code == 200
        items = {i["task_id"]: i for i in resp.json()}
        assert set(items) == {single, batch2}, "只列覆盖该章的任务"
        assert round(items[single]["cost_total"], 4) == 0.01, "单章 = 全任务 cost"
        assert round(items[batch2]["cost_total"], 4) == 0.02, \
            "批次章成本 = :ch2 切片，book 级 batch_plan 不计入"
        assert items[batch2]["batch_size"] == 3
        # 过滤 ch5 → 只有 batch5，cost = ch5 切片
        resp5 = client.get(f"/internal/v1/projects/{temp_project}/tasks?chapter_seq=5",
                           headers=_h(_demo_user_id()))
        items5 = {i["task_id"]: i for i in resp5.json()}
        assert set(items5) == {batch5}
        assert round(items5[batch5]["cost_total"], 4) == 0.07
        # 无过滤 → 全量（回归：原口径批次 = 裸 id + 全 :ch 前缀聚合）
        resp_all = client.get(f"/internal/v1/projects/{temp_project}/tasks",
                              headers=_h(_demo_user_id()))
        items_all = {i["task_id"]: i for i in resp_all.json()}
        assert round(items_all[batch2]["cost_total"], 4) == 0.1, "全量口径批次含 book 级 run"
    finally:
        _cleanup([single, batch2, batch5], temp_project)


def test_list_tasks_ownership_matrix(temp_project):
    """越权矩阵：缺失身份 403 / 伪造他人 403（§14.1 ③ fail closed）。"""
    url = f"/internal/v1/projects/{temp_project}/tasks"
    assert client.get(url).status_code == 403
    assert client.get(url, headers=_h("00000000-0000-0000-0000-000000000000")).status_code == 403


def test_list_tasks_project_missing_404():
    pid = uuid.uuid4()
    resp = client.get(f"/internal/v1/projects/{pid}/tasks", headers=_h(_demo_user_id()))
    assert resp.status_code == 404


def test_list_tasks_invalid_project_id_400():
    resp = client.get("/internal/v1/projects/not-a-uuid/tasks", headers=_h(_demo_user_id()))
    assert resp.status_code == 400


def test_get_task_backfills_zero_cost_for_deepseek_flash(temp_project):
    """历史行 cost_est=0 但 Token 有数：按 deepseek-flash 单价回算，避免流转图全是 ¥0。"""
    tid = _add_task(temp_project, task_type="chapter_generate", status="done", chapter_seq=1)
    pid = uuid.UUID(temp_project)
    with new_session() as db:
        db.add(AgentRun(
            project_id=pid, task_id=tid, node="write", model_id="deepseek-flash",
            input_tokens=1_000_000, output_tokens=500_000, cache_hit=False, cost_est=0.0,
        ))
        db.add(AgentRun(project_id=pid, task_id=tid, node="persist", cost_est=0.0))
        db.commit()
    try:
        detail = client.get(f"/internal/v1/tasks/{tid}", headers=_h(_demo_user_id())).json()
        assert detail["runs"][0]["cost_est"] == 3.0
        assert detail["runs"][1]["cost_est"] == 0.0
        assert detail["cost_total"] == 3.0
        listed = client.get(
            f"/internal/v1/projects/{temp_project}/tasks", headers=_h(_demo_user_id()),
        ).json()
        item = next(row for row in listed if row["task_id"] == tid)
        assert item["cost_total"] == 3.0
    finally:
        _cleanup([tid], temp_project)

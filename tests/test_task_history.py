"""章节流转历史必须完整持久化读取，不能因刷新或数量上限消失。"""

import uuid

from myink.api.routes_tasks import _task_payload, list_project_tasks
from myink.db import new_session
from myink.models import AgentRun, Task


def _task(project_id: str, *, chapter_seq: int) -> Task:
    return Task(
        project_id=uuid.UUID(project_id), task_type="chapter_generate", status="done",
        payload={"seq": chapter_seq}, chapter_seq=chapter_seq,
    )


def test_task_detail_returns_every_persisted_run_beyond_old_200_limit(temp_project: str) -> None:
    with new_session() as db:
        task = _task(temp_project, chapter_seq=1)
        db.add(task)
        db.flush()
        db.add_all([
            AgentRun(
                project_id=uuid.UUID(temp_project), task_id=str(task.id), node=f"step_{index}",
                input_tokens=0, output_tokens=0, cache_hit=False, duration_ms=0,
                cost_est=0, retry_count=0, degraded=False,
            )
            for index in range(205)
        ])
        db.commit()
        task_id = str(task.id)

    detail = _task_payload(task_id)
    assert len(detail["runs"]) == 205
    assert detail["runs"][0]["node"] == "step_0"
    assert detail["runs"][-1]["node"] == "step_204"


def test_chapter_query_finds_old_task_after_more_than_50_newer_project_tasks(temp_project: str) -> None:
    with new_session() as db:
        old = _task(temp_project, chapter_seq=1)
        db.add(old)
        db.flush()
        old_id = str(old.id)
        db.add_all([_task(temp_project, chapter_seq=2) for _ in range(51)])
        db.commit()

    rows = list_project_tasks(temp_project, chapter_seq=1)
    assert [row["task_id"] for row in rows] == [old_id]

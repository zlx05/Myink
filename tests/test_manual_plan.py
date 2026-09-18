"""手动 Plan 门和 replan 再确认的端到端工作流回归（模型为本地测试桩）。"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException

from myink.api.routes_tasks import PlanConfirmBody, confirm_task_plan
from myink.db import new_session
from myink.models import AgentRun, Task
from myink.providers.base import ModelProvider, ModelResponse
from myink.workflow.runner import generate_chapter, new_task, resume_chapter_plan


class ManualPlanProvider(ModelProvider):
    def __init__(self, *, replan_once: bool = False):
        self.replan_once = replan_once
        self.calls: list[str] = []
        self.plan_count = 0
        self.audit_count = 0

    def name(self) -> str:
        return "manual-plan-stub"

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None,
                 json_mode=False, tools=None, disable_thinking=False):
        from test_flow import _infer_node

        node = _infer_node(messages)
        self.calls.append(node)
        if node == "cast":
            content = json.dumps({"cast": ["林砚"], "locations": ["山门"]}, ensure_ascii=False)
        elif node == "plan":
            self.plan_count += 1
            content = json.dumps({
                "goals": [f"第 {self.plan_count} 版目标"],
                "scenes": [{"location_id": "山门", "participants": ["林砚"],
                            "goal": "继续追查", "time": "清晨"}],
                "characters": [], "hooks_to_plant": [], "hooks_to_resolve": [],
                "expected_events": [f"落实第 {self.plan_count} 版事件"],
                "hard_constraints": ["不得无因升级"],
                "transition": {
                    "mode": "opening", "anchor_quote": "", "pending_action": "",
                    "opening_beat": "林砚推门走入清晨的山道。", "bridge": "",
                },
            }, ensure_ascii=False)
        elif node == "write":
            content = "=== CONTENT ===\n林砚推门走入清晨的山道，循着新计划继续追查。"
        elif node == "extract":
            content = '{"candidates":[]}'
        elif node == "audit":
            self.audit_count += 1
            verdict = "replan" if self.replan_once and self.audit_count == 1 else "pass"
            content = json.dumps({
                "verdict": verdict,
                "replan_target": "chapter" if verdict == "replan" else None,
                "findings": [], "reasons": ["需要换一版计划" if verdict == "replan" else "通过"],
                "confidence": 0.9,
            }, ensure_ascii=False)
        elif node == "summarize":
            content = '{"summary":"手动计划测试章"}'
        elif node == "revise":
            content = "=== CONTENT ===\n修订正文"
        else:
            content = "{}"
        return ModelResponse(
            content=content, model_id=model_id, input_tokens=10,
            output_tokens=20, duration_ms=1,
        )


def _create_manual_task(project_id: str) -> str:
    return new_task(
        project_id=project_id, task_type="chapter_generate",
        payload={"seq": 1, "mode": "manual"}, chapter_seq=1, status="running",
    )


def test_manual_plan_waits_for_edit_and_resume_does_not_rerun_planner(
    temp_project, monkeypatch,
):
    import myink.providers as providers_mod

    provider = ManualPlanProvider()
    monkeypatch.setattr(providers_mod, "default_provider", provider)
    task_id = _create_manual_task(temp_project)

    waiting = generate_chapter(
        project_id=temp_project, chapter_seq=1, task_id=task_id, writing_mode="manual",
    )
    assert waiting["awaiting_plan"] is True
    assert provider.calls.count("plan") == 1
    assert provider.calls.count("write") == 0
    with new_session() as db:
        assert db.get(Task, uuid.UUID(task_id)).status == "awaiting_plan"

    approved = dict(waiting["plan"])
    approved["goals"] = ["用户编辑后的目标"]
    finished = resume_chapter_plan(
        project_id=temp_project, task_id=task_id, approved_plan=approved,
    )

    assert not finished.get("awaiting_plan")
    assert provider.calls.count("plan") == 1, "确认计划不应重新调用 Planner"
    assert provider.calls.count("write") == 1
    with new_session() as db:
        review = (db.query(AgentRun)
                  .filter(AgentRun.task_id == task_id, AgentRun.node == "plan_review")
                  .one())
        assert review.detail["changed"] is True
        assert review.detail["approved_plan"]["goals"] == ["用户编辑后的目标"]
        assert db.get(Task, uuid.UUID(task_id)).status == "done"


def test_manual_replan_pauses_again_and_keeps_both_plan_versions(
    temp_project, monkeypatch,
):
    import myink.providers as providers_mod

    provider = ManualPlanProvider(replan_once=True)
    monkeypatch.setattr(providers_mod, "default_provider", provider)
    task_id = _create_manual_task(temp_project)

    first = generate_chapter(
        project_id=temp_project, chapter_seq=1, task_id=task_id, writing_mode="manual",
    )
    second = resume_chapter_plan(
        project_id=temp_project, task_id=task_id, approved_plan=first["plan"],
    )
    assert second["awaiting_plan"] is True
    assert second["plan"]["goals"] == ["第 2 版目标"]
    assert provider.calls.count("plan") == 2
    assert provider.calls.count("write") == 1

    final = resume_chapter_plan(
        project_id=temp_project, task_id=task_id, approved_plan=second["plan"],
    )
    assert not final.get("awaiting_plan")
    assert provider.calls.count("plan") == 2
    assert provider.calls.count("write") == 2
    with new_session() as db:
        plans = (db.query(AgentRun)
                 .filter(AgentRun.task_id == task_id, AgentRun.node == "plan_chapter")
                 .order_by(AgentRun.id).all())
        reviews = (db.query(AgentRun)
                   .filter(AgentRun.task_id == task_id, AgentRun.node == "plan_review")
                   .order_by(AgentRun.id).all())
        assert [row.detail["plan_attempt"] for row in plans] == [1, 2]
        assert [row.detail["plan_attempt"] for row in reviews] == [1, 2]


def test_plan_confirm_api_validates_checkpoint_and_queues_exact_edited_version(
    temp_project, monkeypatch,
):
    import myink.providers as providers_mod
    from myink.worker import amqp

    provider = ManualPlanProvider()
    monkeypatch.setattr(providers_mod, "default_provider", provider)
    task_id = _create_manual_task(temp_project)
    waiting = generate_chapter(
        project_id=temp_project, chapter_seq=1, task_id=task_id, writing_mode="manual",
    )

    invalid = dict(waiting["plan"])
    invalid["transition"] = {**invalid["transition"], "anchor_quote": "并不存在的上一章原文"}
    with pytest.raises(HTTPException) as exc:
        confirm_task_plan(task_id, PlanConfirmBody(plan=invalid, expected_attempt=1))
    assert exc.value.status_code == 422
    with new_session() as db:
        assert db.get(Task, uuid.UUID(task_id)).status == "awaiting_plan"

    published: list[tuple[str, str]] = []
    monkeypatch.setattr(amqp, "publish", lambda message, key: published.append((message, key)))
    edited = dict(waiting["plan"])
    edited["goals"] = ["API 提交的用户版本"]
    result = confirm_task_plan(task_id, PlanConfirmBody(plan=edited, expected_attempt=1))

    assert result["status"] == "queued"
    assert len(published) == 1
    message = json.loads(published[0][0])
    assert message["task_type"] == "chapter_plan_resume"
    assert message["task_id"] == task_id
    assert message["payload"]["approved_plan"]["goals"] == ["API 提交的用户版本"]
    with new_session() as db:
        assert db.get(Task, uuid.UUID(task_id)).status == "queued"

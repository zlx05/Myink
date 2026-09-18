"""整书大纲：题材分卷 + 约 30 章一段的阶段（禁止逐章细纲）。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from myink.api.main import app
from myink.db import new_session, tenant_session
from myink.models import AgentRun, User, VolumeOutline
from myink.providers.base import ModelProvider, ModelResponse
from myink.workflow import prompts
from myink.workflow.outline import STAGE_SPAN, normalize_outline

client = TestClient(app)

_OUTLINE = {
    "objective": "从杂役修士成为宗门长老并公开父辈冤案真相",
    "volumes": [
        {
            "volume_seq": 1,
            "title": "第一卷 · 青云山下",
            "theme": "立身",
            "goal": "入宗立足并发现玉佩疑点",
            "key_results": ["通过入门试炼", "拜入长老座下", "发现玉佩疑点"],
            "end_event": "主角被迫离开青云宗",
            "chapter_start": 1,
            "chapter_end": 80,
            "stages": [
                {"name": "前期", "chapter_start": 1, "chapter_end": 30,
                 "goal": "入门试炼", "beats": ["得玉佩", "遇苏瑶"]},
                {"name": "中期", "chapter_start": 31, "chapter_end": 60,
                 "goal": "拜入长老", "beats": ["试炼", "结怨"]},
                {"name": "后期", "chapter_start": 61, "chapter_end": 80,
                 "goal": "发现玉佩疑点", "beats": ["夜探藏经阁"]},
            ],
        },
        {
            "volume_seq": 2,
            "title": "第二卷 · 北境",
            "theme": "流亡",
            "goal": "查明真相并攒下复仇资本",
            "key_results": ["追查玉佩源头", "结识盟友"],
            "end_event": "宗门惊变、真相大白",
            "chapter_start": 81,
            "chapter_end": 160,
            "stages": [
                {"name": "本卷", "chapter_start": 81, "chapter_end": 160,
                 "goal": "流落北境立足", "beats": ["北境遇险"]},
            ],
        },
    ],
}


def _demo_user_id() -> uuid.UUID:
    with new_session() as db:
        u = db.query(User).filter(User.username == "demo").first()
        assert u is not None, "请先运行 `myink init`（demo 用户未建）"
        return u.id


def _h(uid: str | uuid.UUID | None) -> dict:
    return {"X-Myink-User": str(uid)} if uid is not None else {}


class _OutlineStub(ModelProvider):
    def __init__(self, payload=None, *, raw=None, raise_error=False):
        self._payload = payload
        self._raw = raw
        self._raise_error = raise_error

    def name(self):
        return "outline-stub"

    def generate(self, messages, *, model_id, max_tokens=None, temperature=None, json_mode=False,
                 tools=None, disable_thinking=False):
        if self._raise_error:
            raise RuntimeError("provider down")
        import json as _json
        content = self._raw if self._raw is not None else _json.dumps(self._payload or {}, ensure_ascii=False)
        return ModelResponse(content=content, model_id=model_id, input_tokens=10, output_tokens=20)


def _outline_run_count(pid: str) -> int:
    with new_session() as db:
        return db.query(AgentRun).filter(
            AgentRun.project_id == uuid.UUID(pid), AgentRun.node == "book_outline").count()


def test_outline_draft_returns_draft_not_persisted(temp_project, monkeypatch):
    import myink.providers as providers_mod

    monkeypatch.setattr(providers_mod, "default_provider", _OutlineStub(_OUTLINE))
    resp = client.post(f"/internal/v1/projects/{temp_project}/outline-draft",
                       headers=_h(_demo_user_id()),
                       json={"premise": "少年得玉佩追寻真相", "chapter_count": 160,
                             "storyline": "前期宗门、中期追查、后期决战"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] is None
    outline = data["outline"]
    assert outline["objective"] == _OUTLINE["objective"]
    assert len(outline["volumes"]) == 2
    assert len(outline["volumes"][0]["stages"]) == 3
    assert outline["volumes"][0]["chapter_end"] == 80
    with tenant_session(temp_project) as db:
        assert db.query(VolumeOutline).count() == 0
    assert _outline_run_count(temp_project) == 1


def test_outline_draft_degraded_on_provider_error(temp_project, monkeypatch):
    import myink.providers as providers_mod
    monkeypatch.setattr(providers_mod, "default_provider", _OutlineStub(raise_error=True))
    resp = client.post(f"/internal/v1/projects/{temp_project}/outline-draft",
                       headers=_h(_demo_user_id()),
                       json={"premise": "少年追查玉佩真相", "chapter_count": 80})
    assert resp.status_code == 200
    assert resp.json() == {"outline": {}, "error": "provider down"}


def test_outline_draft_degraded_on_bad_json(temp_project, monkeypatch):
    import myink.providers as providers_mod
    monkeypatch.setattr(providers_mod, "default_provider",
                        _OutlineStub(raw="not json{{{", raise_error=False))
    resp = client.post(f"/internal/v1/projects/{temp_project}/outline-draft",
                       headers=_h(_demo_user_id()),
                       json={"premise": "少年追查玉佩真相", "chapter_count": 80})
    assert resp.status_code == 200
    data = resp.json()
    assert data["outline"] == {}
    assert data["error"] is not None and "parse_error" in data["error"]


def test_outline_draft_degraded_wrong_shape(temp_project, monkeypatch):
    import myink.providers as providers_mod
    monkeypatch.setattr(providers_mod, "default_provider",
                        _OutlineStub({"arc": ["起"], "not_volumes": []}))
    resp = client.post(f"/internal/v1/projects/{temp_project}/outline-draft",
                       headers=_h(_demo_user_id()),
                       json={"premise": "少年追查玉佩真相", "chapter_count": 80})
    assert resp.status_code == 200
    data = resp.json()
    assert data["outline"] == {}
    assert data["error"] is not None and "unexpected_shape" in data["error"]


def test_outline_draft_validation_400(temp_project, monkeypatch):
    import myink.providers as providers_mod
    monkeypatch.setattr(providers_mod, "default_provider", _OutlineStub(_OUTLINE))
    url = f"/internal/v1/projects/{temp_project}/outline-draft"
    assert client.post(url, headers=_h(_demo_user_id()),
                       json={"premise": "  ", "chapter_count": 80}).status_code == 400
    assert client.post(url, headers=_h(_demo_user_id()),
                       json={"premise": "少年", "chapter_count": 0}).status_code == 400
    assert client.post(url, headers=_h(_demo_user_id()),
                       json={"premise": "少年", "chapter_count": 49}).status_code == 400
    assert client.post(url, headers=_h(_demo_user_id()),
                       json={"premise": "少年", "chapter_count": 1001}).status_code == 400


def test_outline_draft_ownership(temp_project):
    url = f"/internal/v1/projects/{temp_project}/outline-draft"
    body = {"premise": "少年追查玉佩真相", "chapter_count": 80}
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, headers=_h(uuid.uuid4()), json=body).status_code == 403
    assert client.post(f"/internal/v1/projects/{uuid.uuid4()}/outline-draft",
                       headers=_h(_demo_user_id()), json=body).status_code == 404


def test_put_outline_persists_volumes_and_stages(temp_project):
    body = {
        "objective": _OUTLINE["objective"],
        "volumes": _OUTLINE["volumes"],
        "premise": "少年得玉佩追寻真相",
        "chapter_count": 160,
        "storyline": "前期宗门、中期追查、后期决战",
    }
    resp = client.put(f"/internal/v1/projects/{temp_project}/outline",
                      headers=_h(_demo_user_id()), json=body)
    assert resp.status_code == 200
    saved = resp.json()["outline"]
    assert saved["objective"] == _OUTLINE["objective"]
    assert [v["volume_seq"] for v in saved["volumes"]] == [1, 2]
    v1 = saved["volumes"][0]
    assert v1["chapter_start"] == 1 and v1["chapter_end"] == 80
    assert [s["name"] for s in v1["stages"]] == ["前期", "中期", "后期"]
    assert v1["stages"][0]["beats"] == ["得玉佩", "遇苏瑶"]

    got = client.get(f"/internal/v1/projects/{temp_project}/outline",
                     headers=_h(_demo_user_id()))
    assert got.status_code == 200
    assert got.json()["outline"]["volumes"][0]["stages"][0]["goal"] == "入门试炼"

    with tenant_session(temp_project) as db:
        row = db.query(VolumeOutline).filter(VolumeOutline.volume_seq == 1).first()
        assert row is not None and row.outline["chapter_count"] == 160


def test_put_outline_legacy_chapters_become_stages(temp_project):
    body = {
        "objective": "终局",
        "volumes": [{
            "title": "第一卷",
            "goal": "入宗",
            "chapters": [{"seq": i, "title": f"第{i}章", "goal": "推进"} for i in range(1, 41)],
        }],
        "premise": "", "chapter_count": 40, "storyline": "",
    }
    saved = client.put(f"/internal/v1/projects/{temp_project}/outline",
                       headers=_h(_demo_user_id()), json=body).json()["outline"]
    stages = saved["volumes"][0]["stages"]
    assert len(stages) == 2
    assert stages[0]["chapter_start"] == 1
    assert stages[-1]["chapter_end"] == 40


def test_put_outline_drops_blank_volume(temp_project):
    body = {
        "objective": "终局",
        "volumes": [
            {"title": "有效卷", "goal": "推进", "chapter_start": 1, "chapter_end": 50,
             "stages": [{"name": "前期", "goal": "立身", "chapter_start": 1, "chapter_end": 50}]},
            {"title": "", "goal": "", "stages": []},
        ],
        "premise": "", "chapter_count": 50, "storyline": "",
    }
    saved = client.put(f"/internal/v1/projects/{temp_project}/outline",
                       headers=_h(_demo_user_id()), json=body).json()["outline"]
    assert len(saved["volumes"]) == 1
    assert saved["volumes"][0]["title"] == "有效卷"


def test_put_outline_overwrites_existing(temp_project):
    body = {"objective": "旧终局", "volumes": [
        {"title": "旧卷", "goal": "旧目标", "chapter_start": 1, "chapter_end": 50,
         "stages": [{"name": "本卷", "goal": "旧", "chapter_start": 1, "chapter_end": 50}]}],
             "premise": ""}
    client.put(f"/internal/v1/projects/{temp_project}/outline",
               headers=_h(_demo_user_id()), json=body)
    body2 = {"objective": "新终局", "volumes": [
        {"title": "新卷", "goal": "新目标", "chapter_start": 1, "chapter_end": 60,
         "stages": [{"name": "本卷", "goal": "新", "chapter_start": 1, "chapter_end": 60}]}],
              "premise": ""}
    client.put(f"/internal/v1/projects/{temp_project}/outline",
               headers=_h(_demo_user_id()), json=body2)
    got = client.get(f"/internal/v1/projects/{temp_project}/outline",
                     headers=_h(_demo_user_id())).json()
    assert got["outline"]["objective"] == "新终局"
    with tenant_session(temp_project) as db:
        assert db.query(VolumeOutline).count() == 1


def test_get_outline_empty_project_null(temp_project):
    resp = client.get(f"/internal/v1/projects/{temp_project}/outline",
                      headers=_h(_demo_user_id()))
    assert resp.status_code == 200
    assert resp.json() == {"outline": None}


def test_outline_ownership_matrix(temp_project):
    url = f"/internal/v1/projects/{temp_project}/outline"
    assert client.get(url).status_code == 403
    assert client.get(url, headers=_h(uuid.uuid4())).status_code == 403
    assert client.get(f"/internal/v1/projects/{uuid.uuid4()}/outline",
                      headers=_h(_demo_user_id())).status_code == 404
    body = {"objective": "", "volumes": []}
    assert client.put(url, json=body).status_code == 403
    assert client.put(url, headers=_h(uuid.uuid4()), json=body).status_code == 403


def test_normalize_outline_old_flat_shape_wrapped():
    old = {"arc": ["起", "承"], "chapters": [{"seq": 1, "title": "第一章", "goal": "入宗"},
                                            {"seq": 2, "title": "第二章", "goal": "试炼"}],
           "premise": "少年得玉佩"}
    out = normalize_outline(old)
    assert out["volumes"][0]["title"] == "全书主线"
    assert out["volumes"][0]["stages"]
    assert out["premise"] == "少年得玉佩"
    assert "arc" not in out
    assert normalize_outline(None) is None
    assert normalize_outline("nope") is None


def test_get_outline_normalizes_legacy_flat_shape(temp_project):
    with tenant_session(temp_project) as db:
        db.add(VolumeOutline(project_id=uuid.UUID(temp_project), volume_seq=1, title="全书大纲",
                             outline={"arc": ["起", "承"],
                                      "chapters": [{"seq": 1, "title": "旧章", "goal": "旧目标"}],
                                      "premise": ""}))
        db.commit()
    got = client.get(f"/internal/v1/projects/{temp_project}/outline",
                     headers=_h(_demo_user_id())).json()
    assert got["outline"]["volumes"][0]["title"] == "全书主线"
    assert got["outline"]["volumes"][0]["stages"][0]["goal"] == "旧目标"


def test_plan_messages_injects_outline_slice():
    ctx = {}
    outline = {
        "objective": "成为宗门长老并公开真相",
        "volume": {"volume_seq": 1, "title": "第一卷 · 青云山下", "goal": "入宗立足",
                   "chapter_start": 1, "chapter_end": 80,
                   "key_results": ["通过试炼", "拜入长老座下"], "end_event": "被迫离开青云宗"},
        "stage": {"name": "前期", "chapter_start": 1, "chapter_end": 30,
                  "goal": "入门试炼", "beats": ["得玉佩", "遇苏瑶"]},
    }
    msgs = prompts.plan_messages(ctx, None, outline=outline)
    sys_content = msgs[0]["content"]
    assert "全书 Objective" in sys_content and "成为宗门长老并公开真相" in sys_content
    assert "当前卷 · 第一卷 · 青云山下" in sys_content
    assert "通过试炼" in sys_content and "被迫离开青云宗" in sys_content
    assert "当前阶段 · 前期" in sys_content
    assert "得玉佩" in sys_content
    assert "以已写正文为准" in sys_content


def test_plan_messages_no_outline_no_section():
    msgs = prompts.plan_messages({}, None)
    assert "全书 Objective" not in msgs[0]["content"]


def test_write_messages_injects_outline_and_no_prev_opening():
    ctx = {
        "short_context": [{"kind": "prev_chapter_summary", "chapter": 2, "summary": "第二章摘要"}],
    }
    outline = {"volume": {"volume_seq": 1, "title": "第一卷 · 青云山下", "goal": "入宗立足",
                          "key_results": ["通过试炼"]},
               "stage": {"name": "前期", "chapter_start": 1, "chapter_end": 30,
                         "goal": "入门试炼", "beats": ["遇袭"]}}
    msgs = prompts.write_messages(ctx, {}, outline=outline)
    sys_content = msgs[0]["content"]
    user_content = msgs[1]["content"]
    assert "当前卷 · 第一卷 · 青云山下" in sys_content
    assert "当前阶段 · 前期" in sys_content
    assert "入门试炼" in sys_content
    assert "本阶段" in sys_content
    assert "prev_chapter_opening" not in user_content
    assert "严禁与之重复" not in user_content
    assert "清晨的雾气" not in user_content


def test_outline_prompt_contract():
    sys_outline = prompts.SYSTEM_BOOK_OUTLINE
    assert "自行推导" in sys_outline
    assert "禁止" in sys_outline and "逐章" in sys_outline
    assert "30 章" in sys_outline
    assert "不得复述" in sys_outline
    user = prompts.book_outline_messages("修仙", "少年得玉佩", 200, "", genre_pack=None)[1]["content"]
    assert "分卷约束" in user
    assert str(STAGE_SPAN) in user


def test_write_prompt_opening_contract():
    sys_write = prompts.SYSTEM_WRITE
    assert "上一章结尾片段" in sys_write
    assert "作万能开场" in sys_write
    assert "不得与其他章共用开场景" in sys_write
    assert "唤醒" in sys_write
    assert "静止收束" in sys_write


def test_plan_prompt_repetition_contract():
    sys_plan = prompts.SYSTEM_PLAN
    assert "开场节拍不得与上一章开场动作重复" in sys_plan
    assert "静止收束" in sys_plan
    assert "照搬" in sys_plan

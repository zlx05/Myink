"""共享测试设施：复用 test_flow 的活库 + 假 provider 模式（阶段 2 worker/API 测试）。

test_flow.py 自带同名 fixtures（模块级优先于 conftest），此处 re-export 仅服务新测试文件；
不修改既有测试文件（外科手术式改动）。
"""

from __future__ import annotations

import os

# 扫榜默认关（§10）：图节点集成测试不触外网。DaoSearch 不可达会让 mcp initialize 挂起/
# 被内部 cancel scope 取消（CancelledError 不降级直接炸节点）；且 test_multiprocess 的子
# 进程是全新 import（monkeypatch 传播不到）。必须在 myink.config 首次导入前设（settings
# 是 frozen dataclass 模块级单例）。rankings 特性测试自带 FakeSettings/monkeypatch，不受影响。
os.environ["RANKINGS_ENABLED"] = "0"
# RabbitMQ 测试隔离（阶段 6）：QUEUE_PREFIX=-mp- 让本套件发布/消费全走 queue:tasks-mp-，
# 不碰开发栈无前缀真实队列（避免测试消息污染生产队列/被真实 worker 抢走）。
# 必须在 myink.config 首次导入前设（settings 是 frozen 单例）——单在 test_multiprocess.py
# 模块级设已太晚：conftest 的 myink.db 导入会先触发 settings 冻结。
os.environ["QUEUE_PREFIX"] = "-mp-"
# 本地 compose 对外暴露的 RabbitMQ 用户。显式传入的 CI/开发环境配置仍优先。
os.environ.setdefault("AMQP_URL", "amqp://myink:myink@localhost:5672/")

import uuid

import pytest
from sqlalchemy import delete as sa_delete, select as sa_select

from myink.db import new_session
from myink.models import AgentRun, Project, ProjectSettings

from test_flow import (  # noqa: F401  (re-export fixtures/StubProvider)
    FakeEmbedder,
    StubProvider,
    fake_embedder,
    project_id,
    stub_provider,
)


@pytest.fixture
def temp_project():
    """每测试独立临时书（复制 demo 的 Project+ProjectSettings，§13 多书）。

    写保护（§11 顺序约束）要求章节只能写「已写最大章+1」；demo 已写到 ch-55，
    共享 demo 会让测试的固定 seq 被拦截且顺序耦合。临时书 max_seq=0 → next=1，
    测试用 seq=1 即可无耦合跑 worker 机制验证。用毕删书（FK 级联子表 + agent_runs 手动）。
    """
    with new_session() as db:
        row = db.execute(sa_select(Project).where(Project.title == "九州问天")).scalars().first()
        assert row is not None, "请先运行 `myink init`"
        demo = row
        b = Project(user_id=demo.user_id, title=f"test书-{uuid.uuid4().hex[:6]}",
                    genre=demo.genre, target_words=demo.target_words)
        db.add(b)
        db.flush()
        ds = db.execute(sa_select(ProjectSettings).where(
            ProjectSettings.project_id == demo.id)).scalar_one_or_none()
        if ds is not None:
            db.add(ProjectSettings(
                project_id=b.id, world_rules=ds.world_rules, style_profile=ds.style_profile,
                skill_pack=ds.skill_pack, genre_pack=getattr(ds, "genre_pack", None) or {},
                model_routes=ds.model_routes,
                hard_constraints=ds.hard_constraints, version=1))
        db.commit()
        pid = str(b.id)
    yield pid
    with new_session() as db:
        db.execute(sa_delete(AgentRun).where(AgentRun.project_id == pid))
        db.execute(sa_delete(ProjectSettings).where(ProjectSettings.project_id == pid))
        db.execute(sa_delete(Project).where(Project.id == pid))
        db.commit()

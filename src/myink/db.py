"""数据库访问层。

多租户隔离核心（plan.md §14.1）：
- RLS 主强制：所有业务表 FORCE ROW LEVEL SECURITY，策略读 `current_setting('app.tenant_id')`；
- 连接级上下文：`SET LOCAL app.tenant_id = :id` 只在当前事务生效，事务结束自动恢复，
  连接池复用时不泄漏上一位用户的租户（坑 1：连接池污染）；
- fail closed：未设置租户则 RLS 策略返回空集，任何查询空结果，不返回他人数据。
  坑 2（自定义 GUC 默认值）：任意 SET LOCAL 提交后，该连接会话级 `app.tenant_id` 恢复为
  自定义变量的默认空串 `''`（而非 NULL）——策略必须用 NULLIF(...,'') 判空，否则
  `''::uuid` 抛 DataError，而不是 fail closed（见下方 CREATE POLICY）。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from myink.config import settings
from myink.db_url import normalize_localhost_database_url

# 生产用连接池配置（plan.md §5.2 真实边界：连接池）。
# pool_size * 并发 worker < PG max_connections(默认 100)。
_engine: Engine = create_engine(
    normalize_localhost_database_url(settings.database_url),
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True,  # 回收失效连接
)

# 管理连接：DDL，以及 require_admin 之后 SET TRANSACTION READ ONLY 的报告查询。
# 普通业务/worker 不使用（超级用户绕过 RLS）；报告角色的部署限制见 routes_admin.py。
_admin_engine: Engine = create_engine(
    normalize_localhost_database_url(settings.admin_database_url), pool_pre_ping=True)

_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)


def get_engine() -> Engine:
    return _engine


def get_admin_engine() -> Engine:
    return _admin_engine


def session_factory() -> sessionmaker[Session]:
    return _SessionLocal


def new_session() -> Session:
    return _SessionLocal()


@contextmanager
def tenant_session(tenant_id: str | uuid.UUID) -> Iterator[Session]:
    """带租户上下文的会话：事务级 SET LOCAL，RLS 策略据此过滤。

    用法：`with tenant_session(project_id) as db: ...` —— 会话内所有查询自动按租户隔离
    （RLS USING(project_id = current_setting('app.tenant_id'))，§14.1）。
    """
    session = _SessionLocal()
    try:
        # set_config(..., is_local=true) 等价 SET LOCAL：事务级，事务结束自动恢复（§14.1 防连接池污染）
        session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---- RLS 主强制（§14.1：默认拒绝 + 强制集中，非每处自觉带 where）----

def enable_row_level_security() -> None:
    """为所有带 project_id 的业务表启用 FORCE RLS + 租户策略。

    未设置 app.tenant_id 时策略返回空集 → 查询空结果（fail closed）。
    users / projects 是租户根表，不走此策略（应用层归属校验，§14.1）。

    注意：必须用超级用户（owner）连接执行——ALTER TABLE FORCE 只允许 owner/超级；
    且应用连接角色必须 NOBYPASSRLS（超级用户永远绕过 RLS，§14.1 坑 3）。
    """
    from sqlalchemy import inspect

    with _admin_engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        inspector = inspect(conn)
        # 观测/队列表不参与 RLS（§14 隔离清单：agent_runs/tasks 无租户语义，运营查询用普通连接）
        _NO_RLS_TABLES = {"agent_runs", "tasks"}
        tables = [t for t in inspector.get_table_names()
                  if t not in ("users", "projects", "alembic_version", *_NO_RLS_TABLES)]
        # 撤销观测表已存在的 RLS（幂等）
        for no_rls in _NO_RLS_TABLES:
            if no_rls in inspector.get_table_names():
                conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {no_rls}"))
                conn.execute(text(f"ALTER TABLE {no_rls} DISABLE ROW LEVEL SECURITY"))
        for t in tables:
            cols = {c["name"] for c in inspector.get_columns(t)}
            if "project_id" not in cols:
                continue
            conn.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY"))
            conn.execute(text(
                f"DROP POLICY IF EXISTS tenant_isolation ON {t}"
            ))
            # NULLIF(...,'') 判空（坑 2）：SET LOCAL 提交后连接会话级 tenant 是 '' 不是 NULL，
            # 裸 current_setting 的 ::uuid 会 DataError。与 models/base.py myink.tenant_id() 同口径。
            conn.execute(text(
                f"CREATE POLICY tenant_isolation ON {t} "
                "USING (NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL "
                "AND project_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
            ))


# ---- 组合索引 + 向量 ANN 索引（评审 A6 同款：模型已声明，create_all 不改已有表）----

def ensure_storage_indexes() -> None:
    """为活库补齐组合索引与 HNSW（幂等，评审存储建议）。

    create_all 对已存在的表不会补索引，故建模后需显式 CREATE INDEX。每枚索引
    先查 pg_indexes 判存在，幂等可重跑；存在重复索引名时跳过不报错。
    """
    from sqlalchemy import text as _text

    _INDEX_DDL = {
        # 与 models 声明同名同列：组合索引前缀 = 查询里的等值/范围前缀（§14.1 RLS 使
        # 所有查询恒带 project_id，故前缀必是 project_id，评审建议列核对后仅留真实热键）
        "ix_character_states_project_char_seq":
            "CREATE INDEX ix_character_states_project_char_seq "
            "ON character_states (project_id, character_id, chapter_seq)",
        "ix_events_project_chapter":
            "CREATE INDEX ix_events_project_chapter ON events (project_id, source_chapter)",
        "ix_relations_project_source":
            "CREATE INDEX ix_relations_project_source ON relations (project_id, source_id)",
        "ix_relations_project_target":
            "CREATE INDEX ix_relations_project_target ON relations (project_id, target_id)",
        "ix_embeddings_project_level":
            "CREATE INDEX ix_embeddings_project_level ON embeddings (project_id, level)",
        # ANN（§5.2）：pgvector ≥0.5 的 HNSW，cosine 与 PgvectorStore.search 的
        # cosine_distance 对齐；显式 WHERE project_id 使扫描限定本租户向量集（§14.1 坑 1）
        "ix_embeddings_embedding_hnsw":
            "CREATE INDEX ix_embeddings_embedding_hnsw ON embeddings "
            "USING hnsw (embedding vector_cosine_ops)",
        # agent_runs 无 RLS（观测表，§14 清单），查询按 task_id 前缀 + id 增量扫，
        # 前缀用 task_id 而非 project_id（评审建议的 project_id 前缀对真实查询无益）
        "ix_agent_runs_task_id":
            "CREATE INDEX ix_agent_runs_task_id ON agent_runs (task_id, id)",
        # writing_lessons（§8.9 reflexion）：在效经验列出 / 复发率按 category 匹配
        "ix_writing_lessons_project_status":
            "CREATE INDEX ix_writing_lessons_project_status "
            "ON writing_lessons (project_id, status)",
        "ix_writing_lessons_project_category":
            "CREATE INDEX ix_writing_lessons_project_category "
            "ON writing_lessons (project_id, category)",
        # 跨批同内容去重（仅 active 在效）：同一本书同一条经验只一条
        "uq_writing_lessons_active_content":
            "CREATE UNIQUE INDEX uq_writing_lessons_active_content "
            "ON writing_lessons (project_id, content_hash) WHERE status = 'active'",
    }
    with _admin_engine.begin() as conn:
        existing = {
            r[0] for r in conn.execute(_text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            ))
        }
        for name, ddl in _INDEX_DDL.items():
            if name not in existing:
                conn.execute(_text(ddl))


def ensure_project_creation() -> None:
    """Additive upgrade; classify abandoned empty books only on the first upgrade."""
    with get_admin_engine().begin() as conn:
        existed = conn.scalar(text("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                                   "WHERE table_schema=current_schema() AND table_name='projects' "
                                   "AND column_name='creation_status')"))
        conn.execute(text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS creation_status "
                          "VARCHAR(24) NOT NULL DEFAULT 'legacy_ready'"))
        conn.execute(text("ALTER TABLE projects ADD COLUMN IF NOT EXISTS creation_context "
                          "JSON NOT NULL DEFAULT '{}'"))
        if not existed:
            conn.execute(text("UPDATE projects p SET creation_status='draft' "
                              "WHERE p.current_chapter=0 "
                              "AND NOT EXISTS (SELECT 1 FROM chapters c WHERE c.project_id=p.id) "
                              "AND NOT EXISTS (SELECT 1 FROM volume_outlines v WHERE v.project_id=p.id)"))


def ensure_user_tier() -> None:
    """为老库补齐 users.tier（幂等，阶段 6 VIP 优先级）。

    create_all 对已存在的表不会补列——老 demo 库需显式 ADD COLUMN IF NOT EXISTS
    （新建库模型已含该列，幂等无副作用）；server_default 保证存量行落为 normal。
    """
    with _admin_engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS tier VARCHAR(16) "
            "NOT NULL DEFAULT 'normal'"
        ))


def ensure_user_role() -> None:
    """Add the independent user/admin role to existing installations."""
    with _admin_engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(16) "
            "NOT NULL DEFAULT 'user'"
        ))
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_users_user_role'
                      AND conrelid = 'users'::regclass
                ) THEN
                    ALTER TABLE users ADD CONSTRAINT ck_users_user_role
                    CHECK (role IN ('user', 'admin'));
                END IF;
            END $$;
        """))


def ensure_genre_pack() -> None:
    """为老库补齐 project_settings.genre_pack（幂等，本书题材包快照）。"""
    with _admin_engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE project_settings ADD COLUMN IF NOT EXISTS genre_pack JSON "
            "NOT NULL DEFAULT '{}'"
        ))


def ensure_user_environment() -> None:
    """为老库补齐 users.environment（幂等，账号级模型连接/扫榜配置）。

    create_all 对已存在的表不会补列——活 demo 库需显式 ADD COLUMN IF NOT EXISTS；
    新建库模型已含该列，幂等无副作用。默认空对象，密钥由前端环境配置页写入。
    """
    with _admin_engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS environment JSONB "
            "NOT NULL DEFAULT '{}'::jsonb"
        ))


def ensure_user_auth_schema() -> None:
    """Add password authentication columns and canonical username uniqueness.

    This is deliberately separate from ``init``'s historical cleanup so an existing
    installation can upgrade accounts without dropping or rewriting user content.
    Legacy accounts remain passwordless until an administrator resets their password.
    If trimmed, case-folded legacy names collide, the whole transaction fails with an
    actionable error; no account is merged, renamed, or deleted.
    """
    with _admin_engine.begin() as conn:
        _upgrade_user_auth_schema(conn)


def _upgrade_user_auth_schema(conn) -> None:
    """Run the auth upgrade in the caller's transaction (also enables safe tests)."""
    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT"))
    conn.execute(text(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_version INTEGER "
        "NOT NULL DEFAULT 1"
    ))
    collisions = conn.execute(text("""
        SELECT lower(btrim(username)) AS canonical
        FROM users
        GROUP BY lower(btrim(username))
        HAVING count(*) > 1
        ORDER BY canonical
        LIMIT 10
    """)).scalars().all()
    if collisions:
        joined = ", ".join(repr(name) for name in collisions)
        raise RuntimeError(
            "users contain duplicate normalized usernames; resolve them manually "
            f"before retrying auth-upgrade: {joined}"
        )
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_canonical "
        "ON users ((lower(btrim(username))))"
    ))


def ensure_unique_constraints() -> None:
    """为活库补齐 create_all 不会 ALTER 的唯一约束（幂等，阶段 5 迁移链前的过渡）。

    模型 __table_args__ 的 UniqueConstraint 只对新建表生效；老库（create_all 建）
    缺约束需显式 ALTER。用 pg_constraint 查重保证幂等；库内已有重复行时 ALTER
    会如实报错（此时应先人工去重，不能静默吞掉）。
    """
    with _admin_engine.begin() as conn:
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_chapters_project_seq') THEN
                    ALTER TABLE chapters ADD CONSTRAINT uq_chapters_project_seq UNIQUE (project_id, chapter_seq);
                END IF;
            END $$;
        """))


def ensure_memory_candidate_kinds() -> None:
    """补齐 memory_candidates.kind CHECK（新增 memory_removal 校正删除候选，阶段 3 编辑校正）。

    create_all 只对新建表生效、不更新已存在表的 CHECK——老库 kind_enum 缺 memory_removal，
    需显式重建（DROP + ADD，事务内原子）。幂等：新库模型已含 memory_removal，重建后结果一致；
    重跑先 DROP IF EXISTS 再 ADD，不抛错。
    """
    _KINDS = ("event", "fact", "character_state", "relation_change",
              "foreshadow", "chapter_summary", "memory_removal",
              "character_card", "new_entity")
    # 约束全名按命名约定 ck_%(table)s_%(constraint)s（base.py convention）：
    with get_admin_engine().begin() as conn:
        conn.execute(text("ALTER TABLE memory_candidates ADD COLUMN IF NOT EXISTS review JSON"))
    # memory_candidates.kind_enum → ck_memory_candidates_kind_enum
    with _admin_engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE memory_candidates DROP CONSTRAINT IF EXISTS ck_memory_candidates_kind_enum"
        ))
        conn.execute(text(
            f"ALTER TABLE memory_candidates ADD CONSTRAINT ck_memory_candidates_kind_enum "
            f"CHECK (kind IN {_KINDS})"
        ))


def ensure_global_audit_reports() -> None:
    """幂等建表 + RLS（阶段 3 长线治理：global_audit_reports）。

    create_all 只建不存在的表（老库跑过 init 的缺新表），此处单表显式补齐，活 demo 库
    不用破坏性重初始化；索引随 create_all 建（新表）。RLS 由 enable_row_level_security
    全表迭代覆盖（带 project_id 即强制隔离），幂等可重跑。
    """
    from myink.models import GlobalAuditReport
    from myink.models.base import Base

    with _admin_engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[GlobalAuditReport.__table__])
    enable_row_level_security()


def ensure_chapter_versions() -> None:
    """幂等建表 + RLS（阶段 4 章节版本表 chapter_versions）。

    与 ensure_global_audit_reports 同款：老 demo 库补建新表（含索引，create_all 对新建表
    生效），RLS 由 enable_row_level_security 全表迭代覆盖（带 project_id 即强制隔离）。
    """
    from myink.models import ChapterVersion
    from myink.models.base import Base

    with _admin_engine.begin() as conn:
        Base.metadata.create_all(conn, tables=[ChapterVersion.__table__])
        # 同章版本号唯一约束（评审 M2 兜底）：create_all 只对新建表建约束，老表需显式补。
        # 库内已有重复行时 ALTER 如实报错（与 ensure_unique_constraints 同口径，先人工去重）。
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conname = 'uq_chapter_versions_chapter_version') THEN
                    ALTER TABLE chapter_versions
                    ADD CONSTRAINT uq_chapter_versions_chapter_version UNIQUE (chapter_id, version);
                END IF;
            END $$;
        """))
    enable_row_level_security()


def ensure_legacy_schema_cleanup() -> None:
    """幂等清理已死的表/列（老库遗留；新库压根不建）。

    本仓库没有 alembic 迁移链——建表唯一来源是 `Base.metadata.create_all`，而它**只建新表、
    不改已有表**，删表删列同样不管。所以模型侧删掉的东西必须在老库（dev 栈 + 用户已有
    作品库）显式 DROP，否则结构里永远留着它，只是没人再读写。`IF EXISTS` 保证新库/重跑
    无副作用，连跑两次 init 也不报错。

    清的都是「没有主人的契约」：无写入者、无读取者，却仍出现在表结构里误导后来的人。
    唯一例外是 `plot_threads.open_duration`（代码用 last_progress_chapter 现算）和
    `events.promoted_to_fact`（曾被前端渲染成「已入事实」徽章，值恒 false）——它们有
    读取者，读到的却永远是假值，比没有更坏。
    """
    dropped_columns = (
        # 别名有活着的替身：aliases 表（名字 → canonical id 归一化，repository.get_character）
        ("characters", "aliases"),
        ("factions", "members"),
        ("facts", "version"),
        ("events", "location_id"),
        ("events", "timeline"),
        ("events", "related_threads"),
        ("events", "promoted_to_fact"),
        ("events", "version"),
        ("relations", "version"),
        ("entities", "version"),
        ("foreshadows", "related_entities"),
        ("plot_threads", "progress"),
        ("plot_threads", "participants"),
        ("plot_threads", "open_duration"),
        # 乐观锁的真身是 chapters.version（routes_chapters 的 409）与 project_settings.version，
        # 上面这些实体版本号从来没有写入者，删它们不影响那两处
        ("characters", "version"),
    )
    with _admin_engine.begin() as conn:
        # 章节计划表：0 行、生产代码零写入。计划的事实来源是 agent_runs.detail
        # （node=="plan_chapter"）+ LangGraph state，留这张表只会让人以为计划存在关系库里。
        conn.execute(text("DROP TABLE IF EXISTS chapter_outlines"))
        for table, column in dropped_columns:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))

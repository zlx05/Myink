"""Alembic 迁移环境（§17.2：迁移是部署步骤，镜像构建后、发布前执行）。

- 目标元数据：src/myink/models 全部表（autogenerate 依赖）；
- URL：从 myink.config 读取（复用 .env，密钥不进代码库）。
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from myink.config import settings
from myink.models import Base  # noqa: F401  导入全部模型注册到 metadata

config = context.config
# 迁移走超级用户（owner）连接：ALTER/CREATE TABLE 需要 owner/DDL 权限（§17.2 迁移是部署步骤）
config.set_main_option("sqlalchemy.url", settings.admin_database_url)

target_metadata = Base.metadata

# LangGraph checkpointer 内部表（§6.7）由 PostgresSaver.setup() 自建/自迁移，
# 不在我们的 metadata 里——排除掉，避免 `alembic check` 漂移门禁误报"待删除"。
_LANGGRAPH_TABLES = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}


def _include_name(name: str, type_: str, parent_names) -> bool:
    if type_ == "table" and name in _LANGGRAPH_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.admin_database_url,
        target_metadata=target_metadata,
        include_name=_include_name,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          include_name=_include_name)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

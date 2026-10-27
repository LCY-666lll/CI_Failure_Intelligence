"""配置 CI Failure Intelligence 的 Alembic 迁移环境。
告诉 Alembic：比较谁？怎么连接数据库？怎么执行迁移
"""

import asyncio
import sys
from logging.config import fileConfig

# Alembic 当前这次迁移任务的运行上下文
from alembic import context
from ci_failure_intelligence.core.config import PostgresSettings
from ci_failure_intelligence.infrastructure.database import create_database_url
from ci_failure_intelligence.infrastructure.persistence import models  # noqa: F401
from ci_failure_intelligence.infrastructure.persistence.base import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Alembic 根据命令行的 -c 参数创建 Config 对象。
config = context.config

# 如果存在 alembic.ini，就加载里面的日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 导入 models 后，ci_failure_events 已注册到共享 MetaData。
# Alembic 使用它比较 Python 模型与真实 PostgreSQL 结构。
target_metadata = Base.metadata

# 密码由 BaseSettings 从 CFI_POSTGRES_PASSWORD 或 .env 动态注入；
# Pylance 无法识别该运行时注入，因此只忽略这一处构造签名误报。
postgres_settings = PostgresSettings()  # pyright: ignore[reportCallIssue]

database_url = create_database_url(postgres_settings)


def run_migrations_offline() -> None:
    """不连接数据库，使用迁移版本生成 SQL 文本。"""

    # Offline 模式只需要数据库方言和 URL，不创建 Engine。
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        # 将迁移参数直接渲染进生成的 SQL，方便人工审查。
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # 自动生成迁移时同时比较字段类型和数据库默认值。
        compare_type=True,
        compare_server_default=True,
    )

    # Alembic 将本次迁移操作组织在一个迁移事务边界中。
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在 Alembic 的同步迁移上下文中执行迁移版本。

    Args:
        connection: 由异步 Engine 建立、再交给同步迁移 API 使用的连接。
    """

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """创建一次性异步 Engine，并执行在线数据库迁移。"""

    # Alembic 是短命命令，不复用 FastAPI 的长期连接池。
    connectable = create_async_engine(
        database_url,
        poolclass=pool.NullPool,
    )

    try:
        # 建立真实异步连接，再让 Alembic 在该连接上运行同步迁移 API。
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        # 成功或失败都释放 Engine，防止迁移命令残留数据库连接。
        await connectable.dispose()


def run_migrations_online() -> None:
    """使用当前操作系统兼容的事件循环执行在线迁移。"""

    if sys.platform == "win32":
        # Psycopg 异步连接在 Windows 上使用 Selector event loop。
        asyncio.run(
            run_async_migrations(),
            loop_factory=asyncio.SelectorEventLoop,
        )
        return

    # Linux 部署环境使用 asyncio 的默认事件循环。
    asyncio.run(run_async_migrations())


# --sql 使用 offline；真实检查、升级和自动生成使用 online。
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

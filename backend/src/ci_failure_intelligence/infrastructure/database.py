"""根据运行配置创建 PostgreSQL 异步 Engine。"""

# SQLAlchemy 的数据库连接描述对象
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from ci_failure_intelligence.core.config import Settings


def create_database_url(settings: Settings) -> URL:
    """根据已校验配置创建 PostgreSQL 异步连接地址。

    使用 SQLAlchemy URL 对象分别保存连接参数，避免手工拼接时遗漏
    密码中的 URL 特殊字符。

    Args:
        settings: 已完成环境读取和字段校验的后端运行配置，
            包含 PostgreSQL 地址、账号、密码和数据库名称。

    Returns:
        URL: 使用 Psycopg 异步方言的 SQLAlchemy 数据库地址。
            URL 的普通字符串表示会隐藏真实密码。
    """

    postgres_password = settings.postgres_password.get_secret_value()
    return URL.create(
        # SQLAlchemy 使用 PostgreSQL 方言生成 SQL，通过 Psycopg 3 与服务器通信
        drivername="postgresql+psycopg",
        username=settings.postgres_username,
        password=postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_database,
    )


def create_database_engine(settings: Settings) -> AsyncEngine:
    """创建供整个 FastAPI 进程共享的 PostgreSQL 异步 Engine，不会立即联网。

    创建 Engine 时只建立连接池管理对象，不会立即连接 PostgreSQL。
    第一次借用数据库连接时，连接池才通过 Psycopg 建立真实连接。

    Args:
        settings: 已完成环境读取和字段校验的后端运行配置，
            包含数据库地址、认证信息、超时和连接池参数。

    Returns:
        AsyncEngine: 配置完成但尚未强制建立网络连接的异步 Engine。
            调用方负责在应用关闭时等待 `dispose()`。
    """

    database_url = create_database_url(settings)

    return create_async_engine(
        database_url,
        # 长期保留的连接数量；连接按需建立，不是在启动时一次建满。
        pool_size=settings.postgres_pool_size,
        # 长期连接全部繁忙时，允许临时创建的额外连接数量。
        max_overflow=settings.postgres_max_overflow,
        # 每次从池中借出旧连接前检查它是否仍然有效。
        pool_pre_ping=True,
        # 数据库断开时，建立新连接最多等待指定秒数。
        connect_args={
            "connect_timeout": (settings.postgres_connect_timeout_seconds),
        },
        # 即使以后打开 SQL 日志，也隐藏 SQL 参数中的敏感内容。
        hide_parameters=True,
    )


def create_database_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """创建供整个 FastAPI 进程共享的异步 Session 工厂。

    Session 工厂本身不代表数据库连接或事务。每次调用该工厂时，
    才会创建一个独立 AsyncSession；Session 首次执行数据库操作时，
    再从 Engine 连接池借用连接。

    Args:
        engine: 当前 FastAPI 进程共享的 PostgreSQL 异步 Engine。

    Returns:
        async_sessionmaker[AsyncSession]: 已绑定 Engine 的 Session 工厂。
            调用方应为每次业务操作创建并关闭独立 Session。
    """

    return async_sessionmaker(
        bind=engine,
        # 明确让工厂创建异步Session，而不是同步Session。
        class_=AsyncSession,
        # 事务提交后保留已加载字段，避免普通属性访问触发隐式异步查询。
        expire_on_commit=False,
    )

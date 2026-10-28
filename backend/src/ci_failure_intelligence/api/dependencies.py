"""提供 HTTP 路由使用的 FastAPI 依赖。"""

from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_database_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI 给每个 HTTP 请求分配一个独立的数据库 Session，用完自动关闭

    Args:
        request: 当前 FastAPI 请求，用于取得所属应用实例中共享的
            Session 工厂。

    Yields:
        AsyncSession: 只属于当前请求的异步数据库 Session。
    """

    # FastAPI 的 app.state 是动态属性容器，静态检查器无法自动知道
    # 该属性已经由应用工厂保存为 AsyncSession 工厂。
    session_factory = cast(
        async_sessionmaker[AsyncSession],
        # create_app() → 保存 Session 工厂到 app.state
        # HTTP 请求 → Request 持有当前 app → 依赖从 request.app.state 取出工厂
        request.app.state.database_session_factory,
    )

    # session_factory()：调用工厂，创建新的 AsyncSession
    # async with 保证使用完毕后自动关闭 Session
    async with session_factory() as session:
        # 临时把 Session 交给 FastAPI，让接口使用；接口使用结束后再继续执行清理流程
        yield session

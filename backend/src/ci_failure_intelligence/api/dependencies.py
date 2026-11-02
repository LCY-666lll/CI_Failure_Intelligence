"""提供 HTTP 路由使用的 FastAPI 依赖。"""

import secrets
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ci_failure_intelligence.core.config import Settings

FAILURE_READ_UNAUTHORIZED_DETAIL = "失败详情读取凭据无效"

# 只负责解析 Authorization: Bearer <token>。
# auto_error=False 让项目统一处理缺少或错误凭据的响应。
failure_read_bearer = HTTPBearer(auto_error=False)


def require_failure_read_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(failure_read_bearer),
    ],
) -> None:
    """验证调用方是否持有失败详情读取令牌。

    Args:
        request: 当前 HTTP 请求，用于取得应用工厂保存的运行配置。
        credentials: FastAPI 从 Authorization 请求头解析出的 Bearer
            凭据；请求头缺失或认证方案不符合要求时可能为空。

    Raises:
        HTTPException: 请求没有提供有效的失败详情读取令牌。
    """

    # create_app() 已把当前应用的 Settings 保存到 app.state。
    # cast 只帮助静态检查器理解动态属性，不会修改运行时对象。
    settings = cast(Settings, request.app.state.settings)
    expected_token = settings.failure_read_token.get_secret_value()

    # 即使凭据缺失也执行一次比较，让所有失败分支使用同一判断出口。
    supplied_token = credentials.credentials if credentials is not None else ""
    token_matches = secrets.compare_digest(
        supplied_token,
        expected_token,
    )

    if credentials is None or not token_matches:
        # 依赖抛出异常后，FastAPI 不会进入数据库依赖或路由函数。
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=FAILURE_READ_UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )


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

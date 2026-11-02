"""FastAPI 的全局异常处理器,把 CI 失败事件的应用异常统一转换为 HTTP 响应。"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from ci_failure_intelligence.service.ci_failure_persistence import (
    CiFailurePayloadConflictError,
    CiFailurePersistenceUnavailableError,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
)
from ci_failure_intelligence.service.ci_run_list import (
    CiRunListUnavailableError,
)

CI_FAILURE_PAYLOAD_CONFLICT_DETAIL = "同一 CI 业务事件已经存在不同内容"
CI_FAILURE_PERSISTENCE_UNAVAILABLE_DETAIL = "CI 失败事件存储暂时不可用"

CI_FAILURE_QUERY_NOT_FOUND_DETAIL = "CI 失败事件不存在"
CI_FAILURE_QUERY_UNAVAILABLE_DETAIL = "CI 失败详情查询暂时不可用"
CI_RUN_LIST_UNAVAILABLE_DETAIL = "CI 事件列表查询暂时不可用"

# 获取当前模块的日志记录器
logger = logging.getLogger(__name__)


async def handle_ci_failure_payload_conflict(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """把业务身份对应不同内容的冲突转换为 HTTP 409。

    Args:
        request: 触发异常的 HTTP 请求。本处理器暂时不读取请求内容。
        error: FastAPI 捕获到的业务冲突异常。异常类型由注册规则保证。

    Returns:
        JSONResponse: 不泄露数据库事件 ID 的固定 409 响应。
    """

    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": CI_FAILURE_PAYLOAD_CONFLICT_DETAIL},
    )


async def handle_ci_failure_persistence_unavailable(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """把数据库持久化不可用转换为可重试的 HTTP 503。

    Args:
        request: 触发异常的 HTTP 请求。本处理器暂时不读取请求内容。
        error: FastAPI 捕获到的持久化异常，用于记录内部错误链。

    Returns:
        JSONResponse: 不泄露数据库内部信息的固定 503 响应。
    """

    # 对客户端隐藏 SQL 和连接信息，但在服务端日志中保留完整异常链。
    logger.error(
        "CI 失败事件持久化暂时不可用",
        exc_info=(type(error), error, error.__traceback__),
    )

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": CI_FAILURE_PERSISTENCE_UNAVAILABLE_DETAIL},
    )


async def handle_ci_failure_query_not_found(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """把事件不存在转换为不泄露内部数据的 HTTP 404。

    Args:
        request: 触发异常的 HTTP 请求，本处理器不读取请求内容。
        error: FastAPI 捕获到的事件不存在异常。

    Returns:
        JSONResponse: 使用固定错误信息的 404 响应。
    """

    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": CI_FAILURE_QUERY_NOT_FOUND_DETAIL},
    )


async def handle_ci_failure_query_unavailable(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """把数据库查询故障转换为固定的 HTTP 503。

    Args:
        request: 触发异常的 HTTP 请求，本处理器不读取请求内容。
        error: 查询服务抛出的不可用异常，用于记录内部错误链。

    Returns:
        JSONResponse: 不泄露数据库信息的固定 503 响应。
    """

    logger.error(
        "CI 失败详情查询暂时不可用",
        exc_info=(type(error), error, error.__traceback__),
    )

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": CI_FAILURE_QUERY_UNAVAILABLE_DETAIL},
    )


async def handle_ci_run_list_unavailable(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """把最近事件列表查询故障转换为固定 HTTP 503。

    Args:
        request: 触发异常的 HTTP 请求，本处理器不读取请求内容。
        error: 列表服务抛出的不可用异常，用于记录内部错误链。

    Returns:
        JSONResponse: 不泄露数据库信息的固定 503 响应。
    """

    logger.error(
        "CI 事件列表查询暂时不可用",
        exc_info=(type(error), error, error.__traceback__),
    )

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": CI_RUN_LIST_UNAVAILABLE_DETAIL},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """为 FastAPI 应用注册 CI 失败事件异常处理规则。
        “异常类型 → 处理函数”

    Args:
        app: 当前应用工厂刚创建、尚未开始处理请求的 FastAPI 实例。
    """

    app.add_exception_handler(
        CiFailurePayloadConflictError,
        handle_ci_failure_payload_conflict,
    )
    app.add_exception_handler(
        CiFailurePersistenceUnavailableError,
        handle_ci_failure_persistence_unavailable,
    )
    app.add_exception_handler(
        CiFailureQueryNotFoundError,
        handle_ci_failure_query_not_found,
    )
    app.add_exception_handler(
        CiFailureQueryUnavailableError,
        handle_ci_failure_query_unavailable,
    )
    app.add_exception_handler(
        CiRunListUnavailableError,
        handle_ci_run_list_unavailable,
    )

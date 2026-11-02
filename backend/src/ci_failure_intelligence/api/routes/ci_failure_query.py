"""提供 CI 事件列表、解析状态和结构化失败证据的只读接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ci_failure_intelligence.api.dependencies import (
    get_database_session,
    require_failure_read_token,
)
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureQueryResponse,
)
from ci_failure_intelligence.schemas.ci_run_list import (
    CiRunListResponse,
)
from ci_failure_intelligence.service.ci_failure_query import (
    get_ci_failure_details,
)
from ci_failure_intelligence.service.ci_run_list import (
    DEFAULT_CI_RUN_LIST_LIMIT,
    MAXIMUM_CI_RUN_LIST_LIMIT,
    list_recent_ci_runs,
)

router = APIRouter(
    prefix="/api/v1/ci-runs",
    tags=["ci-runs"],
)


@router.get(
    "",
    response_model=CiRunListResponse,
    dependencies=[Depends(require_failure_read_token)],
)
async def read_recent_ci_runs(
    # 没有合法读取令牌时，FastAPI不会进入本路由。
    session: Annotated[AsyncSession, Depends(get_database_session)],
    # Query负责把HTTP参数转换成int，并在超出范围时返回422。
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAXIMUM_CI_RUN_LIST_LIMIT,
        ),
    ] = DEFAULT_CI_RUN_LIST_LIMIT,
) -> CiRunListResponse:
    """返回最近接收的 CI 事件摘要和失败数量。

    Args:
        session: FastAPI 为本次请求创建的异步数据库 Session。
        limit: 本次最多返回的事件数量，允许范围为1到100。

    Returns:
        CiRunListResponse: 按接收时间和事件主键倒序排列的事件摘要。

    Raises:
        CiRunListUnavailableError: PostgreSQL 无法可靠完成列表查询。
    """

    return await list_recent_ci_runs(
        session,
        limit=limit,
    )


@router.get(
    "/{event_id}/failures",
    response_model=CiFailureQueryResponse,
    dependencies=[Depends(require_failure_read_token)],
)
async def read_ci_failure_details(
    event_id: Annotated[int, Path(gt=0)],
    session: Annotated[AsyncSession, Depends(get_database_session)],
) -> CiFailureQueryResponse:
    """返回指定 CI 事件的解析状态和有序失败明细。

    Args:
        event_id: ci_failure_events 表中的正整数事件主键。
        session: FastAPI 为本次请求创建的异步数据库 Session。

    Returns:
        CiFailureQueryResponse: 事件运行信息、解析状态及按照原 XML
            位置排序的 failure/error 明细。

    Raises:
        CiFailureQueryNotFoundError: 指定事件不存在。
        CiFailureQueryUnavailableError: PostgreSQL 无法可靠完成查询。
    """

    return await get_ci_failure_details(
        session,
        event_id,
    )

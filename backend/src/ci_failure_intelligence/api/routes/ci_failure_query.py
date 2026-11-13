"""提供 CI 事件列表、解析状态和结构化失败证据的只读接口。"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, Query, Request
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
from ci_failure_intelligence.schemas.failure_history import (
    CiFailureHistoryResponse,
)
from ci_failure_intelligence.service.ai_diagnosis_orchestration import (
    AiDiagnosisGraphContext,
)
from ci_failure_intelligence.service.ci_failure_history_query import (
    get_ci_failure_history,
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

@router.get(
    "/{event_id}/history",
    response_model=CiFailureHistoryResponse,
    dependencies=[Depends(require_failure_read_token)],
)
async def read_ci_failure_history(
    event_id: Annotated[int, Path(gt=0)],
    request: Request,
) -> CiFailureHistoryResponse:
    """返回指定 CI 事件的历史检索状态及核实后的历史错误。

    Args:
        event_id: 已由 FastAPI 校验为正整数的当前事件主键。
        request: 当前请求，用于取得应用启动时准备好的共享资源。

    Returns:
        CiFailureHistoryResponse: 当前解析状态、检索状态、
            检索范围是否截断，以及最多五条历史错误。

    Raises:
        CiFailureQueryNotFoundError: 当前事件不存在。
        CiFailureQueryUnavailableError: 当前事件或来源信息无法读取。
        ValueError: 检索输入、向量或历史来源数据不符合约束。
    """
    # 只读取应用启动时一次性组装的上下文，不重新创建任何资源。
    context = cast(
        AiDiagnosisGraphContext,
        request.app.state.ai_diagnosis_context,
    )

    return await get_ci_failure_history(
        event_id=event_id,
        session_factory=context.session_factory,
        embedding_semaphore=context.embedding_semaphore,
        retrieval_settings=context.retrieval_settings,
        qdrant_client=context.qdrant_client,
        embedding_model=context.embedding_model,
    )

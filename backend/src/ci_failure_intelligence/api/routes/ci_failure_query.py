"""提供 CI 失败状态和结构化失败证据的只读接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from ci_failure_intelligence.api.dependencies import (
    get_database_session,
    require_failure_read_token,
)
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureQueryResponse,
)
from ci_failure_intelligence.service.ci_failure_query import (
    get_ci_failure_details,
)

router = APIRouter(
    prefix="/api/v1/ci-runs",
    tags=["ci-runs"],
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
        CiFailureQueryResponse: 事件运行信息、解析状态及按照原 XML位置排序的 failure/error 明细。

    Raises:
        CiFailureQueryNotFoundError: 指定事件不存在。
        CiFailureQueryUnavailableError: PostgreSQL 无法可靠完成查询。
    """

    return await get_ci_failure_details(
        session,
        event_id,
    )

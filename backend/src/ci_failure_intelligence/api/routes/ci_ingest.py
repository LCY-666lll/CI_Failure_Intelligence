"""提供 CI 失败数据的 HTTP 接入路由。"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from ci_failure_intelligence.api.dependencies import get_database_session
from ci_failure_intelligence.schemas.ci_ingest import (
    CiFailureIngestRequest,
    CiFailureIngestResponse,
)
from ci_failure_intelligence.service.ci_failure_persistence import (
    persist_ci_failure_event,
)

router = APIRouter(prefix="/api/v1/ci-runs", tags=["ci-runs"])


@router.post(
    "/ingest",
    # 让 FastAPI按响应契约检查和序列化结果
    response_model=CiFailureIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_ci_failure(
    ingest_request: CiFailureIngestRequest,
    # 参数是 AsyncSession 类型，FastAPI 通过 get_database_session 提供它
    session: Annotated[AsyncSession, Depends(get_database_session)],
):
    """保存一次 CI 失败事件并回传其稳定运行身份。

    Args:
        ingest_request: 已通过严格类型和字段约束校验的 CI 失败请求。
        session: FastAPI 依赖为本次请求创建的独立数据库 Session。

    Returns:
        CiFailureIngestResponse: 表示原始 CI 事件已经提交到 PostgreSQL
            或确认相同事件早已存在的 accepted 响应。

    Raises:
        CiFailurePayloadConflictError: 相同业务身份已经保存了不同内容。
            该异常将在后续异常处理层转换为 HTTP 409。
    """

    # 服务返回前，内部数据库事务已经提交或确认原事件存在。
    await persist_ci_failure_event(session, ingest_request)

    return CiFailureIngestResponse(
        run_id=ingest_request.run.run_id,
        run_attempt=ingest_request.run.run_attempt,
        job_key=ingest_request.run.job_key,
    )

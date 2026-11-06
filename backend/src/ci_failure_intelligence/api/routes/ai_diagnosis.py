"""提供分别受读取权限和付费触发权限保护的 AI 诊断接口。"""

from typing import Annotated, cast

from fastapi import (
    APIRouter,
    Depends,
    Path,
    Request,
    Response,
    status,
)
from langchain_deepseek import ChatDeepSeek
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ci_failure_intelligence.api.dependencies import (
    get_database_session,
    require_ai_diagnosis_trigger_token,
    require_failure_read_token,
)
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.schemas.ai_diagnosis_api import (
    AiDiagnosisReadResponse,
    AiDiagnosisResponse,
)
from ci_failure_intelligence.service.ai_diagnosis_orchestration import (
    AiDiagnosisOrchestrationStatus,
    orchestrate_ai_diagnosis,
)
from ci_failure_intelligence.service.ai_diagnosis_query import (
    get_ai_diagnosis,
)

router = APIRouter(
    prefix="/api/v1/ci-runs",
    tags=["ai-diagnosis"],
)


@router.post(
    "/{event_id}/diagnosis",
    response_model=AiDiagnosisResponse,
    dependencies=[Depends(require_ai_diagnosis_trigger_token)],
    responses={
        # 这个接口除了正常的 200，还可能返回 202
        status.HTTP_202_ACCEPTED: {
            "model": AiDiagnosisResponse,
            "description": "相同事件的 AI 诊断正在处理中",
        }
    },
)
async def diagnose_ci_failure(
    event_id: Annotated[int, Path(gt=0)],
    request: Request,
    response: Response,
) -> AiDiagnosisResponse:
    """生成、复用或报告正在处理的 CI 失败诊断。

    Args:
        event_id: 准备诊断的 CI 失败事件主键。
        request: 当前请求，用于取得应用共享配置和基础设施对象。
        response: FastAPI 响应对象，用于动态设置 202 状态码。

    Returns:
        AiDiagnosisResponse: 新生成、缓存命中或正在处理的诊断状态。

    Raises:
        CiFailureQueryNotFoundError: CI 事件不存在。
        CiFailureQueryUnavailableError: 失败详情查询不可用。
        AiEvidenceBuildError: 当前事件没有可供模型分析的有效证据。
        AiDiagnosisPersistenceUnavailableError: 诊断任务状态不可用。
        AiDiagnosisProviderError: DeepSeek 暂时无法完成请求。
        AiDiagnosisStructuredOutputError: 模型结果不符合输出契约。
        AiDiagnosisEvidenceReferenceError: 模型引用了不存在的证据。
        AiDiagnosisLeaseLostError: 当前执行者已经失去写回权限。
    """

    # 应用工厂保存的是完整 Settings，其中包含 DeepSeek 运行配置。
    settings = cast(Settings, request.app.state.settings)

    # 编排服务需要创建三段相互独立的 Session，
    # 因此传入 Session 工厂，而不是单个请求 Session。
    session_factory = cast(
        async_sessionmaker[AsyncSession],
        request.app.state.database_session_factory,
    )
    # 整个 FastAPI 进程共享一个配置完成的模型客户端。
    deepseek_client = cast(
        ChatDeepSeek,
        request.app.state.deepseek_client,
    )

    result = await orchestrate_ai_diagnosis(
        session_factory=session_factory,
        client=deepseek_client,
        settings=settings,
        event_id=event_id,
    )

    if result.status is AiDiagnosisOrchestrationStatus.IN_PROGRESS:
        # 当前请求没有执行模型，只确认另一个有效租约仍在工作。
        response.status_code = status.HTTP_202_ACCEPTED

    return AiDiagnosisResponse(
        event_id=result.event_id,
        status=result.status.value,
        attempt_count=result.attempt_count,
        diagnosis=result.diagnosis,
        model_name=result.model_name,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        total_tokens=result.total_tokens,
        lease_expires_at=result.lease_expires_at,
    )

@router.get(
    "/{event_id}/diagnosis",
    response_model=AiDiagnosisReadResponse,
    dependencies=[Depends(require_failure_read_token)]
)
async def read_ai_diagnosis(
    event_id:Annotated[int,Path(gt=0)],
    session:Annotated[AsyncSession,Depends(get_database_session)]
) -> AiDiagnosisReadResponse:
    """读取指定事件当前保存的 AI 诊断状态。

    Args:
        event_id: 要读取的 CI 事件正整数主键。
        session: FastAPI 为本次请求提供的异步数据库 Session。

    Returns:
        AiDiagnosisReadResponse: 尚未开始、正在处理、
            已完成或失败的当前诊断状态。

    Raises:
        CiFailureQueryNotFoundError: 指定 CI 事件不存在。
        AiDiagnosisPersistenceUnavailableError: 诊断存储不可用，
            或已保存的诊断不符合当前响应契约。
    """

    return await get_ai_diagnosis(
        session=session,
        event_id=event_id
    )

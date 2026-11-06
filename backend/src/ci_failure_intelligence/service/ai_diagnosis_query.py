"""只读查询一个 CI 事件当前保存的 AI 诊断。"""

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_diagnosis import (
    CiFailureDiagnosisModel,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.schemas.ai_diagnosis_api import (
    AiDiagnosisReadResponse,
)
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisPersistenceUnavailableError,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
)


async def get_ai_diagnosis(
    session: AsyncSession,
    event_id: int,
) -> AiDiagnosisReadResponse:
    """读取事件当前诊断，不抢占任务或调用模型。

    Args:
        session: 调用方提供、仅用于本次查询的异步数据库 Session。
        event_id: 要读取的 CI 事件主键；进入函数前应为正整数。

    Returns:
        AiDiagnosisReadResponse: 事件尚未诊断、正在处理、
            已完成或已失败的当前数据库状态。

    Raises:
        CiFailureQueryNotFoundError: 指定 CI 事件不存在。
        AiDiagnosisPersistenceUnavailableError: 数据库查询失败，
            或已保存的诊断不符合当前响应契约。
    """

    statement = (
        select(
            CiFailureEventModel.id.label("event_id"),
            CiFailureDiagnosisModel.status,
            CiFailureDiagnosisModel.attempt_count,
            # 查询时对齐接口字段名，避免后面重复转换字段名称。
            CiFailureDiagnosisModel.diagnosis_json.label("diagnosis"),
            CiFailureDiagnosisModel.model_name,
            CiFailureDiagnosisModel.input_tokens,
            CiFailureDiagnosisModel.output_tokens,
            CiFailureDiagnosisModel.total_tokens,
            CiFailureDiagnosisModel.lease_expires_at,
            CiFailureDiagnosisModel.error_code,
            CiFailureDiagnosisModel.error_message,
            CiFailureDiagnosisModel.started_at,
            CiFailureDiagnosisModel.finished_at,
            CiFailureDiagnosisModel.updated_at,
        )
        # 从事件表出发，保留“事件存在但尚无诊断”的情况。
        .select_from(CiFailureEventModel)
        .outerjoin(
            CiFailureDiagnosisModel,
            CiFailureDiagnosisModel.event_id == CiFailureEventModel.id,
        )
        .where(CiFailureEventModel.id == event_id)
    )

    try:
        # 此处才向 PostgreSQL 发送查询；整个函数没有写入操作。
        result = await session.execute(statement)
        row = result.mappings().one_or_none()

        if row is None:
            raise CiFailureQueryNotFoundError(event_id)

        # 诊断表的 status 不允许为空，因此这里的 None 表示右表没有记录。
        if row["status"] is None:
            return AiDiagnosisReadResponse(
                event_id=event_id,
                status="not_started",
                attempt_count=0,
            )

        # JSONB字典会继续按嵌套的AiDiagnosis模型进行结构校验。
        response = AiDiagnosisReadResponse.model_validate(dict(row))

        if response.status == "completed" and response.diagnosis is None:
            raise AiDiagnosisPersistenceUnavailableError(
                f"已完成的 AI 诊断缺少结果：event_id={event_id}"
            )

        return response

    except (SQLAlchemyError, ValidationError) as error:
        # HTTP层接收项目异常，不依赖SQLAlchemy或Pydantic的内部错误。
        raise AiDiagnosisPersistenceUnavailableError(
            f"AI 诊断暂时无法读取：event_id={event_id}"
        ) from error

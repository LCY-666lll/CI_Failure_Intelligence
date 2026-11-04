"""管理 AI 诊断任务的原子抢占和当前租约。
判断当前请求能不能获得某个 CI 事件的 AI 诊断执行权。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_diagnosis import (
    CiFailureDiagnosisModel,
)
from ci_failure_intelligence.service.ai_diagnosis import (
    AiDiagnosisGenerationResult,
)

# 租约覆盖 DeepSeek 单次超时、一次重试和有限网络退避。
AI_DIAGNOSIS_LEASE_SECONDS = 120
# 与数据库 error_code 和 error_message 字段约束保持一致。
MAX_AI_DIAGNOSIS_ERROR_CODE_LENGTH = 100
MAX_AI_DIAGNOSIS_ERROR_MESSAGE_LENGTH = 2_000


class AiDiagnosisClaimStatus(StrEnum):
    """描述一次诊断任务抢占请求的稳定业务结果。"""

    CLAIMED = "claimed"
    CACHED = "cached"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, slots=True)
class AiDiagnosisCacheIdentity:
    """描述判断已有诊断能否复用的稳定缓存身份。"""

    prompt_version: str
    output_schema_version: str
    requested_model: str
    evidence_sha256: str
    model_config_sha256: str


@dataclass(frozen=True, slots=True)
class AiDiagnosisClaimResult:
    """返回一次原子抢占或已有任务读取结果。

    `CLAIMED` 时只有当前结果中的 `lease_token` 能用于后续写回。
    `CACHED` 时 `diagnosis_json` 保存已有结构化诊断。
    `IN_PROGRESS` 时不会暴露另一个执行者的 lease token。
    """

    event_id: int
    status: AiDiagnosisClaimStatus
    attempt_count: int

    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None

    diagnosis_json: dict[str, Any] | None = None
    model_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class AiDiagnosisPersistenceUnavailableError(RuntimeError):
    """表示 PostgreSQL 无法可靠完成 AI 诊断任务操作。"""


class AiDiagnosisFinalStatus(StrEnum):
    """描述诊断任务已经提交的最终状态。"""

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AiDiagnosisFinalizeResult:
    """返回已经由当前租约持有者提交的最终状态。"""

    event_id: int
    status: AiDiagnosisFinalStatus


class AiDiagnosisLeaseLostError(RuntimeError):
    """表示当前执行者的租约已经失效或被替换。"""

    def __init__(self, event_id: int) -> None:
        """记录无法再提交结果的 CI 事件。

        Args:
            event_id: 当前执行者原本负责诊断的 CI 事件主键。
        """

        self.event_id = event_id
        super().__init__(f"AI 诊断任务租约已经失效：event_id={event_id}")


def _cache_identity_matches(
    diagnosis: CiFailureDiagnosisModel,
    cache_identity: AiDiagnosisCacheIdentity,
) -> bool:
    """判断数据库中的完成结果能否服务当前诊断请求。

    Args:
        diagnosis: 已经从 PostgreSQL 读取的当前诊断记录。
        cache_identity: 本次实际证据、Prompt、Schema 和模型配置身份。

    Returns:
        bool: 所有缓存身份字段完全相同则为 True，否则为 False。
    """

    return (
        diagnosis.prompt_version == cache_identity.prompt_version
        and diagnosis.output_schema_version == cache_identity.output_schema_version
        and diagnosis.requested_model == cache_identity.requested_model
        and diagnosis.evidence_sha256 == cache_identity.evidence_sha256
        and diagnosis.model_config_sha256 == cache_identity.model_config_sha256
    )


def _build_claim_statement(
    event_id: int,
    cache_identity: AiDiagnosisCacheIdentity,
    lease_token: UUID,
):
    """构造由 PostgreSQL 原子执行的诊断任务抢占语句。

    Args:
        event_id: 已经存在并且具备可诊断失败证据的 CI 事件主键。
        cache_identity: 本次诊断的完整缓存身份。
        lease_token: 为本次抢占预先生成的随机 UUID。

    Returns:
        PostgreSQL INSERT ON CONFLICT 语句。执行后仅在真正取得
        新租约时通过 RETURNING 返回一行。
    """

    lease_expires_at = func.now() + timedelta(seconds=AI_DIAGNOSIS_LEASE_SECONDS)

    insert_statement = insert(CiFailureDiagnosisModel).values(
        event_id=event_id,
        status="processing",
        prompt_version=cache_identity.prompt_version,
        output_schema_version=cache_identity.output_schema_version,
        requested_model=cache_identity.requested_model,
        evidence_sha256=cache_identity.evidence_sha256,
        model_config_sha256=cache_identity.model_config_sha256,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        attempt_count=1,
        # JSONB 的 Python None 默认会编码成 JSON null；状态约束需要 SQL NULL。
        diagnosis_json=null(),
        model_name=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        error_code=None,
        error_message=None,
        started_at=func.now(),
        finished_at=None,
        updated_at=func.now(),
    )

    # excluded 表示本次准备插入的新值，不是数据库里已有的旧值。
    excluded = insert_statement.excluded

    # SQLAlchemy 用来构造 SQL OR 条件的函数：只要有一个字段不同，结果就是 True
    cache_identity_changed = or_(
        CiFailureDiagnosisModel.prompt_version != excluded.prompt_version,
        CiFailureDiagnosisModel.output_schema_version != excluded.output_schema_version,
        CiFailureDiagnosisModel.requested_model != excluded.requested_model,
        CiFailureDiagnosisModel.evidence_sha256 != excluded.evidence_sha256,
        CiFailureDiagnosisModel.model_config_sha256 != excluded.model_config_sha256,
    )

    # 旧记录是否允许被当前请求重新接管
    reclaimable = or_(
        # 已失败的任务允许显式重新尝试。
        CiFailureDiagnosisModel.status == "failed",
        # 旧执行者的 processing 租约已经失效。
        and_(
            CiFailureDiagnosisModel.status == "processing",
            CiFailureDiagnosisModel.lease_expires_at <= func.now(),
        ),
        # 已完成结果与本次实际诊断身份不同，需要生成新结果。
        and_(
            CiFailureDiagnosisModel.status == "completed",
            cache_identity_changed,
        ),
    )

    # 没有相同 event_id：直接插入。
    # 有相同 event_id：检查是否允许更新。
    # 不允许更新：不修改旧记录
    return insert_statement.on_conflict_do_update(
        # 以 event_id 作为冲突判断依据
        index_elements=[CiFailureDiagnosisModel.event_id],
        set_={
            "status": "processing",
            "prompt_version": excluded.prompt_version,
            "output_schema_version": excluded.output_schema_version,
            "requested_model": excluded.requested_model,
            "evidence_sha256": excluded.evidence_sha256,
            "model_config_sha256": excluded.model_config_sha256,
            "lease_token": lease_token,
            "lease_expires_at": lease_expires_at,
            "attempt_count": CiFailureDiagnosisModel.attempt_count + 1,
            # 新一轮执行开始后，必须清除旧结果和旧错误。
            "diagnosis_json": null(),
            "model_name": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "error_code": None,
            "error_message": None,
            "started_at": func.now(),
            "finished_at": None,
            "updated_at": func.now(),
        },
        where=reclaimable,
        # 指定数据库操作成功后返回哪些字段
    ).returning(
        CiFailureDiagnosisModel.event_id,
        CiFailureDiagnosisModel.lease_token,
        CiFailureDiagnosisModel.attempt_count,
        CiFailureDiagnosisModel.lease_expires_at,
    )


async def _claim_ai_diagnosis_in_transaction(
    session: AsyncSession,
    event_id: int,
    cache_identity: AiDiagnosisCacheIdentity,
) -> AiDiagnosisClaimResult:
    """在单个数据库事务中抢占任务或读取稳定现状。

    Args:
        session: 当前请求独享、尚未开启其他事务的异步 Session。
        event_id: 已确认存在并且具备失败证据的 CI 事件主键。
        cache_identity: 本次诊断的稳定缓存身份。

    Returns:
        AiDiagnosisClaimResult: 抢占成功、缓存命中或正在处理。

    Raises:
        AiDiagnosisPersistenceUnavailableError: 数据库记录不符合当前
            状态机，无法可靠解释本次抢占结果。
        SQLAlchemyError: SQL 执行或事务提交失败。
    """

    lease_token = uuid4()
    claim_statement = _build_claim_statement(
        event_id=event_id,
        cache_identity=cache_identity,
        lease_token=lease_token,
    )

    async with session.begin():
        claim_result = await session.execute(claim_statement)
        claimed_row = claim_result.one_or_none()

        if claimed_row is not None:
            return AiDiagnosisClaimResult(
                event_id=claimed_row.event_id,
                status=AiDiagnosisClaimStatus.CLAIMED,
                attempt_count=claimed_row.attempt_count,
                lease_token=claimed_row.lease_token,
                lease_expires_at=claimed_row.lease_expires_at,
            )

        # UPSERT 没有返回行，说明旧记录存在但当前不能重新抢占。
        current_result = await session.execute(
            select(CiFailureDiagnosisModel).where(
                CiFailureDiagnosisModel.event_id == event_id
            )
        )
        current_diagnosis = current_result.scalar_one_or_none()

        if current_diagnosis is None:
            raise AiDiagnosisPersistenceUnavailableError(
                f"AI 诊断任务状态无法确认：event_id={event_id}"
            )

        if (
            current_diagnosis.status == "completed"
            and _cache_identity_matches(
                current_diagnosis,
                cache_identity,
            )
            and current_diagnosis.diagnosis_json is not None
        ):
            return AiDiagnosisClaimResult(
                event_id=event_id,
                status=AiDiagnosisClaimStatus.CACHED,
                attempt_count=current_diagnosis.attempt_count,
                diagnosis_json=current_diagnosis.diagnosis_json,
                model_name=current_diagnosis.model_name,
                input_tokens=current_diagnosis.input_tokens,
                output_tokens=current_diagnosis.output_tokens,
                total_tokens=current_diagnosis.total_tokens,
            )

        if (
            current_diagnosis.status == "processing"
            and current_diagnosis.lease_expires_at is not None
        ):
            return AiDiagnosisClaimResult(
                event_id=event_id,
                status=AiDiagnosisClaimStatus.IN_PROGRESS,
                attempt_count=current_diagnosis.attempt_count,
                lease_expires_at=current_diagnosis.lease_expires_at,
            )

        raise AiDiagnosisPersistenceUnavailableError(
            f"AI 诊断任务处于无法解释的状态：event_id={event_id}"
        )


async def claim_ai_diagnosis(
    session: AsyncSession,
    event_id: int,
    cache_identity: AiDiagnosisCacheIdentity,
) -> AiDiagnosisClaimResult:
    """原子抢占一个 AI 诊断任务，或返回已有稳定状态。

    Args:
        session: 当前请求独享的异步数据库 Session。
        event_id: 已确认存在并且具备结构化失败证据的事件主键。
        cache_identity: 本次实际发送证据和诊断配置形成的缓存身份。

    Returns:
        AiDiagnosisClaimResult: `CLAIMED` 表示调用方取得租约；
            `CACHED` 表示应复用已有诊断；`IN_PROGRESS`
            表示另一个执行者仍持有有效租约。

    Raises:
        AiDiagnosisPersistenceUnavailableError: PostgreSQL 无法可靠完成
            抢占、事务提交或当前记录违反任务状态机。
    """

    try:
        return await _claim_ai_diagnosis_in_transaction(
            session=session,
            event_id=event_id,
            cache_identity=cache_identity,
        )
    except SQLAlchemyError as error:
        # 上层不依赖 SQLAlchemy 异常；异常链仍保留真实数据库原因。
        raise AiDiagnosisPersistenceUnavailableError(
            f"AI 诊断任务存储暂时不可用：event_id={event_id}"
        ) from error


def _active_lease_conditions(
    event_id: int,
    lease_token: UUID,
) -> tuple[ColumnElement[bool], ...]:
    """构造只有当前有效租约持有者才能满足的更新条件。

    Args:
        event_id: 当前正在处理的 CI 事件主键。
        lease_token: 抢占成功时返回给当前执行者的 UUID。

    Returns:
        tuple[ColumnElement[bool], ...]: 交给 SQLAlchemy UPDATE.where()
            的数据库布尔条件。
    """

    return (
        CiFailureDiagnosisModel.event_id == event_id,
        CiFailureDiagnosisModel.status == "processing",
        CiFailureDiagnosisModel.lease_token == lease_token,
        CiFailureDiagnosisModel.lease_expires_at > func.now(),
    )


async def complete_ai_diagnosis(
    session: AsyncSession,
    lease_token: UUID,
    generation_result: AiDiagnosisGenerationResult,
) -> AiDiagnosisFinalizeResult:
    """由当前有效租约持有者提交已经校验的结构化诊断。

    Args:
        session: 当前业务操作独享、尚未开启其他事务的异步 Session。
        lease_token: 抢占成功时取得的当前任务 UUID。
        generation_result: 已通过 Pydantic、结构化输出和证据引用
            校验的 DeepSeek 诊断及有限调用元数据。

    Returns:
        AiDiagnosisFinalizeResult: 已经提交的事件 ID 和 completed 状态。

    Raises:
        AiDiagnosisLeaseLostError: token 不属于当前任务，或者租约
            已经过期，当前执行者不得再覆盖数据库结果。
        AiDiagnosisPersistenceUnavailableError: PostgreSQL 无法可靠完成
            更新或事务提交。
    """

    event_id = generation_result.event_id

    statement = (
        update(CiFailureDiagnosisModel)
        .where(
            *_active_lease_conditions(
                event_id=event_id,
                lease_token=lease_token,
            )
        )
        .values(
            status=AiDiagnosisFinalStatus.COMPLETED.value,
            lease_token=None,
            lease_expires_at=None,
            # 只保存已经通过全部项目校验的结构化结果。
            # 把 Pydantic 模型转换成普通 Python 数据结构
            diagnosis_json=generation_result.diagnosis.model_dump(mode="json"),
            model_name=generation_result.model_name,
            input_tokens=generation_result.input_tokens,
            output_tokens=generation_result.output_tokens,
            total_tokens=generation_result.total_tokens,
            error_code=None,
            error_message=None,
            finished_at=func.now(),
            updated_at=func.now(),
        )
        .returning(CiFailureDiagnosisModel.event_id)
    )

    try:
        async with session.begin():
            result = await session.execute(statement)
            completed_event_id = result.scalar_one_or_none()

            if completed_event_id is None:
                raise AiDiagnosisLeaseLostError(event_id)

            return AiDiagnosisFinalizeResult(
                event_id=completed_event_id,
                status=AiDiagnosisFinalStatus.COMPLETED,
            )

    except SQLAlchemyError as error:
        raise AiDiagnosisPersistenceUnavailableError(
            f"AI 诊断完成结果暂时无法保存：event_id={event_id}"
        ) from error


def _normalize_failure_metadata(
    error_code: str,
    error_message: str,
) -> tuple[str, str]:
    """校验并限制准备写入数据库的安全失败摘要。

    Args:
        error_code: 由编排服务提供的稳定内部机器代码。
        error_message: 不含密钥和原始 provider 响应的排查摘要。

    Returns:
        tuple[str, str]: 去除首尾空白的错误代码，以及最长
            2000 字符的安全错误摘要。

    Raises:
        ValueError: 错误代码或摘要为空，或者错误代码超过数据库上限。
    """

    normalized_code = error_code.strip()
    normalized_message = error_message.strip()

    if not normalized_code:
        raise ValueError("AI 诊断失败代码不能为空")

    if len(normalized_code) > MAX_AI_DIAGNOSIS_ERROR_CODE_LENGTH:
        raise ValueError("AI 诊断失败代码超过数据库长度上限")

    if not normalized_message:
        raise ValueError("AI 诊断失败摘要不能为空")

    return (
        normalized_code,
        normalized_message[:MAX_AI_DIAGNOSIS_ERROR_MESSAGE_LENGTH],
    )


async def fail_ai_diagnosis(
    session: AsyncSession,
    event_id: int,
    lease_token: UUID,
    error_code: str,
    error_message: str,
) -> AiDiagnosisFinalizeResult:
    """由当前有效租约持有者提交诊断失败状态。

    Args:
        session: 当前业务操作独享、尚未开启其他事务的异步 Session。
        event_id: 本次诊断对应的 CI 事件主键。
        lease_token: 抢占成功时取得的当前任务 UUID。
        error_code: 由编排服务提供的稳定内部失败代码。
        error_message: 已避免密钥和原始 provider 响应的排查摘要。

    Returns:
        AiDiagnosisFinalizeResult: 已经提交的事件 ID 和 failed 状态。

    Raises:
        ValueError: 错误代码或摘要不符合内部持久化契约。
        AiDiagnosisLeaseLostError: 当前 token 已被替换或租约已过期。
        AiDiagnosisPersistenceUnavailableError: PostgreSQL 无法可靠完成
            更新或事务提交。
    """

    normalized_code, normalized_message = _normalize_failure_metadata(
        error_code=error_code,
        error_message=error_message,
    )

    statement = (
        update(CiFailureDiagnosisModel)
        .where(
            *_active_lease_conditions(
                event_id=event_id,
                lease_token=lease_token,
            )
        )
        .values(
            status=AiDiagnosisFinalStatus.FAILED.value,
            lease_token=None,
            lease_expires_at=None,
            # JSONB 必须使用真正的 SQL NULL，而不是 JSON null。
            diagnosis_json=null(),
            model_name=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            error_code=normalized_code,
            error_message=normalized_message,
            finished_at=func.now(),
            updated_at=func.now(),
        )
        .returning(CiFailureDiagnosisModel.event_id)
    )

    try:
        async with session.begin():
            result = await session.execute(statement)
            failed_event_id = result.scalar_one_or_none()

            if failed_event_id is None:
                raise AiDiagnosisLeaseLostError(event_id)

            return AiDiagnosisFinalizeResult(
                event_id=failed_event_id,
                status=AiDiagnosisFinalStatus.FAILED,
            )

    except SQLAlchemyError as error:
        raise AiDiagnosisPersistenceUnavailableError(
            f"AI 诊断失败状态暂时无法保存：event_id={event_id}"
        ) from error

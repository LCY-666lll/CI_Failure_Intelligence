"""编排一次具备缓存、租约和结果持久化能力的 AI 诊断。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from langchain_deepseek import ChatDeepSeek
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ci_failure_intelligence.core.config import DeepSeekSettings
from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis
from ci_failure_intelligence.schemas.ai_evidence import AiEvidencePack
from ci_failure_intelligence.service.ai_diagnosis import (
    AiDiagnosisProviderError,
    AiDiagnosisStructuredOutputError,
    generate_ai_diagnosis,
    serialize_ai_evidence_pack,
)
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisCacheIdentity,
    AiDiagnosisClaimStatus,
    AiDiagnosisPersistenceUnavailableError,
    claim_ai_diagnosis,
    complete_ai_diagnosis,
    fail_ai_diagnosis,
)
from ci_failure_intelligence.service.ai_diagnosis_validation import (
    AiDiagnosisEvidenceReferenceError,
)
from ci_failure_intelligence.service.ai_evidence import build_ai_evidence_pack
from ci_failure_intelligence.service.ci_failure_query import (
    get_ci_failure_details,
)

# 修改诊断规则时递增，避免复用旧 Prompt 产生的结果。
AI_DIAGNOSIS_PROMPT_VERSION = "v2"
# 修改 AiDiagnosis 的业务字段含义时递增。
AI_DIAGNOSIS_OUTPUT_SCHEMA_VERSION = "v2"


def build_ai_diagnosis_cache_identity(
    evidence_pack: AiEvidencePack, settings: DeepSeekSettings
) -> AiDiagnosisCacheIdentity:
    """根据实际模型输入和语义配置生成稳定缓存身份。
        让 PostgreSQL 知道旧诊断能不能复用

    Args:
        evidence_pack: 已完成脱敏、截断和资源限制的模型证据包。
        settings: 已校验的 DeepSeek 模型配置。

    Returns:
        AiDiagnosisCacheIdentity: 用于判断已有诊断能否复用的缓存身份。
    """

    evidence_json = serialize_ai_evidence_pack(evidence_pack)
    evidence_sha256 = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    # 只记录会影响模型诊断内容的配置。
    # 超时和重试只影响调用过程，不影响模型应该生成什么内容。
    model_config = {
        "max_output_tokens": settings.deepseek_max_output_tokens,
        "model": settings.deepseek_model,
        "structured_output_method": "json_mode",
        "thinking": "disabled",
    }
    model_config_json = json.dumps(
        model_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    model_config_sha256 = hashlib.sha256(model_config_json.encode("utf-8")).hexdigest()
    return AiDiagnosisCacheIdentity(
        prompt_version=AI_DIAGNOSIS_PROMPT_VERSION,
        output_schema_version=AI_DIAGNOSIS_OUTPUT_SCHEMA_VERSION,
        requested_model=settings.deepseek_model,
        evidence_sha256=evidence_sha256,
        model_config_sha256=model_config_sha256,
    )


class AiDiagnosisOrchestrationStatus(StrEnum):
    """描述一次 AI 诊断编排对调用方产生的业务结果。"""

    GENERATED = "generated"
    CACHED = "cached"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, slots=True)
class AiDiagnosisOrchestrationResult:
    """描述一次 AI 诊断编排的稳定业务结果。

    `generated` 和 `cached` 会携带诊断内容。
    `in_progress` 不返回其他执行者的租约，只返回租约到期时间。
    """

    event_id: int
    status: AiDiagnosisOrchestrationStatus
    attempt_count: int
    diagnosis: AiDiagnosis | None = None
    model_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    lease_expires_at: datetime | None = None


async def orchestrate_ai_diagnosis(
    session_factory: async_sessionmaker[AsyncSession],
    client: ChatDeepSeek,
    settings: DeepSeekSettings,
    event_id: int,
) -> AiDiagnosisOrchestrationResult:
    """读取失败证据，抢占租约并生成或复用 AI 诊断。

    Args:
        session_factory: 为每段数据库操作创建独立 Session 的工厂。
        client: 已配置完成的 DeepSeek LangChain 客户端。
        settings: 已校验的 DeepSeek 模型配置。
        event_id: 准备诊断的 CI 失败事件主键。

    Returns:
        AiDiagnosisOrchestrationResult: 新生成、缓存命中或正在处理。

    Raises:
        CiFailureQueryNotFoundError: 指定 CI 事件不存在。
        CiFailureQueryUnavailableError: 失败证据查询不可用。
        AiEvidenceNotReadyError: JUnit XML 尚未解析成功。
        AiEvidenceEmptyError: 事件没有可诊断的 failure/error。
        AiEvidencePackTooLargeError: 有限证据仍超过资源上限。
        AiDiagnosisPersistenceUnavailableError: 任务状态或缓存结果不可信。
        AiDiagnosisProviderError: DeepSeek 调用失败。
        AiDiagnosisStructuredOutputError: 模型没有返回合法结构。
        AiDiagnosisEvidenceReferenceError: 模型引用了不存在的证据。
        AiDiagnosisLeaseLostError: 结果写回前租约已经失效。
    """

    # 第一段数据库操作：只读取事件和失败明细。
    # 离开 async with 后，Session 和它借用的连接会归还连接池。
    async with session_factory() as session:
        event = await get_ci_failure_details(
            session=session,
            event_id=event_id,
        )

    # 下面两步都是纯 Python 计算，不访问数据库和外部模型。
    evidence_pack = build_ai_evidence_pack(event)
    cache_identity = build_ai_diagnosis_cache_identity(
        evidence_pack=evidence_pack,
        settings=settings,
    )
    # 第二段数据库操作：原子判断缓存、处理中状态或取得新租约。
    async with session_factory() as session:
        claim_result = await claim_ai_diagnosis(
            session=session, event_id=event_id, cache_identity=cache_identity
        )
    if claim_result.status is AiDiagnosisClaimStatus.CACHED:
        try:
            # 把字典重新交给 Pydantic 校验，并转换成模型对象
            cached_diagnosis = AiDiagnosis.model_validate(claim_result.diagnosis_json)
        except ValidationError as error:
            # 数据库中的 completed 结果必须仍符合当前输出模型。
            raise AiDiagnosisPersistenceUnavailableError(
                f"AI 诊断缓存内容无效：event_id={event_id}"
            ) from error
        return AiDiagnosisOrchestrationResult(
            event_id=event_id,
            status=AiDiagnosisOrchestrationStatus.CACHED,
            attempt_count=claim_result.attempt_count,
            diagnosis=cached_diagnosis,
            model_name=claim_result.model_name,
            input_tokens=claim_result.input_tokens,
            output_tokens=claim_result.output_tokens,
            total_tokens=claim_result.total_tokens,
        )
    if claim_result.status is AiDiagnosisClaimStatus.IN_PROGRESS:
        return AiDiagnosisOrchestrationResult(
            event_id=event_id,
            status=AiDiagnosisOrchestrationStatus.IN_PROGRESS,
            attempt_count=claim_result.attempt_count,
            lease_expires_at=claim_result.lease_expires_at,
        )

    # 如果前两条分支都没返回，说明本次抢占成功。
    # 拿到数据库返回的当前租约令牌.
    lease_token = claim_result.lease_token

    if lease_token is None:
        raise AiDiagnosisPersistenceUnavailableError(
            f"AI 诊断任务已抢占但缺少租约：event_id={event_id}"
        )

    try:
        # 此处没有打开数据库 Session。
        # DeepSeek 等待、超时和重试不会长期占用 PostgreSQL 连接。
        generation_result = await generate_ai_diagnosis(
            client=client,
            evidence_pack=evidence_pack,
        )
    except (
        # DeepSeek 网络、超时或服务故障
        AiDiagnosisProviderError,
        # 模型输出无法形成合法结构
        AiDiagnosisStructuredOutputError,
        # 模型引用了不存在的 evidence_id
        AiDiagnosisEvidenceReferenceError,
    ) as error:
        if isinstance(error, AiDiagnosisProviderError):
            error_code = "provider_unavailable"
            error_message = "DeepSeek 暂时无法完成本次诊断"
        elif isinstance(error, AiDiagnosisStructuredOutputError):
            error_code = "invalid_structured_output"
            error_message = "DeepSeek 未返回符合项目约束的结构化诊断"
        else:
            error_code = "invalid_evidence_reference"
            error_message = "DeepSeek 诊断引用了本次输入中不存在的证据"

        # 使用新的短事务记录稳定失败状态。
        # 不保存 provider 原始响应，避免泄露或写入不可控文本。
        async with session_factory() as session:
            await fail_ai_diagnosis(
                session=session,
                event_id=event_id,
                lease_token=lease_token,
                error_code=error_code,
                error_message=error_message,
            )

        # 数据库记录失败后，继续把原业务异常交给上层映射 HTTP 响应。
        raise

    # 第三段数据库操作：只有当前有效租约持有者才能写入结果。
    async with session_factory() as session:
        await complete_ai_diagnosis(
            session=session,
            lease_token=lease_token,
            generation_result=generation_result,
        )

    return AiDiagnosisOrchestrationResult(
        event_id=event_id,
        status=AiDiagnosisOrchestrationStatus.GENERATED,
        attempt_count=claim_result.attempt_count,
        diagnosis=generation_result.diagnosis,
        model_name=generation_result.model_name,
        input_tokens=generation_result.input_tokens,
        output_tokens=generation_result.output_tokens,
        total_tokens=generation_result.total_tokens,
    )

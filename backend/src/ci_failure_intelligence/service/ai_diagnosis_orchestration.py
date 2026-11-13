"""编排一次具备缓存、租约和结果持久化能力的 AI 诊断。"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, NotRequired, TypedDict

from fastembed import TextEmbedding
from langchain_deepseek import ChatDeepSeek
from langgraph.graph import END, START, StateGraph

# 编译后的可执行图类型，供返回类型标注使用。
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ci_failure_intelligence.core.config import (
    DeepSeekSettings,
    RetrievalSettings,
)
from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis
from ci_failure_intelligence.schemas.ai_evidence import AiEvidencePack
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureQueryResponse,
)
from ci_failure_intelligence.schemas.failure_history import (
    HistoricalFailureResponse,
)
from ci_failure_intelligence.service.ai_diagnosis import (
    AiDiagnosisGenerationResult,
    AiDiagnosisProviderError,
    AiDiagnosisStructuredOutputError,
    generate_ai_diagnosis,
    serialize_ai_evidence_pack,
)
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisCacheIdentity,
    AiDiagnosisClaimResult,
    AiDiagnosisClaimStatus,
    AiDiagnosisPersistenceUnavailableError,
    claim_ai_diagnosis,
    complete_ai_diagnosis,
    fail_ai_diagnosis,
)
from ci_failure_intelligence.service.ai_diagnosis_validation import (
    AiDiagnosisEvidenceReferenceError,
)
from ci_failure_intelligence.service.ai_evidence import (
    AiEvidenceEmptyError,
    AiEvidenceNotReadyError,
    build_ai_evidence_pack,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryUnavailableError,
    get_ci_failure_context,
)
from ci_failure_intelligence.service.failure_history import (
    retrieve_event_history,
)

# 使用当前模块的日志名称，沿用应用统一日志配置。
logger = logging.getLogger(__name__)

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


class AiDiagnosisGraphState(TypedDict):
    """声明一次诊断的数据通道，由后续节点逐步补齐。

    这里只保存本次事件及处理结果，不保存客户端、
    活动数据库 Session 或访问凭据。
    """

    # 启动一次诊断时必须提供的业务输入。
    event_id: int
    # 读取节点完成后，才具备当前事件与检索所需的内部上下文
    event: NotRequired[CiFailureQueryResponse]
    github_repository_id: NotRequired[int]
    received_at: NotRequired[datetime]
    # 空列表表示没有历史明细；是否正常完成检索，由状态另外说明
    history: NotRequired[list[HistoricalFailureResponse]]
    retrieval_status: NotRequired[Literal["available", "degraded", "disabled"]]
    # 后续只把这个有限、脱敏的证据包发送给模型。
    evidence_pack: NotRequired[AiEvidencePack]
    # 仅本次抢占成功时传给后续节点；内部租约不得进入模型或HTTP响应。
    claim_result: NotRequired[AiDiagnosisClaimResult]
    # 模型生成什么:模型服务已完成结构和引用校验，但结果尚待数据库提交。
    generation_result: NotRequired[AiDiagnosisGenerationResult]
    # 本请求最终可以对外返回什么
    result: NotRequired[AiDiagnosisOrchestrationResult]


@dataclass(frozen=True, slots=True, repr=False)
class AiDiagnosisGraphContext:
    """提供图节点使用的共享资源。

    资源由调用方创建和管理；本对象只保存引用，
    不负责打开连接、加载模型或关闭客户端。
    """

    # 每段数据库操作独立创建 Session，避免等待模型时占用连接。
    session_factory: async_sessionmaker[AsyncSession]

    # 复用应用已经配置好的 DeepSeek 客户端和模型配置。
    client: ChatDeepSeek
    settings: DeepSeekSettings

    # 同一进程中使用同一模型的检索任务，应共享这个并发限制。
    # 声明 Context 里有一个信号量字段，并不是在这里创建信号量
    embedding_semaphore: asyncio.Semaphore

    # None 保留调用方未启用历史检索的明确状态。
    retrieval_settings: RetrievalSettings | None = None

    # 只接收调用方提供的实例，不在每次诊断中重新创建。
    qdrant_client: AsyncQdrantClient | None = None
    embedding_model: TextEmbedding | None = None


class AiDiagnosisGraphInput(TypedDict):
    """声明启动一次诊断所需的图输入。"""

    event_id: int


class AiDiagnosisGraphOutput(TypedDict):
    """声明图完成后可返回的业务结果，不暴露内部租约。"""

    result: AiDiagnosisOrchestrationResult


# 类型别名：为入口和路由复用同一份图类型说明，不创建新的业务类或图实例。
AiDiagnosisGraph = CompiledStateGraph[
    AiDiagnosisGraphState,
    AiDiagnosisGraphContext,
    AiDiagnosisGraphInput,
    AiDiagnosisGraphOutput,
]


async def load_current_failure_node(
    state: AiDiagnosisGraphState,
    runtime: Runtime[AiDiagnosisGraphContext],
) -> dict[str, object]:
    """读取当前事件和历史检索所需的仓库、时间边界。

    Args:
        state: 已包含正整数 event_id 的本次诊断状态。
        runtime: LangGraph 提供的运行对象，其 context
            已包含由调用方管理的数据库 Session 工厂。

    Returns:
        dict[str, object]: 当前事件、稳定仓库 ID 和接收时间
            三个状态更新，不包含数据库 Session。

    Raises:
        CiFailureQueryNotFoundError: 当前事件不存在或读取期间被删除。
        CiFailureQueryUnavailableError: 数据库无法完成当前事实查询。
    """

    # 从运行上下文取得工厂，仅在本次读取期间持有 Session。
    async with runtime.context.session_factory() as session:
        # 复用索引、检索已有的来源查询，不在节点中重复编写 SQL。
        event, github_repository_id, received_at = await get_ci_failure_context(
            session=session,
            event_id=state["event_id"],
        )

    # 已离开 Session 作用域；后续检索和模型等待不占用这段连接。
    # 返回部分更新，由 LangGraph 合并到本次状态。
    return {
        "event": event,
        "github_repository_id": github_repository_id,
        "received_at": received_at,
    }


async def retrieve_failure_history_node(
    state: AiDiagnosisGraphState,
    runtime: Runtime[AiDiagnosisGraphContext],
) -> dict[str, object]:
    """取得核实后的历史失败，并记录检索是否正常完成。

    Args:
        state: 读取节点已填写当前事件、稳定仓库 ID 和接收时间。
        runtime: 提供共享检索资源及并发限制的运行上下文。

    Returns:
        dict[str, object]: 历史列表与 available、degraded
            或 disabled 检索状态。

    Raises:
        AiEvidenceNotReadyError: 当前事件尚未成功解析。
        AiEvidenceEmptyError: 当前事件没有可诊断的失败明细。
        ValueError: 缺少前置读取结果，或检索来源、向量及历史数据不符合约束。
    """

    # 这些键在初始 State 中可以不存在；必须确认读取节点已提供完整结果。
    event = state.get("event")
    github_repository_id = state.get("github_repository_id")
    received_at = state.get("received_at")
    if event is None or github_repository_id is None or received_at is None:
        raise ValueError("历史检索节点缺少前置事件、仓库 ID 或接收时间")

    context = runtime.context

    # 当前事件不能诊断时提前结束，不执行无意义的向量计算。
    if event.parse_status != "parsed":
        raise AiEvidenceNotReadyError(
            event.event_id,
            event.parse_status,
        )

    if not event.failures:
        raise AiEvidenceEmptyError(event.event_id)

    settings = context.retrieval_settings

    # 未提供检索配置，表示调用方没有启用这条能力。
    if settings is None:
        return {
            "history": [],
            "retrieval_status": "disabled",
        }

    # 已配置检索但资源未就绪，必须标为降级，而非正常零匹配。
    if context.qdrant_client is None or context.embedding_model is None:
        logger.warning(
            "历史检索降级：event_id=%s，reason=resource_not_ready",
            event.event_id,
        )
        return {
            "history": [],
            "retrieval_status": "degraded",
        }

    try:
        # 共享许可控制本进程内的检索并发，不在节点中重建模型。
        async with context.embedding_semaphore:
            history = await retrieve_event_history(
                event=event,
                github_repository_id=github_repository_id,
                received_at=received_at,
                session_factory=context.session_factory,
                client=context.qdrant_client,
                embedding_model=context.embedding_model,
                settings=settings,
            )

    except (
        ApiException,
        # PostgreSQL 历史回查不可用
        CiFailureQueryUnavailableError,
        OSError,
    ) as error:
        # 只记录故障类型，不把可能包含凭据的原始异常写入日志。
        logger.warning(
            "历史检索降级：event_id=%s，error_type=%s",
            event.event_id,
            type(error).__name__,
        )
        return {
            "history": [],
            "retrieval_status": "degraded",
        }

    # 空列表也属于正常检索完成，不能因此标记为降级。
    return {
        "history": history,
        "retrieval_status": "available",
    }


def build_diagnosis_evidence_node(
    state: AiDiagnosisGraphState,
    runtime: Runtime[AiDiagnosisGraphContext],
) -> dict[str, object]:
    """将当前事件和历史检索结果转换为有限模型输入。

    Args:
        state: 已包含当前事件、历史列表和检索状态的图状态。
        runtime: 提供检索配置的运行上下文。

    Returns:
        dict[str, object]: 已完成脱敏、截断和总字节检查的
            evidence_pack 状态更新。

    Raises:
        AiEvidenceNotReadyError: 当前事件尚未成功解析。
        AiEvidenceEmptyError: 当前事件没有可诊断的失败明细。
        AiEvidencePackTooLargeError: 单条有限证据仍超过整包上限。
        ValueError: 缺少前置结果，或检索上下文不符合构包约束。
    """

    # 可选状态键先取值并检查，避免跳过前置节点后继续构包。
    event = state.get("event")
    history = state.get("history")
    retrieval_status = state.get("retrieval_status")

    if event is None or history is None or retrieval_status is None:
        raise ValueError("构包节点缺少前置事件或历史检索结果")
    # 明确关闭检索时不记录向量模型；正常或降级时保留模型配置。
    embedding_model = None
    if retrieval_status != "disabled":
        settings = runtime.context.retrieval_settings
        if settings is None:
            raise ValueError("启用或降级的历史检索缺少模型配置")
        embedding_model = settings.embedding_model
    evidence_pack = build_ai_evidence_pack(
        event,
        history=history,
        retrieval_status=retrieval_status,
        embedding_model=embedding_model,
    )
    return {"evidence_pack": evidence_pack}


async def claim_diagnosis_node(
    state: AiDiagnosisGraphState,
    runtime: Runtime[AiDiagnosisGraphContext],
) -> dict[str, object]:
    """原子取得诊断执行权，或形成可以直接返回的业务结果。

    Args:
        state: 已包含与当前事件一致的最终证据包。
        runtime: 提供模型配置与数据库 Session 工厂的运行上下文。

    Returns:
        dict[str, object]: 抢占成功时返回内部 claim_result；
            缓存命中或处理中时返回对外业务 result。

    Raises:
        ValueError: 缺少最终证据包，或证据包属于其他事件。
        AiDiagnosisPersistenceUnavailableError: 抢占失败、返回状态不可信，
            或已有缓存不符合诊断输出契约。
    """

    evidence_pack = state.get("evidence_pack")
    if evidence_pack is None or evidence_pack.event_id != state["event_id"]:
        raise ValueError("抢占节点缺少与当前事件一致的最终证据包")

    # 复用稳定缓存身份，依据实际模型输入判断旧结果能否使用。
    cache_identity = build_ai_diagnosis_cache_identity(
        evidence_pack=evidence_pack,
        settings=runtime.context.settings,
    )

    # 原子抢占函数负责事务；节点只管理这段 Session 的作用域。
    async with runtime.context.session_factory() as session:
        claim_result = await claim_ai_diagnosis(
            session=session,
            event_id=state["event_id"],
            cache_identity=cache_identity,
        )

    # 数据库结果必须属于本次事件，不能带着其他任务继续执行。
    if claim_result.event_id != state["event_id"]:
        raise AiDiagnosisPersistenceUnavailableError("抢占结果与当前事件不一致")

    if claim_result.status is AiDiagnosisClaimStatus.CLAIMED:
        # 取得执行权后，后续成功或失败写回都需要完整租约。
        if claim_result.lease_token is None or claim_result.lease_expires_at is None:
            raise AiDiagnosisPersistenceUnavailableError("已取得任务但缺少完整租约")
        return {"claim_result": claim_result}

    if claim_result.status is AiDiagnosisClaimStatus.CACHED:
        try:
            # 已有JSON仍需符合当前诊断模型，不能直接当可信对象返回。
            diagnosis = AiDiagnosis.model_validate(claim_result.diagnosis_json)
        except ValidationError as error:
            raise AiDiagnosisPersistenceUnavailableError(
                "AI诊断缓存内容无效"
            ) from error

        result = AiDiagnosisOrchestrationResult(
            event_id=state["event_id"],
            status=AiDiagnosisOrchestrationStatus.CACHED,
            attempt_count=claim_result.attempt_count,
            diagnosis=diagnosis,
            model_name=claim_result.model_name,
            input_tokens=claim_result.input_tokens,
            output_tokens=claim_result.output_tokens,
            total_tokens=claim_result.total_tokens,
        )
        return {"result": result}

    if claim_result.status is AiDiagnosisClaimStatus.IN_PROGRESS:
        result = AiDiagnosisOrchestrationResult(
            event_id=state["event_id"],
            status=AiDiagnosisOrchestrationStatus.IN_PROGRESS,
            attempt_count=claim_result.attempt_count,
            lease_expires_at=claim_result.lease_expires_at,
        )
        return {"result": result}

    raise AiDiagnosisPersistenceUnavailableError("无法解释AI诊断抢占状态")


def route_after_claim(
    state: AiDiagnosisGraphState,
) -> Literal["finish", "generate"]:
    """根据抢占节点的结果，选择结束或生成路线。

    Args:
        state: 已由抢占节点产生业务结果或本次内部租约的状态。

    Returns:
        Literal["finish", "generate"]: 结束本次流程，或进入生成节点。

    Raises:
        ValueError: 抢占阶段出现了不应存在的生成结果。
        AiDiagnosisPersistenceUnavailableError: 缺少有效抢占结果或完整租约。
    """

    result = state.get("result")

    # 缓存命中和其他请求处理中，都不需要本请求执行模型。
    if result is not None:
        if result.status in (
            AiDiagnosisOrchestrationStatus.CACHED,
            AiDiagnosisOrchestrationStatus.IN_PROGRESS,
        ):
            return "finish"
        raise ValueError("抢占阶段出现了不应存在的生成结果")

    claim_result = state.get("claim_result")
    if (
        claim_result is None
        or claim_result.status is not AiDiagnosisClaimStatus.CLAIMED
    ):
        raise AiDiagnosisPersistenceUnavailableError("生成路线缺少本次抢占结果")

    if claim_result.lease_token is None or claim_result.lease_expires_at is None:
        raise AiDiagnosisPersistenceUnavailableError("生成路线缺少完整租约")

    return "generate"


async def generate_diagnosis_node(
    state: AiDiagnosisGraphState,
    runtime: Runtime[AiDiagnosisGraphContext],
) -> dict[str, object]:
    """生成并校验诊断，分类记录可处理的模型失败。

    Args:
        state: 已包含当前最终证据和本次成功抢占结果的状态。
        runtime: 提供共享模型客户端和数据库工厂的运行上下文。

    Returns:
        dict[str, object]: 已通过校验、尚未提交数据库的生成结果。

    Raises:
        ValueError: 前置数据缺失，或事件与执行权不一致。
        AiDiagnosisPersistenceUnavailableError: 租约不完整或失败状态无法保存。
        AiDiagnosisProviderError: 模型调用失败，记录失败后继续传播。
        AiDiagnosisStructuredOutputError: 模型结果结构不合法。
        AiDiagnosisEvidenceReferenceError: 模型引用了未提供的证据。
        AiDiagnosisLeaseLostError: 记录失败时已失去租约。
    """

    evidence_pack = state.get("evidence_pack")
    claim_result = state.get("claim_result")

    if evidence_pack is None or claim_result is None:
        raise ValueError("生成节点缺少最终证据或本次抢占结果")

    # 证据、执行权和当前任务必须属于同一个事件。
    if (
        evidence_pack.event_id != state["event_id"]
        or claim_result.event_id != state["event_id"]
        or claim_result.status is not AiDiagnosisClaimStatus.CLAIMED
    ):
        raise ValueError("生成节点的事件或执行权不一致")

    lease_token = claim_result.lease_token
    if lease_token is None or claim_result.lease_expires_at is None:
        raise AiDiagnosisPersistenceUnavailableError("生成节点缺少完整租约")

    try:
        # 等待模型期间不打开数据库 Session。
        # 结构化输出、引用校验和用量读取均复用现有服务。
        generation_result = await generate_ai_diagnosis(
            client=runtime.context.client,
            evidence_pack=evidence_pack,
        )
    except (
        AiDiagnosisProviderError,
        AiDiagnosisStructuredOutputError,
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

        # 只在记录失败时创建短 Session，并继续校验本次租约。
        async with runtime.context.session_factory() as session:
            await fail_ai_diagnosis(
                session=session,
                event_id=state["event_id"],
                lease_token=lease_token,
                error_code=error_code,
                error_message=error_message,
            )

        # 数据库记录失败后，仍交给上层形成对应的错误响应。
        raise

    return {"generation_result": generation_result}


async def persist_diagnosis_node(
    state: AiDiagnosisGraphState,
    runtime: Runtime[AiDiagnosisGraphContext],
) -> dict[str, object]:
    """提交已经校验的诊断，再形成生成成功的业务结果。

    Args:
        state: 包含本次成功抢占结果和已校验生成结果的状态。
        runtime: 提供数据库 Session 工厂的运行上下文。

    Returns:
        dict[str, object]: 数据库成功提交后的 generated 业务结果。

    Raises:
        ValueError: 前置结果缺失，或事件与执行权不一致。
        AiDiagnosisPersistenceUnavailableError: 租约不完整或保存失败。
        AiDiagnosisLeaseLostError: 数据库拒绝过期或已被替换的租约。
    """

    claim_result = state.get("claim_result")
    generation_result = state.get("generation_result")

    if claim_result is None or generation_result is None:
        raise ValueError("保存节点缺少本次抢占结果或已校验的生成结果")

    # 防止把某次生成结果提交到另一个事件。
    if (
        claim_result.status is not AiDiagnosisClaimStatus.CLAIMED
        or claim_result.event_id != state["event_id"]
        or generation_result.event_id != state["event_id"]
    ):
        raise ValueError("保存节点的事件或执行权不一致")

    lease_token = claim_result.lease_token
    if lease_token is None or claim_result.lease_expires_at is None:
        raise AiDiagnosisPersistenceUnavailableError("保存节点缺少完整租约")

    # 现有服务负责事务提交，以及 token 和到期时间的数据库校验。
    async with runtime.context.session_factory() as session:
        await complete_ai_diagnosis(
            session=session,
            lease_token=lease_token,
            generation_result=generation_result,
        )

    # 只有提交成功，才形成可以对外返回的 generated 结果。
    return {
        "result": AiDiagnosisOrchestrationResult(
            event_id=state["event_id"],
            status=AiDiagnosisOrchestrationStatus.GENERATED,
            attempt_count=claim_result.attempt_count,
            diagnosis=generation_result.diagnosis,
            model_name=generation_result.model_name,
            input_tokens=generation_result.input_tokens,
            output_tokens=generation_result.output_tokens,
            total_tokens=generation_result.total_tokens,
        )
    }


def build_ai_diagnosis_graph() -> AiDiagnosisGraph:
    """构建并编译诊断流程，不执行节点或创建基础设施资源。

    Returns:
        CompiledStateGraph: 使用内部状态、共享上下文和明确
            输入输出边界的可执行诊断图。
    """

    # 分别声明内部数据、运行资源、公开输入和公开输出。
    builder = StateGraph(
        AiDiagnosisGraphState,
        context_schema=AiDiagnosisGraphContext,
        input_schema=AiDiagnosisGraphInput,
        output_schema=AiDiagnosisGraphOutput,
    )

    # 登记节点名称与已有函数；此处不调用这些函数。
    builder.add_node("load_current", load_current_failure_node)
    builder.add_node("retrieve_history", retrieve_failure_history_node)
    builder.add_node("build_evidence", build_diagnosis_evidence_node)
    builder.add_node("claim_or_reuse", claim_diagnosis_node)
    builder.add_node("generate", generate_diagnosis_node)
    builder.add_node("persist", persist_diagnosis_node)

    # 抢占之前，各步骤按固定业务顺序执行。
    # builder.add_edge(起点, 终点)
    builder.add_edge(START, "load_current")
    builder.add_edge("load_current", "retrieve_history")
    builder.add_edge("retrieve_history", "build_evidence")
    builder.add_edge("build_evidence", "claim_or_reuse")

    # 抢占结果决定结束，还是进入付费生成路线。
    builder.add_conditional_edges(
        # 从哪里开始分支
        "claim_or_reuse",
        # 用来做判断的函数
        route_after_claim,
        {
            # 如果路由函数返回"finish"就直接结束工作流。
            "finish": END,
            "generate": "generate",
        },
    )

    # 只有取得执行权的路线会到这里；提交成功后结束。
    builder.add_edge("generate", "persist")
    builder.add_edge("persist", END)

    # LangGraph 把这张图组装成可执行图对象，不执行数据库查询或模型调用。
    return builder.compile(name="ci_failure_diagnosis")


async def orchestrate_ai_diagnosis(
    *,
    graph: AiDiagnosisGraph,
    context: AiDiagnosisGraphContext,
    event_id: int,
) -> AiDiagnosisOrchestrationResult:
    """通过共享图执行一次独立诊断，返回既有业务结果。

    Args:
        graph: 应用已构建、供多次请求复用的诊断图。
        context: 当前应用提供的共享资源与配置。
        event_id: 本次需要诊断的正整数 CI 事件主键。

    Returns:
        AiDiagnosisOrchestrationResult: 新生成、缓存命中或正在处理的结果。

    Raises:
        ValueError: 事件 ID 非法，或节点前置数据不符合约束。
        CiFailureQueryNotFoundError: 当前事件不存在。
        CiFailureQueryUnavailableError: 当前事实无法读取。
        AiEvidenceBuildError: 无法形成符合约束的模型证据包。
        AiDiagnosisPersistenceUnavailableError: 任务存储不可用，
            或图没有返回当前事件的有效结果。
        AiDiagnosisProviderError: 模型调用失败。
        AiDiagnosisStructuredOutputError: 模型结果结构不合法。
        AiDiagnosisEvidenceReferenceError: 模型引用不属于本次证据。
        AiDiagnosisLeaseLostError: 写回时租约已失效。
    """

    # HTTP之外的内部调用也必须提供有效事件ID，拒绝把bool当成整数。
    if type(event_id) is not int or event_id <= 0:
        raise ValueError("诊断事件 ID 必须是正整数")

    # 每次只传入本次事件ID；共享图为这次调用建立独立运行状态。
    # output:按照 Output Schema 对外返回的结果
    output = await graph.ainvoke(
        # 本次业务数据
        {"event_id": event_id},
        # 运行时共享资源
        context=context,
    )

    # 正常输出只包含业务result，额外确认类型和事件归属。
    result = output.get("result")
    if (
        not isinstance(result, AiDiagnosisOrchestrationResult)
        or result.event_id != event_id
    ):
        raise AiDiagnosisPersistenceUnavailableError(
            f"诊断图未返回当前事件的有效结果：event_id={event_id}"
        )

    return result

"""验证 AI 诊断编排使用的稳定缓存身份。"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

import pytest
from ci_failure_intelligence.core.config import DeepSeekSettings
from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis, AiRootCause
from ci_failure_intelligence.schemas.ai_evidence import (
    AiEvidencePack,
    AiFailureEvidence,
)
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
    CiFailureQueryResponse,
)
from ci_failure_intelligence.service import ai_diagnosis_orchestration as orchestration
from ci_failure_intelligence.service.ai_diagnosis import (
    AiDiagnosisGenerationResult,
    AiDiagnosisProviderError,
    serialize_ai_evidence_pack,
)
from ci_failure_intelligence.service.ai_diagnosis_orchestration import (
    AI_DIAGNOSIS_OUTPUT_SCHEMA_VERSION,
    AI_DIAGNOSIS_PROMPT_VERSION,
    AiDiagnosisOrchestrationStatus,
    build_ai_diagnosis_cache_identity,
    orchestrate_ai_diagnosis,
)
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisClaimResult,
    AiDiagnosisClaimStatus,
    AiDiagnosisFinalizeResult,
    AiDiagnosisFinalStatus,
)
from langchain_deepseek import ChatDeepSeek
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def build_evidence_pack(*, details: str = "AssertionError") -> AiEvidencePack:
    """构造一份已经完成安全处理的测试证据包。"""

    failure = AiFailureEvidence(
        evidence_id="failure:0:0",
        testcase_index=0,
        issue_index=0,
        suite_name="backend",
        classname="tests.test_orders",
        test_name="test_create_order",
        issue_type="failure",
        message="expected 200 but received 500",
        details=details,
        content_truncated=False,
    )
    return AiEvidencePack(
        event_id=42,
        repository_full_name="owner/repository",
        run_id=1001,
        run_attempt=1,
        job_key="backend_tests",
        workflow_name="Backend CI",
        job_name="Backend tests",
        commit_sha="a" * 40,
        head_branch="main",
        total_failure_count=1,
        included_failure_count=1,
        truncated=False,
        failures=[failure],
    )


def build_settings(**overrides: object) -> DeepSeekSettings:
    """构造不访问真实 DeepSeek 的测试配置。"""

    values: dict[str, object] = {
        "deepseek_api_key": SecretStr("sk-test-only-key"),
    }
    values.update(overrides)
    return DeepSeekSettings.model_validate(values)


def test_cache_identity_uses_exact_prompt_evidence_serialization() -> None:
    """证据摘要必须来自模型实际收到的稳定 JSON。"""

    evidence_pack = build_evidence_pack(details="断言失败")
    identity = build_ai_diagnosis_cache_identity(
        evidence_pack,
        build_settings(),
    )
    expected_evidence_sha256 = hashlib.sha256(
        serialize_ai_evidence_pack(evidence_pack).encode("utf-8")
    ).hexdigest()

    assert identity.prompt_version == AI_DIAGNOSIS_PROMPT_VERSION
    assert identity.output_schema_version == AI_DIAGNOSIS_OUTPUT_SCHEMA_VERSION
    assert identity.requested_model == "deepseek-flash"
    assert identity.evidence_sha256 == expected_evidence_sha256
    assert len(identity.model_config_sha256) == 64


def test_evidence_change_only_changes_evidence_hash() -> None:
    """错误证据变化应重新诊断，但不应伪装成模型配置变化。"""

    settings = build_settings()
    first = build_ai_diagnosis_cache_identity(
        build_evidence_pack(details="first failure"),
        settings,
    )
    second = build_ai_diagnosis_cache_identity(
        build_evidence_pack(details="second failure"),
        settings,
    )

    assert first.evidence_sha256 != second.evidence_sha256
    assert first.model_config_sha256 == second.model_config_sha256


def test_semantic_model_config_change_invalidates_cache() -> None:
    """输出上限变化可能影响诊断内容，因此必须改变模型配置摘要。"""

    evidence_pack = build_evidence_pack()
    first = build_ai_diagnosis_cache_identity(
        evidence_pack,
        build_settings(deepseek_max_output_tokens=4096),
    )
    second = build_ai_diagnosis_cache_identity(
        evidence_pack,
        build_settings(deepseek_max_output_tokens=2048),
    )

    assert first.evidence_sha256 == second.evidence_sha256
    assert first.model_config_sha256 != second.model_config_sha256


def test_secret_and_operational_settings_do_not_invalidate_cache() -> None:
    """密钥、超时和重试只影响调用过程，不应改变诊断缓存身份。"""

    evidence_pack = build_evidence_pack()
    first = build_ai_diagnosis_cache_identity(
        evidence_pack,
        build_settings(
            deepseek_api_key=SecretStr("sk-first-key"),
            deepseek_timeout_seconds=10,
            deepseek_max_retries=0,
        ),
    )
    second = build_ai_diagnosis_cache_identity(
        evidence_pack,
        build_settings(
            deepseek_api_key=SecretStr("sk-second-key"),
            deepseek_timeout_seconds=60,
            deepseek_max_retries=2,
        ),
    )

    assert first == second


def build_query_response() -> CiFailureQueryResponse:
    """构造查询服务交给编排层的已解析失败事件。"""

    return CiFailureQueryResponse(
        event_id=42,
        repository_full_name="owner/repository",
        run_id=1001,
        run_attempt=1,
        job_key="backend_tests",
        workflow_name="Backend CI",
        job_name="Backend tests",
        commit_sha="a" * 40,
        head_branch="main",
        parse_status="parsed",
        parse_error_code=None,
        parse_error_message=None,
        parse_finished_at=datetime.now(UTC),
        failures=[
            CiFailureDetailResponse(
                id=1,
                testcase_index=0,
                issue_index=0,
                suite_name="backend",
                classname="tests.test_orders",
                test_name="test_create_order",
                issue_type="failure",
                message="expected 200 but received 500",
                details="AssertionError",
            )
        ],
    )


def build_diagnosis() -> AiDiagnosis:
    """构造一份已经通过结构约束的诊断。"""

    return AiDiagnosis(
        summary="订单接口返回了非预期状态码",
        root_causes=[
            AiRootCause(
                title="接口内部错误",
                explanation="证据显示预期 200，但实际返回 500",
                confidence="high",
                evidence_ids=["failure:0:0"],
            )
        ],
        recommended_actions=["检查订单接口日志和异常堆栈"],
        needs_more_information=False,
        missing_information=[],
    )


class TrackingSessionContext:
    """记录 Session 上下文是否处于打开状态。"""

    def __init__(self, factory: "TrackingSessionFactory") -> None:
        self.factory = factory

    async def __aenter__(self) -> AsyncSession:
        self.factory.active_sessions += 1
        return cast(AsyncSession, object())

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.factory.active_sessions -= 1


class TrackingSessionFactory:
    """为编排测试创建可计数的假 Session 上下文。"""

    def __init__(self) -> None:
        self.call_count = 0
        self.active_sessions = 0

    def __call__(self) -> TrackingSessionContext:
        self.call_count += 1
        return TrackingSessionContext(self)


def as_session_factory(
    factory: TrackingSessionFactory,
) -> async_sessionmaker[AsyncSession]:
    """只向静态检查器声明假工厂满足当前测试边界。"""

    return cast(async_sessionmaker[AsyncSession], cast(object, factory))


def as_chat_deepseek() -> ChatDeepSeek:
    """构造不会被测试直接调用的模型占位对象。"""

    return cast(ChatDeepSeek, object())


def test_cached_diagnosis_skips_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缓存身份相同应直接返回已有诊断，不重复产生模型费用。"""

    diagnosis = build_diagnosis()
    factory = TrackingSessionFactory()
    provider_called = False

    async def fake_query(*, session: AsyncSession, event_id: int):
        assert factory.active_sessions == 1
        assert event_id == 42
        return build_query_response()

    async def fake_claim(**_: object) -> AiDiagnosisClaimResult:
        return AiDiagnosisClaimResult(
            event_id=42,
            status=AiDiagnosisClaimStatus.CACHED,
            attempt_count=1,
            diagnosis_json=diagnosis.model_dump(mode="json"),
            model_name="deepseek-flash",
            total_tokens=200,
        )

    async def fake_generate(**_: object) -> AiDiagnosisGenerationResult:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("缓存命中时不得调用模型")

    monkeypatch.setattr(orchestration, "get_ci_failure_details", fake_query)
    monkeypatch.setattr(orchestration, "claim_ai_diagnosis", fake_claim)
    monkeypatch.setattr(orchestration, "generate_ai_diagnosis", fake_generate)

    result = asyncio.run(
        orchestrate_ai_diagnosis(
            as_session_factory(factory),
            as_chat_deepseek(),
            build_settings(),
            event_id=42,
        )
    )

    assert result.status is AiDiagnosisOrchestrationStatus.CACHED
    assert result.diagnosis == diagnosis
    assert result.total_tokens == 200
    assert provider_called is False
    assert factory.call_count == 2
    assert factory.active_sessions == 0


def test_in_progress_returns_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """其他执行者持有租约时应返回处理中，而不是并发调用模型。"""

    factory = TrackingSessionFactory()
    lease_expires_at = datetime.now(UTC) + timedelta(seconds=120)

    async def fake_query(**_: object) -> CiFailureQueryResponse:
        return build_query_response()

    async def fake_claim(**_: object) -> AiDiagnosisClaimResult:
        return AiDiagnosisClaimResult(
            event_id=42,
            status=AiDiagnosisClaimStatus.IN_PROGRESS,
            attempt_count=2,
            lease_expires_at=lease_expires_at,
        )

    async def forbidden_generate(**_: object) -> AiDiagnosisGenerationResult:
        raise AssertionError("处理中状态不得调用模型")

    monkeypatch.setattr(orchestration, "get_ci_failure_details", fake_query)
    monkeypatch.setattr(orchestration, "claim_ai_diagnosis", fake_claim)
    monkeypatch.setattr(orchestration, "generate_ai_diagnosis", forbidden_generate)

    result = asyncio.run(
        orchestrate_ai_diagnosis(
            as_session_factory(factory),
            as_chat_deepseek(),
            build_settings(),
            event_id=42,
        )
    )

    assert result.status is AiDiagnosisOrchestrationStatus.IN_PROGRESS
    assert result.diagnosis is None
    assert result.lease_expires_at == lease_expires_at
    assert factory.call_count == 2


def test_claimed_task_calls_provider_without_open_database_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型等待期间不得占用数据库 Session，成功后再用短事务写回。"""

    factory = TrackingSessionFactory()
    diagnosis = build_diagnosis()
    lease_token = UUID("00000000-0000-4000-8000-000000000042")
    completed_tokens: list[UUID] = []

    async def fake_query(**_: object) -> CiFailureQueryResponse:
        return build_query_response()

    async def fake_claim(**_: object) -> AiDiagnosisClaimResult:
        return AiDiagnosisClaimResult(
            event_id=42,
            status=AiDiagnosisClaimStatus.CLAIMED,
            attempt_count=1,
            lease_token=lease_token,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
        )

    async def fake_generate(**_: object) -> AiDiagnosisGenerationResult:
        assert factory.active_sessions == 0
        return AiDiagnosisGenerationResult(
            event_id=42,
            diagnosis=diagnosis,
            model_name="deepseek-flash",
            input_tokens=120,
            output_tokens=80,
            total_tokens=200,
        )

    async def fake_complete(
        *,
        session: AsyncSession,
        lease_token: UUID,
        generation_result: AiDiagnosisGenerationResult,
    ) -> AiDiagnosisFinalizeResult:
        assert factory.active_sessions == 1
        assert generation_result.diagnosis == diagnosis
        completed_tokens.append(lease_token)
        return AiDiagnosisFinalizeResult(
            event_id=42,
            status=AiDiagnosisFinalStatus.COMPLETED,
        )

    monkeypatch.setattr(orchestration, "get_ci_failure_details", fake_query)
    monkeypatch.setattr(orchestration, "claim_ai_diagnosis", fake_claim)
    monkeypatch.setattr(orchestration, "generate_ai_diagnosis", fake_generate)
    monkeypatch.setattr(orchestration, "complete_ai_diagnosis", fake_complete)

    result = asyncio.run(
        orchestrate_ai_diagnosis(
            as_session_factory(factory),
            as_chat_deepseek(),
            build_settings(),
            event_id=42,
        )
    )

    assert result.status is AiDiagnosisOrchestrationStatus.GENERATED
    assert result.diagnosis == diagnosis
    assert completed_tokens == [lease_token]
    assert factory.call_count == 3
    assert factory.active_sessions == 0


def test_provider_failure_is_persisted_with_current_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型故障应使用当前租约记录安全失败摘要，再向上抛出原异常。"""

    factory = TrackingSessionFactory()
    lease_token = UUID("00000000-0000-4000-8000-000000000043")
    persisted_failures: list[tuple[str, str]] = []

    async def fake_query(**_: object) -> CiFailureQueryResponse:
        return build_query_response()

    async def fake_claim(**_: object) -> AiDiagnosisClaimResult:
        return AiDiagnosisClaimResult(
            event_id=42,
            status=AiDiagnosisClaimStatus.CLAIMED,
            attempt_count=1,
            lease_token=lease_token,
        )

    async def fake_generate(**_: object) -> AiDiagnosisGenerationResult:
        assert factory.active_sessions == 0
        raise AiDiagnosisProviderError(event_id=42)

    async def fake_fail(
        *,
        session: AsyncSession,
        event_id: int,
        lease_token: UUID,
        error_code: str,
        error_message: str,
    ) -> AiDiagnosisFinalizeResult:
        assert factory.active_sessions == 1
        assert event_id == 42
        assert lease_token == UUID("00000000-0000-4000-8000-000000000043")
        persisted_failures.append((error_code, error_message))
        return AiDiagnosisFinalizeResult(
            event_id=42,
            status=AiDiagnosisFinalStatus.FAILED,
        )

    monkeypatch.setattr(orchestration, "get_ci_failure_details", fake_query)
    monkeypatch.setattr(orchestration, "claim_ai_diagnosis", fake_claim)
    monkeypatch.setattr(orchestration, "generate_ai_diagnosis", fake_generate)
    monkeypatch.setattr(orchestration, "fail_ai_diagnosis", fake_fail)

    with pytest.raises(AiDiagnosisProviderError):
        asyncio.run(
            orchestrate_ai_diagnosis(
                as_session_factory(factory),
                as_chat_deepseek(),
                build_settings(),
                event_id=42,
            )
        )

    assert persisted_failures == [
        ("provider_unavailable", "DeepSeek 暂时无法完成本次诊断")
    ]
    assert factory.call_count == 3
    assert factory.active_sessions == 0

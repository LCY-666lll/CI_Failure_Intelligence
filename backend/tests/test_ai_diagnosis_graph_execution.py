"""验证图执行阶段的模型等待、失败写回和成功提交边界。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from ci_failure_intelligence.service import ai_diagnosis_orchestration as orchestration
from ci_failure_intelligence.service.ai_diagnosis import (
    AiDiagnosisGenerationResult,
    AiDiagnosisProviderError,
    AiDiagnosisStructuredOutputError,
)
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisClaimResult,
    AiDiagnosisClaimStatus,
    AiDiagnosisLeaseLostError,
    AiDiagnosisPersistenceUnavailableError,
)
from ci_failure_intelligence.service.ai_diagnosis_validation import (
    AiDiagnosisEvidenceReferenceError,
)
from langgraph.runtime import Runtime

from backend.tests.test_ai_diagnosis_orchestration import (
    TrackingSessionFactory,
    as_chat_deepseek,
    as_session_factory,
    build_diagnosis,
    build_evidence_pack,
    build_settings,
)


def make_execution():
    """复用现有假工厂和诊断样本，不创建真实客户端。"""
    factory = TrackingSessionFactory()
    context = orchestration.AiDiagnosisGraphContext(
        session_factory=as_session_factory(factory),
        client=as_chat_deepseek(),
        settings=build_settings(),
        embedding_semaphore=asyncio.Semaphore(1),
    )
    claim = AiDiagnosisClaimResult(
        event_id=42,
        status=AiDiagnosisClaimStatus.CLAIMED,
        attempt_count=2,
        lease_token=uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
    )
    generation = AiDiagnosisGenerationResult(
        event_id=42,
        diagnosis=build_diagnosis(),
        model_name="deepseek-flash",
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
    )
    state: orchestration.AiDiagnosisGraphState = {
        "event_id": 42,
        "evidence_pack": build_evidence_pack(),
        "claim_result": claim,
    }
    return factory, context, state, claim, generation


def test_generation_and_persistence_separate_wait_from_transaction(monkeypatch):
    """模型等待不持连接，提交完成后才产生generated业务结果。"""

    async def scenario():
        factory, context, state, claim, generation = make_execution()

        async def generate(**kwargs):
            assert factory.active_sessions == 0
            assert kwargs["evidence_pack"] is state.get("evidence_pack")
            return generation

        async def complete(**kwargs):
            assert factory.active_sessions == 1
            assert kwargs["lease_token"] == claim.lease_token
            assert kwargs["generation_result"] is generation

        monkeypatch.setattr(orchestration, "generate_ai_diagnosis", generate)
        complete_mock = AsyncMock(side_effect=complete)
        monkeypatch.setattr(orchestration, "complete_ai_diagnosis", complete_mock)
        fail_mock = AsyncMock()
        monkeypatch.setattr(orchestration, "fail_ai_diagnosis", fail_mock)
        update = await orchestration.generate_diagnosis_node(
            state, Runtime(context=context)
        )
        assert update == {"generation_result": generation}
        assert "result" not in state
        assert factory.call_count == 0
        state["generation_result"] = generation
        result = await orchestration.persist_diagnosis_node(
            state, Runtime(context=context)
        )
        completed = result["result"]
        assert isinstance(completed, orchestration.AiDiagnosisOrchestrationResult)
        assert (
            completed.status is orchestration.AiDiagnosisOrchestrationStatus.GENERATED
        )
        assert completed.attempt_count == 2 and completed.total_tokens == 30
        assert completed.diagnosis is generation.diagnosis
        assert factory.active_sessions == 0 and factory.call_count == 1
        complete_mock.assert_awaited_once()
        fail_mock.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error,code",
    [
        (AiDiagnosisProviderError(42), "provider_unavailable"),
        (
            AiDiagnosisStructuredOutputError(42, "test-only"),
            "invalid_structured_output",
        ),
        (
            AiDiagnosisEvidenceReferenceError(42, ["failure:99:0"]),
            "invalid_evidence_reference",
        ),
    ],
)
def test_generation_records_known_failure_then_preserves_original_error(
    monkeypatch, error, code
):
    """已知模型失败按本次租约记录，不能伪装成成功或保存原始响应。"""
    factory, context, state, claim, _ = make_execution()

    async def fail(**kwargs):
        assert factory.active_sessions == 1
        assert kwargs["event_id"] == 42
        assert kwargs["lease_token"] == claim.lease_token
        assert kwargs["error_code"] == code
        assert "diagnosis_json" not in kwargs

    fail_mock = AsyncMock(side_effect=fail)
    monkeypatch.setattr(
        orchestration, "generate_ai_diagnosis", AsyncMock(side_effect=error)
    )
    monkeypatch.setattr(orchestration, "fail_ai_diagnosis", fail_mock)
    with pytest.raises(type(error)) as caught:
        asyncio.run(
            orchestration.generate_diagnosis_node(state, Runtime(context=context))
        )
    assert caught.value is error
    assert factory.active_sessions == 0
    fail_mock.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [
        AiDiagnosisLeaseLostError(42),
        AiDiagnosisPersistenceUnavailableError("test-only"),
    ],
)
def test_failed_completion_never_returns_generated_or_marks_provider_failure(
    monkeypatch, error
):
    """提交错误不能误写为模型失败，更不能返回generated。"""
    factory, context, state, _, generation = make_execution()
    state["generation_result"] = generation
    monkeypatch.setattr(
        orchestration, "complete_ai_diagnosis", AsyncMock(side_effect=error)
    )
    fail_mock = AsyncMock()
    monkeypatch.setattr(orchestration, "fail_ai_diagnosis", fail_mock)
    with pytest.raises(type(error)) as caught:
        asyncio.run(
            orchestration.persist_diagnosis_node(state, Runtime(context=context))
        )
    assert caught.value is error
    assert "result" not in state
    assert factory.active_sessions == 0
    fail_mock.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        AiDiagnosisLeaseLostError(42),
        AiDiagnosisPersistenceUnavailableError("test-only"),
    ],
)
def test_failed_failure_record_is_not_reported_as_committed(monkeypatch, error):
    """失败记录本身被拒绝时，向上传播真实写回故障。"""
    factory, context, state, _, _ = make_execution()
    monkeypatch.setattr(
        orchestration,
        "generate_ai_diagnosis",
        AsyncMock(side_effect=AiDiagnosisProviderError(42)),
    )
    monkeypatch.setattr(
        orchestration, "fail_ai_diagnosis", AsyncMock(side_effect=error)
    )
    with pytest.raises(type(error)) as caught:
        asyncio.run(
            orchestration.generate_diagnosis_node(state, Runtime(context=context))
        )
    assert caught.value is error
    assert factory.active_sessions == 0


@pytest.mark.parametrize(
    "fault", ["missing_pack", "missing_claim", "wrong_event", "not_claimed"]
)
def test_generation_requires_matching_evidence_and_execution_right(monkeypatch, fault):
    """生成之前拒绝错误事件或无执行权状态，避免无意义费用。"""
    _, context, state, claim, _ = make_execution()
    if fault == "missing_pack":
        state.pop("evidence_pack")
    elif fault == "missing_claim":
        state.pop("claim_result")
    elif fault == "wrong_event":
        state["claim_result"] = replace(claim, event_id=99)
    else:
        state["claim_result"] = replace(
            claim, status=AiDiagnosisClaimStatus.IN_PROGRESS
        )
    provider = AsyncMock()
    monkeypatch.setattr(orchestration, "generate_ai_diagnosis", provider)
    with pytest.raises(ValueError):
        asyncio.run(
            orchestration.generate_diagnosis_node(state, Runtime(context=context))
        )
    provider.assert_not_awaited()


@pytest.mark.parametrize("wrong_event", [False, True])
def test_persistence_rejects_missing_or_wrong_generation(monkeypatch, wrong_event):
    """不可将其他事件的生成结果提交到当前任务。"""
    _, context, state, _, generation = make_execution()
    if wrong_event:
        state["generation_result"] = replace(generation, event_id=99)
    complete = AsyncMock()
    monkeypatch.setattr(orchestration, "complete_ai_diagnosis", complete)
    with pytest.raises(ValueError):
        asyncio.run(
            orchestration.persist_diagnosis_node(state, Runtime(context=context))
        )
    complete.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [RuntimeError("programming error"), asyncio.CancelledError()]
)
def test_generation_does_not_swallow_programming_errors_or_cancellation(
    monkeypatch, error
):
    """未分类错误与取消不冒充已记录的模型失败，遗留任务依赖租约恢复。"""
    factory, context, state, _, _ = make_execution()
    monkeypatch.setattr(
        orchestration, "generate_ai_diagnosis", AsyncMock(side_effect=error)
    )
    fail_mock = AsyncMock()
    monkeypatch.setattr(orchestration, "fail_ai_diagnosis", fail_mock)
    with pytest.raises(type(error)):
        asyncio.run(
            orchestration.generate_diagnosis_node(state, Runtime(context=context))
        )
    assert factory.call_count == 0
    fail_mock.assert_not_awaited()

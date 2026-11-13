"""验证正式图工厂的执行分支与输入输出边界。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from ci_failure_intelligence import application as application_module
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.schemas.failure_history import HistoricalFailureResponse
from ci_failure_intelligence.service import ai_diagnosis_orchestration as orchestration
from ci_failure_intelligence.service.ai_diagnosis import (
    AiDiagnosisGenerationResult,
    AiDiagnosisProviderError,
)
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisClaimResult,
    AiDiagnosisClaimStatus,
    AiDiagnosisLeaseLostError,
)
from ci_failure_intelligence.service.ci_failure_query import CiFailureQueryNotFoundError
from fastapi.testclient import TestClient
from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.tests.test_ai_diagnosis_orchestration import (
    TrackingSessionFactory,
    as_chat_deepseek,
    as_session_factory,
    build_diagnosis,
    build_query_response,
    build_settings,
)


def prepare_workflow(monkeypatch, status=AiDiagnosisClaimStatus.CLAIMED):
    """将外部服务替换为可观察的假对象，节点和工厂使用正式实现。"""
    factory = TrackingSessionFactory()
    event = build_query_response()
    context = orchestration.AiDiagnosisGraphContext(
        session_factory=as_session_factory(factory),
        client=as_chat_deepseek(),
        settings=build_settings(),
        embedding_semaphore=asyncio.Semaphore(1),
    )
    expires = datetime.now(UTC) + timedelta(seconds=120)
    diagnosis = build_diagnosis()
    if status is AiDiagnosisClaimStatus.CACHED:
        claim = AiDiagnosisClaimResult(
            event_id=42,
            status=status,
            attempt_count=2,
            diagnosis_json=diagnosis.model_dump(mode="json"),
            total_tokens=30,
        )
    elif status is AiDiagnosisClaimStatus.IN_PROGRESS:
        claim = AiDiagnosisClaimResult(
            event_id=42, status=status, attempt_count=2, lease_expires_at=expires
        )
    else:
        claim = AiDiagnosisClaimResult(
            event_id=42,
            status=status,
            attempt_count=2,
            lease_token=uuid4(),
            lease_expires_at=expires,
        )
    mocks = {
        "get_ci_failure_context": AsyncMock(
            return_value=(event, 123456, datetime(2026, 11, 12, tzinfo=UTC))
        ),
        "retrieve_event_history": AsyncMock(return_value=[]),
        "claim_ai_diagnosis": AsyncMock(return_value=claim),
        "generate_ai_diagnosis": AsyncMock(
            return_value=AiDiagnosisGenerationResult(
                event_id=42,
                diagnosis=diagnosis,
                model_name="deepseek-flash",
                input_tokens=10,
                output_tokens=20,
                total_tokens=30,
            )
        ),
        "complete_ai_diagnosis": AsyncMock(),
        "fail_ai_diagnosis": AsyncMock(),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(orchestration, name, mock)
    return factory, context, event, mocks


@pytest.mark.parametrize("status", list(AiDiagnosisClaimStatus))
def test_formal_factory_branches_only_return_public_result(monkeypatch, status):
    """编译无外部动作；三分支返回既有业务结果且隐藏内部租约。"""
    factory, context, _, mocks = prepare_workflow(monkeypatch, status)
    graph = orchestration.build_ai_diagnosis_graph()
    for mock in mocks.values():
        mock.assert_not_awaited()
    output = asyncio.run(graph.ainvoke({"event_id": 42}, context=context))
    assert set(output) == {"result"}
    result = output["result"]
    assert result.event_id == 42 and result.attempt_count == 2
    expected = {
        AiDiagnosisClaimStatus.CACHED: orchestration.AiDiagnosisOrchestrationStatus.CACHED,
        AiDiagnosisClaimStatus.IN_PROGRESS: orchestration.AiDiagnosisOrchestrationStatus.IN_PROGRESS,
        AiDiagnosisClaimStatus.CLAIMED: orchestration.AiDiagnosisOrchestrationStatus.GENERATED,
    }
    assert result.status is expected[status]
    assert not hasattr(result, "lease_token")
    count = int(status is AiDiagnosisClaimStatus.CLAIMED)
    assert mocks["generate_ai_diagnosis"].await_count == count
    assert mocks["complete_ai_diagnosis"].await_count == count
    mocks["fail_ai_diagnosis"].assert_not_awaited()
    assert factory.active_sessions == 0


@pytest.mark.parametrize("degraded", [False, True])
def test_formal_graph_passes_history_or_degraded_input_to_model(monkeypatch, degraded):
    """正式六节点链路将历史来源或真实降级标记传入最终模型输入。"""
    _, context, event, mocks = prepare_workflow(monkeypatch)
    context = replace(
        context,
        retrieval_settings=RetrievalSettings.model_validate(
            {"qdrant_api_key": "test-only-" + "x" * 32}
        ),
        qdrant_client=cast(AsyncQdrantClient, object()),
        embedding_model=cast(TextEmbedding, object()),
    )
    history = HistoricalFailureResponse(
        event_id=13,
        repository_full_name=event.repository_full_name,
        run_id=999,
        run_attempt=1,
        job_key="backend",
        received_at=datetime(2026, 11, 10, tzinfo=UTC),
        similarity_score=0.9,
        failure=event.failures[0],
    )
    if degraded:
        mocks["retrieve_event_history"].side_effect = ApiException("test-only")
    else:
        mocks["retrieve_event_history"].return_value = [history]
    output = asyncio.run(
        orchestration.build_ai_diagnosis_graph().ainvoke(
            {"event_id": 42}, context=context
        )
    )
    provider_call = mocks["generate_ai_diagnosis"].await_args
    assert provider_call is not None
    pack = provider_call.kwargs["evidence_pack"]
    assert pack.retrieval_status == ("degraded" if degraded else "available")
    assert len(pack.history) == int(not degraded)
    if not degraded:
        assert pack.history[0].evidence_id == "history:13:0:0"
    assert set(output) == {"result"}
    claim_call = mocks["claim_ai_diagnosis"].await_args
    assert claim_call is not None
    assert claim_call.kwargs[
        "cache_identity"
    ] == orchestration.build_ai_diagnosis_cache_identity(pack, context.settings)


def test_formal_input_schema_cannot_preload_internal_result(monkeypatch):
    """调用方额外传入的内部字段不能伪造缓存结果或改变事实来源。"""
    _, context, _, mocks = prepare_workflow(monkeypatch)
    graph = orchestration.build_ai_diagnosis_graph()
    cached = orchestration.AiDiagnosisOrchestrationResult(
        event_id=99,
        status=orchestration.AiDiagnosisOrchestrationStatus.CACHED,
        attempt_count=100,
    )
    output = asyncio.run(
        graph.ainvoke(
            cast(
                orchestration.AiDiagnosisGraphInput,
                {"event_id": 42, "result": cached, "github_repository_id": 999},
            ),
            context=context,
        )
    )
    assert output["result"].event_id == 42
    assert (
        output["result"].status
        is orchestration.AiDiagnosisOrchestrationStatus.GENERATED
    )
    mocks["generate_ai_diagnosis"].assert_awaited_once()


def test_formal_graph_does_not_retry_paid_generation_when_commit_fails(monkeypatch):
    """保存租约被拒绝后直接失败，不在图内重新调用付费模型。"""
    factory, context, _, mocks = prepare_workflow(monkeypatch)
    mocks["complete_ai_diagnosis"].side_effect = AiDiagnosisLeaseLostError(42)
    with pytest.raises(AiDiagnosisLeaseLostError):
        asyncio.run(
            orchestration.build_ai_diagnosis_graph().ainvoke(
                {"event_id": 42}, context=context
            )
        )
    mocks["generate_ai_diagnosis"].assert_awaited_once()
    mocks["complete_ai_diagnosis"].assert_awaited_once()
    mocks["fail_ai_diagnosis"].assert_not_awaited()
    assert factory.active_sessions == 0


@pytest.mark.parametrize("status", list(AiDiagnosisClaimStatus))
def test_http_route_uses_app_graph_and_context_without_exposing_lease(
    monkeypatch, client, app, test_settings, status
):
    """真实HTTP路由和薄入口执行共享图；外部服务均为假对象。"""
    factory, _, event, mocks = prepare_workflow(monkeypatch, status)
    original_graph = app.state.ai_diagnosis_graph
    original_context = app.state.ai_diagnosis_context
    app.state.ai_diagnosis_context = replace(
        original_context, session_factory=as_session_factory(factory)
    )
    headers = {
        "Authorization": "Bearer "
        + test_settings.ai_diagnosis_trigger_token.get_secret_value()
    }
    assert client.post("/api/v1/ci-runs/42/diagnosis").status_code == 401
    read_headers = {
        "Authorization": "Bearer " + test_settings.failure_read_token.get_secret_value()
    }
    assert (
        client.post("/api/v1/ci-runs/42/diagnosis", headers=read_headers).status_code
        == 401
    )
    assert (
        client.post("/api/v1/ci-runs/0/diagnosis", headers=headers).status_code == 422
    )
    mocks["get_ci_failure_context"].assert_not_awaited()

    response = client.post("/api/v1/ci-runs/42/diagnosis", headers=headers)
    assert response.status_code == (
        202 if status is AiDiagnosisClaimStatus.IN_PROGRESS else 200
    )
    payload = response.json()
    assert payload["event_id"] == event.event_id
    assert payload["attempt_count"] == 2
    assert "lease_token" not in payload and "claim_result" not in payload
    assert "generation_result" not in payload
    assert app.state.ai_diagnosis_graph is original_graph
    assert app.state.ai_diagnosis_context.client is original_context.client
    expected = int(status is AiDiagnosisClaimStatus.CLAIMED)
    assert mocks["generate_ai_diagnosis"].await_count == expected
    assert mocks["complete_ai_diagnosis"].await_count == expected
    if expected:
        provider_call = mocks["generate_ai_diagnosis"].await_args
        assert provider_call is not None
        assert provider_call.kwargs["evidence_pack"].retrieval_status == "disabled"
    mocks["get_ci_failure_context"].side_effect = CiFailureQueryNotFoundError(999999)
    assert (
        client.post("/api/v1/ci-runs/999999/diagnosis", headers=headers).status_code
        == 404
    )
    assert factory.active_sessions == 0


def test_http_graph_provider_failure_records_state_and_keeps_503_contract(
    monkeypatch, client, app, test_settings
):
    """正式HTTP图执行失败写回后，仍使用原有固定503响应。"""
    from ci_failure_intelligence.api.exception_handlers import (
        AI_DIAGNOSIS_SERVICE_UNAVAILABLE_DETAIL,
    )

    factory, _, _, mocks = prepare_workflow(monkeypatch)
    app.state.ai_diagnosis_context = replace(
        app.state.ai_diagnosis_context, session_factory=as_session_factory(factory)
    )
    mocks["generate_ai_diagnosis"].side_effect = AiDiagnosisProviderError(42)
    headers = {
        "Authorization": "Bearer "
        + test_settings.ai_diagnosis_trigger_token.get_secret_value()
    }
    response = client.post("/api/v1/ci-runs/42/diagnosis", headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": AI_DIAGNOSIS_SERVICE_UNAVAILABLE_DETAIL}
    mocks["generate_ai_diagnosis"].assert_awaited_once()
    mocks["complete_ai_diagnosis"].assert_not_awaited()
    failure_call = mocks["fail_ai_diagnosis"].await_args
    assert failure_call is not None
    assert failure_call.kwargs["error_code"] == "provider_unavailable"
    assert factory.active_sessions == 0


@pytest.mark.parametrize("retrieval_case", ["history", "empty", "degraded"])
def test_http_graph_uses_lifespan_resources_for_history_evidence(
    monkeypatch, test_settings, nonce_store, retrieval_case
):
    """正式启动→HTTP→图使用完整Context；历史命中、零匹配和故障正确入包。"""
    factory, _, event, mocks = prepare_workflow(monkeypatch)
    qdrant = cast(AsyncQdrantClient, object())
    model = cast(TextEmbedding, object())
    retrieval_settings = RetrievalSettings.model_validate(
        {"qdrant_api_key": "test-only-" + "x" * 32}
    )
    monkeypatch.setattr(
        application_module,
        "create_database_session_factory",
        lambda _engine: as_session_factory(factory),
    )
    if retrieval_case == "history":
        mocks["retrieve_event_history"].return_value = [
            HistoricalFailureResponse(
                event_id=13,
                repository_full_name=event.repository_full_name,
                run_id=999,
                run_attempt=1,
                job_key="backend",
                received_at=datetime(2026, 11, 10, tzinfo=UTC),
                similarity_score=0.9,
                failure=event.failures[0],
            )
        ]
    elif retrieval_case == "degraded":
        mocks["retrieve_event_history"].side_effect = ApiException("test-only")
    app = application_module.create_app(
        settings=test_settings,
        nonce_store=nonce_store,
        database_engine=cast(AsyncEngine, object()),
        deepseek_client=as_chat_deepseek(),
        retrieval_settings=retrieval_settings,
        qdrant_client=qdrant,
        embedding_model=model,
    )
    with TestClient(app) as client:
        context = app.state.ai_diagnosis_context
        headers = {
            "Authorization": "Bearer "
            + test_settings.ai_diagnosis_trigger_token.get_secret_value()
        }
        response = client.post("/api/v1/ci-runs/42/diagnosis", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "generated"
        retrieval_call = mocks["retrieve_event_history"].await_args
        assert retrieval_call is not None
        assert retrieval_call.kwargs["client"] is qdrant
        assert retrieval_call.kwargs["embedding_model"] is model
        assert retrieval_call.kwargs["settings"] is retrieval_settings
        provider_call = mocks["generate_ai_diagnosis"].await_args
        assert provider_call is not None
        pack = provider_call.kwargs["evidence_pack"]
        assert pack.retrieval_status == (
            "degraded" if retrieval_case == "degraded" else "available"
        )
        assert len(pack.history) == int(retrieval_case == "history")
        if pack.history:
            assert pack.history[0].evidence_id == "history:13:0:0"
        claim_call = mocks["claim_ai_diagnosis"].await_args
        assert claim_call is not None
        assert claim_call.kwargs[
            "cache_identity"
        ] == orchestration.build_ai_diagnosis_cache_identity(pack, context.settings)
        assert app.state.ai_diagnosis_context is context
    assert not hasattr(app.state, "ai_diagnosis_context")

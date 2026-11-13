"""验证正式读取节点的状态传递和数据库作用域。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureQueryResponse
from ci_failure_intelligence.schemas.failure_history import HistoricalFailureResponse
from ci_failure_intelligence.service import ai_diagnosis_orchestration as orchestration
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisClaimResult,
    AiDiagnosisClaimStatus,
    AiDiagnosisPersistenceUnavailableError,
)
from ci_failure_intelligence.service.ai_evidence import (
    AiEvidenceEmptyError,
    AiEvidenceNotReadyError,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
)
from fastembed import TextEmbedding
from langchain_deepseek import ChatDeepSeek
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.tests.test_ai_diagnosis_orchestration import build_diagnosis


def make_context(settings, session_scope):
    """只提供假资源，不建立数据库或模型连接。"""
    return orchestration.AiDiagnosisGraphContext(
        session_factory=cast(async_sessionmaker[AsyncSession], session_scope),
        client=cast(ChatDeepSeek, object()),
        settings=settings,
        embedding_semaphore=asyncio.Semaphore(1),
    )


def test_load_node_preserves_source_in_real_graph(monkeypatch, test_settings):
    """真实图必须保留正确仓库键，并在把事实交给下游前关闭Session。"""

    async def scenario():
        session = cast(AsyncSession, object())
        closed = False

        @asynccontextmanager
        async def scope():
            nonlocal closed
            try:
                yield session
            finally:
                closed = True

        event = object()
        received_at = datetime(2026, 11, 11, tzinfo=UTC)
        query = AsyncMock(return_value=(event, 123456, received_at))
        monkeypatch.setattr(orchestration, "get_ci_failure_context", query)
        context = make_context(test_settings, scope)

        def inspect_source(state):
            assert closed
            assert state["github_repository_id"] == 123456
            assert state["received_at"] is received_at
            assert state["event"] is event
            return {}

        builder = StateGraph(
            orchestration.AiDiagnosisGraphState,
            context_schema=orchestration.AiDiagnosisGraphContext,
        )
        builder.add_node("load", orchestration.load_current_failure_node)
        builder.add_node("inspect", inspect_source)
        builder.add_edge(START, "load")
        builder.add_edge("load", "inspect")
        builder.add_edge("inspect", END)
        initial: orchestration.AiDiagnosisGraphState = {"event_id": 17}
        result = await builder.compile().ainvoke(initial, context=context)
        query.assert_awaited_once_with(session=session, event_id=17)
        assert initial == {"event_id": 17}
        assert result["event_id"] == 17
        assert "session" not in result and "client" not in result

    asyncio.run(scenario())


@pytest.fixture
def current_event():
    """构造已解析、含一条当前失败的纯测试事件。"""
    return CiFailureQueryResponse.model_validate(
        {
            "event_id": 17,
            "repository_full_name": "team/backend",
            "run_id": 1001,
            "run_attempt": 1,
            "job_key": "backend",
            "workflow_name": "Backend CI",
            "job_name": "Backend tests",
            "commit_sha": "a" * 40,
            "head_branch": "main",
            "parse_status": "parsed",
            "parse_error_code": None,
            "parse_error_message": None,
            "parse_finished_at": None,
            "failures": [
                {
                    "id": 1,
                    "testcase_index": 0,
                    "issue_index": 0,
                    "suite_name": "backend",
                    "classname": "tests.test_orders",
                    "test_name": "test_order",
                    "issue_type": "failure",
                    "message": "token=test-only-sensitive-value",
                    "details": "AssertionError",
                }
            ],
        }
    )


@pytest.fixture
def current_state(current_event):
    """模拟读取节点已经产生的业务状态。"""
    return {
        "event_id": 17,
        "event": current_event,
        "github_repository_id": 123456,
        "received_at": datetime(2026, 11, 11, tzinfo=UTC),
    }


@pytest.fixture
def retrieval_context(test_settings):
    """使用占位客户端，实际检索必须被测试替换，防止外部调用。"""
    settings = RetrievalSettings.model_validate(
        {"qdrant_api_key": SecretStr("test-only-qdrant-" + "x" * 32)}
    )
    return replace(
        make_context(test_settings, object()),
        retrieval_settings=settings,
        qdrant_client=cast(AsyncQdrantClient, object()),
        embedding_model=cast(TextEmbedding, object()),
    )


@pytest.mark.parametrize(
    "status,has_history",
    [
        ("available", True),
        ("available", False),
        ("degraded", False),
        ("disabled", False),
    ],
)
def test_real_load_retrieve_build_chain_preserves_model_input(
    monkeypatch, current_event, current_state, retrieval_context, status, has_history
):
    """正式三节点串联保留检索语义、历史来源并执行已有脱敏规则。"""

    async def scenario():
        @asynccontextmanager
        async def scope():
            yield cast(AsyncSession, object())

        history = []
        if has_history:
            history.append(
                HistoricalFailureResponse(
                    event_id=13,
                    repository_full_name="team/backend",
                    run_id=999,
                    run_attempt=1,
                    job_key="backend",
                    received_at=datetime(2026, 11, 10, tzinfo=UTC),
                    similarity_score=0.9,
                    failure=current_event.failures[0],
                )
            )
        context = replace(
            retrieval_context,
            session_factory=cast(async_sessionmaker[AsyncSession], scope),
        )
        if status == "disabled":
            context = replace(context, retrieval_settings=None)
        monkeypatch.setattr(
            orchestration,
            "get_ci_failure_context",
            AsyncMock(
                return_value=(current_event, 123456, current_state["received_at"])
            ),
        )
        query = AsyncMock(
            return_value=history,
            side_effect=ApiException("test-only") if status == "degraded" else None,
        )
        monkeypatch.setattr(orchestration, "retrieve_event_history", query)
        builder = StateGraph(
            orchestration.AiDiagnosisGraphState,
            context_schema=orchestration.AiDiagnosisGraphContext,
        )
        builder.add_node("load", orchestration.load_current_failure_node)
        builder.add_node("retrieve", orchestration.retrieve_failure_history_node)
        builder.add_node("build", orchestration.build_diagnosis_evidence_node)
        builder.add_edge(START, "load")
        builder.add_edge("load", "retrieve")
        builder.add_edge("retrieve", "build")
        builder.add_edge("build", END)
        result = await builder.compile().ainvoke({"event_id": 17}, context=context)
        pack = result["evidence_pack"]
        assert pack.event_id == 17
        assert pack.retrieval_status == status
        assert len(pack.history) == int(has_history)
        assert pack.failures[0].message == "token=[REDACTED]"
        assert current_event.failures[0].message == "token=test-only-sensitive-value"
        assert (pack.embedding_model is None) is (status == "disabled")
        if has_history:
            assert pack.history[0].evidence_id == "history:13:0:0"
            assert pack.history[0].message == "token=[REDACTED]"
        if status == "disabled":
            query.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["event", "history", "retrieval_status"])
def test_build_node_rejects_missing_prior_state(
    current_event, retrieval_context, missing
):
    """空历史合法，缺少历史字段则是流程错误，不能混为一谈。"""
    state = {
        "event_id": 17,
        "event": current_event,
        "history": [],
        "retrieval_status": "available",
    }
    state.pop(missing)
    with pytest.raises(ValueError, match="缺少前置"):
        orchestration.build_diagnosis_evidence_node(
            cast(orchestration.AiDiagnosisGraphState, state),
            Runtime(context=retrieval_context),
        )


def test_build_node_rejects_enabled_retrieval_without_model_config(
    current_event, retrieval_context
):
    """构包必须有实际配置来源，不默认伪造检索模型名。"""
    state: orchestration.AiDiagnosisGraphState = {
        "event_id": 17,
        "event": current_event,
        "history": [],
        "retrieval_status": "available",
    }
    with pytest.raises(ValueError, match="缺少模型配置"):
        orchestration.build_diagnosis_evidence_node(
            state, Runtime(context=replace(retrieval_context, retrieval_settings=None))
        )


@pytest.mark.parametrize("status", list(AiDiagnosisClaimStatus))
def test_claim_routes_real_graph_without_duplicate_generation(
    monkeypatch, current_event, test_settings, status
):
    """只有原子抢占成功的状态进入生成；缓存与处理中直接结束。"""

    async def scenario():
        active = False
        generation_calls = []

        @asynccontextmanager
        async def scope():
            nonlocal active
            active = True
            try:
                yield cast(AsyncSession, object())
            finally:
                active = False

        expires = datetime.now(UTC) + timedelta(seconds=120)
        token = uuid4()
        cached_diagnosis = build_diagnosis()
        if status is AiDiagnosisClaimStatus.CLAIMED:
            claim = AiDiagnosisClaimResult(
                event_id=17,
                status=status,
                attempt_count=2,
                lease_token=token,
                lease_expires_at=expires,
            )
        elif status is AiDiagnosisClaimStatus.CACHED:
            claim = AiDiagnosisClaimResult(
                event_id=17,
                status=status,
                attempt_count=2,
                diagnosis_json=cached_diagnosis.model_dump(mode="json"),
                model_name="deepseek-flash",
                input_tokens=10,
                output_tokens=20,
                total_tokens=30,
            )
        else:
            claim = AiDiagnosisClaimResult(
                event_id=17,
                status=status,
                attempt_count=2,
                lease_expires_at=expires,
            )

        async def fake_claim(**kwargs):
            assert active
            assert kwargs["event_id"] == 17
            assert kwargs["cache_identity"] == expected_identity
            return claim

        monkeypatch.setattr(orchestration, "claim_ai_diagnosis", fake_claim)
        context = make_context(test_settings, scope)
        pack = orchestration.build_ai_evidence_pack(current_event)
        expected_identity = orchestration.build_ai_diagnosis_cache_identity(
            pack, test_settings
        )

        async def fake_generate(state):
            assert not active
            assert state["claim_result"] is claim
            assert state["claim_result"].lease_token == token
            generation_calls.append(17)
            return {}

        builder = StateGraph(
            orchestration.AiDiagnosisGraphState,
            context_schema=orchestration.AiDiagnosisGraphContext,
        )
        builder.add_node("claim", orchestration.claim_diagnosis_node)
        builder.add_node("generate", fake_generate)
        builder.add_edge(START, "claim")
        builder.add_conditional_edges(
            "claim",
            orchestration.route_after_claim,
            {"finish": END, "generate": "generate"},
        )
        builder.add_edge("generate", END)
        initial: orchestration.AiDiagnosisGraphState = {
            "event_id": 17,
            "evidence_pack": pack,
        }
        result = await builder.compile().ainvoke(initial, context=context)
        assert not active
        assert "result" not in initial and "claim_result" not in initial
        if status is AiDiagnosisClaimStatus.CLAIMED:
            assert generation_calls == [17]
            assert "result" not in result
        else:
            assert generation_calls == []
            assert "claim_result" not in result
            assert result["result"].attempt_count == 2
            assert not hasattr(result["result"], "lease_token")
            if status is AiDiagnosisClaimStatus.CACHED:
                assert result["result"].diagnosis == cached_diagnosis
                assert result["result"].total_tokens == 30
            else:
                assert result["result"].lease_expires_at == expires

    asyncio.run(scenario())


@pytest.mark.parametrize("missing_pack", [True, False])
def test_claim_rejects_missing_or_mismatched_pack_before_database(
    monkeypatch, current_event, test_settings, missing_pack
):
    """证据归属不明时，不创建诊断任务。"""
    state: orchestration.AiDiagnosisGraphState = {"event_id": 17}
    if not missing_pack:
        state["evidence_pack"] = orchestration.build_ai_evidence_pack(
            current_event
        ).model_copy(update={"event_id": 99})
    claim = AsyncMock()
    monkeypatch.setattr(orchestration, "claim_ai_diagnosis", claim)
    context = make_context(test_settings, object())
    with pytest.raises(ValueError):
        asyncio.run(orchestration.claim_diagnosis_node(state, Runtime(context=context)))
    claim.assert_not_awaited()


@pytest.mark.parametrize(
    "fault", ["event_mismatch", "bad_cache", "missing_token", "missing_expiry"]
)
def test_claim_rejects_untrustworthy_database_result(
    monkeypatch, current_event, test_settings, fault
):
    """不可信缓存或租约不得进入后续模型调用。"""

    @asynccontextmanager
    async def scope():
        yield cast(AsyncSession, object())

    expires = datetime.now(UTC) + timedelta(seconds=120)
    if fault in ("event_mismatch", "bad_cache"):
        claim = AiDiagnosisClaimResult(
            event_id=99 if fault == "event_mismatch" else 17,
            status=AiDiagnosisClaimStatus.CACHED,
            attempt_count=1,
            diagnosis_json={},
        )
    else:
        claim = AiDiagnosisClaimResult(
            event_id=17,
            status=AiDiagnosisClaimStatus.CLAIMED,
            attempt_count=1,
            lease_token=None if fault == "missing_token" else uuid4(),
            lease_expires_at=None if fault == "missing_expiry" else expires,
        )
    monkeypatch.setattr(
        orchestration, "claim_ai_diagnosis", AsyncMock(return_value=claim)
    )
    state: orchestration.AiDiagnosisGraphState = {
        "event_id": 17,
        "evidence_pack": orchestration.build_ai_evidence_pack(current_event),
    }
    with pytest.raises(AiDiagnosisPersistenceUnavailableError):
        asyncio.run(
            orchestration.claim_diagnosis_node(
                state, Runtime(context=make_context(test_settings, scope))
            )
        )


def test_route_rejects_missing_execution_right_and_impossible_result():
    """错误连接或无执行权状态不能被路由到付费生成。"""
    with pytest.raises(AiDiagnosisPersistenceUnavailableError):
        orchestration.route_after_claim({"event_id": 17})
    with pytest.raises(ValueError):
        orchestration.route_after_claim(
            {
                "event_id": 17,
                "result": orchestration.AiDiagnosisOrchestrationResult(
                    event_id=17,
                    status=orchestration.AiDiagnosisOrchestrationStatus.GENERATED,
                    attempt_count=1,
                ),
            }
        )


@pytest.mark.parametrize("has_history", [False, True])
def test_retrieval_available_passes_source_and_preserves_input(
    monkeypatch, current_state, retrieval_context, current_event, has_history
):
    """正常零匹配仍available，命中时保留核实历史及来源边界。"""
    history = []
    if has_history:
        history.append(
            HistoricalFailureResponse(
                event_id=13,
                repository_full_name="team/backend",
                run_id=999,
                run_attempt=1,
                job_key="backend",
                received_at=datetime(2026, 11, 10, tzinfo=UTC),
                similarity_score=0.9,
                failure=current_event.failures[0],
            )
        )
    query = AsyncMock(return_value=history)
    monkeypatch.setattr(orchestration, "retrieve_event_history", query)
    original = dict(current_state)
    result = asyncio.run(
        orchestration.retrieve_failure_history_node(
            current_state, Runtime(context=retrieval_context)
        )
    )
    assert result == {"history": history, "retrieval_status": "available"}
    query.assert_awaited_once_with(
        event=current_event,
        github_repository_id=123456,
        received_at=current_state["received_at"],
        session_factory=retrieval_context.session_factory,
        client=retrieval_context.qdrant_client,
        embedding_model=retrieval_context.embedding_model,
        settings=retrieval_context.retrieval_settings,
    )
    assert current_state == original
    assert not retrieval_context.embedding_semaphore.locked()


@pytest.mark.parametrize("missing", ["event", "github_repository_id", "received_at"])
def test_retrieval_rejects_missing_prior_state(
    monkeypatch, current_state, retrieval_context, missing
):
    """缺少前置读取结果是调用错误，不能伪装成检索降级。"""
    current_state.pop(missing)
    query = AsyncMock()
    monkeypatch.setattr(orchestration, "retrieve_event_history", query)
    with pytest.raises(ValueError, match="缺少前置"):
        asyncio.run(
            orchestration.retrieve_failure_history_node(
                current_state, Runtime(context=retrieval_context)
            )
        )
    query.assert_not_awaited()


@pytest.mark.parametrize(
    "updates,error_type",
    [
        ({"parse_status": "pending"}, AiEvidenceNotReadyError),
        ({"parse_status": "failed"}, AiEvidenceNotReadyError),
        ({"failures": []}, AiEvidenceEmptyError),
    ],
)
def test_retrieval_rejects_undiagnosable_event_before_external_work(
    monkeypatch, current_state, retrieval_context, updates, error_type
):
    """事件尚未可诊断时，不应计算向量或请求Qdrant。"""
    current_state["event"] = current_state["event"].model_copy(update=updates)
    query = AsyncMock()
    monkeypatch.setattr(orchestration, "retrieve_event_history", query)
    with pytest.raises(error_type):
        asyncio.run(
            orchestration.retrieve_failure_history_node(
                current_state, Runtime(context=retrieval_context)
            )
        )
    query.assert_not_awaited()


@pytest.mark.parametrize(
    "field,status",
    [
        ("retrieval_settings", "disabled"),
        ("qdrant_client", "degraded"),
        ("embedding_model", "degraded"),
    ],
)
def test_retrieval_distinguishes_disabled_from_missing_resources(
    monkeypatch, current_state, retrieval_context, field, status
):
    """显式关闭和资源不可用不能使用同一业务状态。"""
    context = replace(retrieval_context, **{field: None})
    query = AsyncMock()
    monkeypatch.setattr(orchestration, "retrieve_event_history", query)
    result = asyncio.run(
        orchestration.retrieve_failure_history_node(
            current_state, Runtime(context=context)
        )
    )
    assert result == {"history": [], "retrieval_status": status}
    query.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        ApiException("test-only-sensitive-exception"),
        CiFailureQueryUnavailableError("test-only-sensitive-exception"),
        OSError("test-only-sensitive-exception"),
    ],
)
def test_retrieval_degrades_classified_infrastructure_faults_without_raw_logs(
    monkeypatch, current_state, retrieval_context, caplog, error
):
    """故障清空历史并标降级，日志不包含原始异常里的敏感文本。"""
    monkeypatch.setattr(
        orchestration, "retrieve_event_history", AsyncMock(side_effect=error)
    )
    result = asyncio.run(
        orchestration.retrieve_failure_history_node(
            current_state, Runtime(context=retrieval_context)
        )
    )
    assert result == {"history": [], "retrieval_status": "degraded"}
    assert type(error).__name__ in caplog.text
    assert "test-only-sensitive-exception" not in caplog.text
    assert not retrieval_context.embedding_semaphore.locked()


@pytest.mark.parametrize(
    "error", [ValueError("bad input"), RuntimeError("bug"), KeyError("missing")]
)
def test_retrieval_preserves_programming_errors(
    monkeypatch, current_state, retrieval_context, error
):
    """程序和输入错误必须暴露，不能被正常降级吞掉。"""
    monkeypatch.setattr(
        orchestration, "retrieve_event_history", AsyncMock(side_effect=error)
    )
    with pytest.raises(type(error)) as caught:
        asyncio.run(
            orchestration.retrieve_failure_history_node(
                current_state, Runtime(context=retrieval_context)
            )
        )
    assert caught.value is error
    assert not retrieval_context.embedding_semaphore.locked()


def test_retrieval_shared_semaphore_serializes_normal_requests(
    monkeypatch, current_state, retrieval_context
):
    """正常并发调用共享许可，检索服务临界区最多一个执行者。"""

    async def scenario():
        active = 0
        peak = 0

        async def query(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return []

        monkeypatch.setattr(orchestration, "retrieve_event_history", query)
        results = await asyncio.gather(
            orchestration.retrieve_failure_history_node(
                current_state, Runtime(context=retrieval_context)
            ),
            orchestration.retrieve_failure_history_node(
                cast(orchestration.AiDiagnosisGraphState, dict(current_state)),
                Runtime(context=retrieval_context),
            ),
        )
        assert peak == 1
        assert all(result["retrieval_status"] == "available" for result in results)
        assert not retrieval_context.embedding_semaphore.locked()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [CiFailureQueryNotFoundError(17), CiFailureQueryUnavailableError("test-only")],
)
def test_load_node_closes_session_and_preserves_error(
    monkeypatch, test_settings, error
):
    """事实读取故障必须终止并传播，不得改写成正常零历史。"""

    async def scenario():
        closed = False

        @asynccontextmanager
        async def scope():
            nonlocal closed
            try:
                yield cast(AsyncSession, object())
            finally:
                closed = True

        monkeypatch.setattr(
            orchestration, "get_ci_failure_context", AsyncMock(side_effect=error)
        )
        context = make_context(test_settings, scope)
        state: orchestration.AiDiagnosisGraphState = {"event_id": 17}
        with pytest.raises(type(error)) as caught:
            await orchestration.load_current_failure_node(
                state, Runtime(context=context)
            )
        assert caught.value is error
        assert closed
        assert state == {"event_id": 17}

    asyncio.run(scenario())

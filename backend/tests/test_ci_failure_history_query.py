"""验证历史查询的Session边界、状态分支和故障归属。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.service import ci_failure_history_query as service
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
)
from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.tests.test_ai_diagnosis_orchestration import build_query_response


def prepare(monkeypatch, event=None):
    active = []

    @asynccontextmanager
    async def scope():
        active.append(True)
        try:
            yield cast(AsyncSession, object())
        finally:
            active.pop()

    source = AsyncMock(
        return_value=(
            event or build_query_response(),
            123,
            datetime(2026, 11, 12, tzinfo=UTC),
        )
    )
    retrieval = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "get_ci_failure_context", source)
    monkeypatch.setattr(service, "retrieve_event_history", retrieval)
    args = {
        "event_id": 42,
        "session_factory": cast(async_sessionmaker[AsyncSession], scope),
        "embedding_semaphore": asyncio.Semaphore(1),
        "retrieval_settings": RetrievalSettings.model_validate(
            {"qdrant_api_key": "test-only-" + "x" * 32}
        ),
        "qdrant_client": cast(AsyncQdrantClient, object()),
        "embedding_model": cast(TextEmbedding, object()),
    }
    return args, source, retrieval, active


@pytest.mark.parametrize(
    "case,expected",
    [
        ("pending", "not_ready"),
        ("failed", "not_ready"),
        ("disabled", "disabled"),
        ("empty", "available"),
        ("missing", "degraded"),
    ],
)
def test_short_circuits_do_not_compute_vectors(monkeypatch, case, expected):
    event = build_query_response()
    if case in ("pending", "failed"):
        event = event.model_copy(update={"parse_status": case})
    elif case == "empty":
        event = event.model_copy(update={"failures": []})
    args, _, retrieval, active = prepare(monkeypatch, event)
    if case == "disabled":
        args["retrieval_settings"] = None
    elif case == "missing":
        args["embedding_model"] = None
    result = asyncio.run(service.get_ci_failure_history(**args))
    assert result.retrieval_status == expected and result.history == []
    retrieval.assert_not_awaited()
    assert active == []


def test_retrieval_uses_closed_session_and_shared_permit(monkeypatch):
    event = build_query_response()
    event = event.model_copy(update={"failures": event.failures * 21})
    args, _, retrieval, active = prepare(monkeypatch, event)

    async def inspect(**kwargs):
        assert active == []
        assert args["embedding_semaphore"].locked()
        assert kwargs["client"] is args["qdrant_client"]
        assert kwargs["embedding_model"] is args["embedding_model"]
        return []

    retrieval.side_effect = inspect
    result = asyncio.run(service.get_ci_failure_history(**args))
    assert result.retrieval_status == "available"
    assert result.current_failures_truncated
    assert not args["embedding_semaphore"].locked()


@pytest.mark.parametrize(
    "error",
    [
        ApiException("sensitive-test-value"),
        OSError("sensitive-test-value"),
        CiFailureQueryUnavailableError("sensitive-test-value"),
    ],
)
def test_history_infrastructure_errors_degrade_without_logging_details(
    monkeypatch, caplog, error
):
    args, _, retrieval, _ = prepare(monkeypatch)
    retrieval.side_effect = error
    result = asyncio.run(service.get_ci_failure_history(**args))
    assert result.retrieval_status == "degraded" and result.history == []
    assert "sensitive-test-value" not in caplog.text
    assert not args["embedding_semaphore"].locked()


@pytest.mark.parametrize(
    "error",
    [
        CiFailureQueryNotFoundError(42),
        CiFailureQueryUnavailableError("source unavailable"),
    ],
)
def test_current_fact_errors_propagate(monkeypatch, error):
    args, source, retrieval, active = prepare(monkeypatch)
    source.side_effect = error
    with pytest.raises(type(error)) as caught:
        asyncio.run(service.get_ci_failure_history(**args))
    assert caught.value is error
    retrieval.assert_not_awaited()
    assert active == []


@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid evidence"),
        RuntimeError("programming error"),
        asyncio.CancelledError(),
    ],
)
def test_program_errors_and_cancellation_are_not_empty_history(monkeypatch, error):
    args, _, retrieval, _ = prepare(monkeypatch)
    retrieval.side_effect = error
    with pytest.raises(type(error)):
        asyncio.run(service.get_ci_failure_history(**args))
    assert not args["embedding_semaphore"].locked()

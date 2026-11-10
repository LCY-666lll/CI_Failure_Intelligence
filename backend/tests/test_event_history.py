"""验证事件级历史检索串联与资源上限。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureQueryResponse
from ci_failure_intelligence.service import failure_history as service
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryUnavailableError,
)
from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def event_context(*, status="parsed", count=1):
    """仅提供编排层实际读取的字段，底层数据校验另有测试覆盖。"""
    return cast(
        CiFailureQueryResponse,
        SimpleNamespace(
            event_id=13,
            parse_status=status,
            failures=[
                SimpleNamespace(testcase_index=i, issue_index=0)
                for i in reversed(range(count))
            ],
        ),
    )


def run_history(event):
    return asyncio.run(
        service.retrieve_event_history(
            event=event,
            github_repository_id=123,
            received_at=datetime(2026, 11, 10, tzinfo=UTC),
            session_factory=cast(async_sessionmaker[AsyncSession], object()),
            client=cast(AsyncQdrantClient, object()),
            embedding_model=cast(TextEmbedding, object()),
            settings=cast(RetrievalSettings, object()),
        )
    )


def test_event_queries_are_ordered_bounded_serial_and_verified_once(monkeypatch):
    positions = []
    candidate_batches = []
    state = {"active": False, "model": None, "client": None, "verified": 0}
    expected = []

    async def search(**kwargs):
        assert not state["active"]
        state["active"] = True
        await asyncio.sleep(0)
        if state["model"] is None:
            state["model"], state["client"] = (
                kwargs["embedding_model"],
                kwargs["client"],
            )
        assert kwargs["embedding_model"] is state["model"]
        assert kwargs["client"] is state["client"]
        assert kwargs["current_event_id"] == 13
        assert kwargs["github_repository_id"] == 123
        positions.append(kwargs["detail"].testcase_index)
        point = models.ScoredPoint(id=len(positions), version=1, score=0.9, payload={})
        candidate_batches.append(point)
        state["active"] = False
        return [point]

    async def verify(**kwargs):
        assert positions == list(range(20))
        assert kwargs["candidates"] == candidate_batches
        assert kwargs["current_event_id"] == 13
        assert kwargs["github_repository_id"] == 123
        state["verified"] += 1
        return expected

    monkeypatch.setattr(service, "search_failure_candidates", search)
    monkeypatch.setattr(service, "load_verified_history", verify)
    assert run_history(event_context(count=31)) is expected
    assert state["verified"] == 1


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_unparsed_event_stops_before_any_query(monkeypatch, status):
    async def forbidden(**kwargs):
        raise AssertionError("状态不允许时不应查询")

    monkeypatch.setattr(service, "search_failure_candidates", forbidden)
    monkeypatch.setattr(service, "load_verified_history", forbidden)
    with pytest.raises(ValueError):
        run_history(event_context(status=status))


def test_zero_failures_does_not_query_either_store(monkeypatch):
    async def forbidden(**kwargs):
        raise AssertionError("零失败不应查询")

    monkeypatch.setattr(service, "search_failure_candidates", forbidden)
    monkeypatch.setattr(service, "load_verified_history", forbidden)
    assert run_history(event_context(count=0)) == []


def test_qdrant_failure_is_not_disguised_as_empty_history(monkeypatch):
    async def unavailable(**kwargs):
        raise ApiException("test-only vector failure")

    async def forbidden(**kwargs):
        raise AssertionError("候选查询失败后不应继续回查")

    monkeypatch.setattr(service, "search_failure_candidates", unavailable)
    monkeypatch.setattr(service, "load_verified_history", forbidden)
    with pytest.raises(ApiException):
        run_history(event_context())


def test_postgres_failure_is_not_disguised_as_empty_history(monkeypatch):
    async def search(**kwargs):
        return [models.ScoredPoint(id=1, version=1, score=0.9, payload={})]

    async def unavailable(**kwargs):
        raise CiFailureQueryUnavailableError("test-only database failure")

    monkeypatch.setattr(service, "search_failure_candidates", search)
    monkeypatch.setattr(service, "load_verified_history", unavailable)
    with pytest.raises(CiFailureQueryUnavailableError):
        run_history(event_context())

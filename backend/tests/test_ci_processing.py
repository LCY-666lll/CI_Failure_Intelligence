"""验证解析到索引的串联、资源边界与故障归属。"""

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.service import ci_processing as service
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
)
from ci_failure_intelligence.service.junit_pending import JunitPendingBatchResult
from ci_failure_intelligence.service.junit_persistence import (
    JunitPersistenceUnavailableError,
)
from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ApiException

from backend.tests.test_ai_diagnosis_orchestration import (
    TrackingSessionFactory,
    as_session_factory,
)
from backend.tests.test_failure_vector_index import make_event


def prepare(monkeypatch):
    factory = TrackingSessionFactory()
    parsing = JunitPendingBatchResult(3, 2, 1, (4, 10))
    parse = AsyncMock(return_value=parsing)

    async def source(session, event_id):
        assert factory.active_sessions == 1
        return make_event(1, event_id=event_id), 123, datetime(2026, 11, 13, tzinfo=UTC)

    read = AsyncMock(side_effect=source)
    index = AsyncMock(return_value=1)
    monkeypatch.setattr(service, "process_pending_junit_batch", parse)
    monkeypatch.setattr(service, "get_ci_failure_context", read)
    monkeypatch.setattr(service, "index_ci_failure_event", index)
    args = {
        "session_factory": as_session_factory(factory),
        "embedding_semaphore": asyncio.Semaphore(1),
        "retrieval_settings": RetrievalSettings.model_validate(
            {"qdrant_api_key": "test-only-" + "x" * 32}
        ),
        "qdrant_client": cast(AsyncQdrantClient, object()),
        "embedding_model": cast(TextEmbedding, object()),
        "limit": 10,
    }
    return args, factory, parsing, parse, read, index


def test_only_successful_ids_are_indexed_with_reused_resources(monkeypatch):
    args, factory, parsing, parse, read, index = prepare(monkeypatch)

    async def inspect(**kwargs):
        assert factory.active_sessions == 0
        assert args["embedding_semaphore"].locked()
        assert kwargs["client"] is args["qdrant_client"]
        assert kwargs["embedding_model"] is args["embedding_model"]
        assert kwargs["settings"] is args["retrieval_settings"]
        return 1 if kwargs["event"].event_id == 4 else 0

    index.side_effect = inspect
    result = asyncio.run(service.process_pending_ci_batch(**args))
    parse.assert_awaited_once_with(args["session_factory"], 10)
    assert [call.args[1] for call in read.await_args_list] == [4, 10]
    assert result.parsing is parsing
    assert result.synced_event_ids == (4, 10)
    assert result.index_failed_event_ids == () and result.written_point_count == 1
    assert not args["embedding_semaphore"].locked()


def test_empty_batch_does_not_query_or_index(monkeypatch):
    args, factory, _, parse, read, index = prepare(monkeypatch)
    parse.return_value = JunitPendingBatchResult(0, 0, 0)
    result = asyncio.run(service.process_pending_ci_batch(**args))
    assert result.synced_event_ids == result.index_failed_event_ids == ()
    assert result.written_point_count == 0 and factory.call_count == 0
    read.assert_not_awaited()
    index.assert_not_awaited()


def test_repairs_share_existing_loop_and_new_events_take_priority(monkeypatch):
    args, _, parsing, _, read, index = prepare(monkeypatch)
    result = asyncio.run(
        service.process_pending_ci_batch(**args, repair_event_ids=(6, 4, 10, 13))
    )
    assert result.parsing is parsing
    assert [call.args[1] for call in read.await_args_list] == [4, 10, 6, 13]
    assert result.synced_event_ids == (4, 10, 6, 13)
    assert index.await_count == 4


def test_repairs_run_even_without_new_pending_events(monkeypatch):
    args, _, _, parse, _, index = prepare(monkeypatch)
    parse.return_value = JunitPendingBatchResult(0, 0, 0)
    index.return_value = 0
    result = asyncio.run(
        service.process_pending_ci_batch(**args, repair_event_ids=(13,))
    )
    assert result.synced_event_ids == (13,)
    assert result.parsing.selected_count == 0 and result.written_point_count == 0


@pytest.mark.parametrize("ids", [(0,), (-1,), tuple(range(1, 102))])
def test_invalid_repair_input_is_rejected_before_parse_writes(monkeypatch, ids):
    args, factory, _, parse, read, index = prepare(monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(service.process_pending_ci_batch(**args, repair_event_ids=ids))
    parse.assert_not_awaited()
    read.assert_not_awaited()
    index.assert_not_awaited()
    assert factory.call_count == 0


@pytest.mark.parametrize(
    "error",
    [
        ApiException("sensitive-test-value"),
        OSError("sensitive-test-value"),
        CiFailureQueryUnavailableError("sensitive-test-value"),
        CiFailureQueryNotFoundError(4),
    ],
)
def test_known_event_failure_does_not_block_next_event(monkeypatch, caplog, error):
    args, factory, parsing, _, read, index = prepare(monkeypatch)
    if isinstance(error, (CiFailureQueryUnavailableError, CiFailureQueryNotFoundError)):
        read.side_effect = [
            error,
            (make_event(1, event_id=10), 123, datetime(2026, 11, 13, tzinfo=UTC)),
        ]
    else:
        index.side_effect = [error, 1]
    result = asyncio.run(service.process_pending_ci_batch(**args))
    assert result.parsing is parsing
    assert result.index_failed_event_ids == (4,) and result.synced_event_ids == (10,)
    assert result.written_point_count == 1 and factory.active_sessions == 0
    assert "sensitive-test-value" not in caplog.text
    assert not args["embedding_semaphore"].locked()


@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid limit"),
        JunitPersistenceUnavailableError("test-only-db-failure"),
    ],
)
def test_parse_batch_failure_stops_before_indexing(monkeypatch, error):
    args, _, _, parse, read, index = prepare(monkeypatch)
    parse.side_effect = error
    with pytest.raises(type(error)):
        asyncio.run(service.process_pending_ci_batch(**args))
    read.assert_not_awaited()
    index.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid evidence"),
        RuntimeError("programming error"),
        asyncio.CancelledError(),
    ],
)
def test_program_error_and_cancel_are_not_batch_success(monkeypatch, error):
    args, factory, _, _, _, index = prepare(monkeypatch)
    index.side_effect = error
    with pytest.raises(type(error)):
        asyncio.run(service.process_pending_ci_batch(**args))
    assert not args["embedding_semaphore"].locked()
    assert factory.active_sessions == 0
    index.assert_awaited_once()


def test_real_batch_and_index_functions_connect_with_local_qdrant(monkeypatch):
    """正式批处理和差异索引串联，失败解析不进入实际本地向量集合。"""
    from ci_failure_intelligence.service import junit_pending
    from ci_failure_intelligence.service.failure_vector_index import (
        index_ci_failure_event,
    )
    from ci_failure_intelligence.service.junit_persistence import (
        JunitPersistenceResult,
        JunitPersistenceStatus,
    )

    from backend.tests.test_failure_vector_index import FakeModel

    factory = TrackingSessionFactory()
    committed_ids = set()

    async def parse_event(session, event_id):
        status = (
            JunitPersistenceStatus.FAILED
            if event_id == 8
            else JunitPersistenceStatus.PARSED
        )
        if status is JunitPersistenceStatus.PARSED:
            committed_ids.add(event_id)
        return JunitPersistenceResult(event_id=event_id, status=status, detail_count=1)

    async def source(session, event_id):
        assert event_id in committed_ids
        return make_event(1, event_id=event_id), 123, datetime(2026, 11, 13, tzinfo=UTC)

    monkeypatch.setattr(
        junit_pending, "_list_pending_event_ids", AsyncMock(return_value=[4, 8, 10])
    )
    monkeypatch.setattr(junit_pending, "process_junit_event", parse_event)
    monkeypatch.setattr(service, "get_ci_failure_context", source)
    monkeypatch.setattr(service, "index_ci_failure_event", index_ci_failure_event)

    async def scenario():
        client, model = AsyncQdrantClient(":memory:"), FakeModel()
        settings = RetrievalSettings.model_validate(
            {
                "qdrant_api_key": "test-only-" + "x" * 32,
                "qdrant_collection": "processing-test",
            }
        )
        try:
            result = await service.process_pending_ci_batch(
                session_factory=as_session_factory(factory),
                embedding_semaphore=asyncio.Semaphore(1),
                retrieval_settings=settings,
                qdrant_client=client,
                embedding_model=cast(TextEmbedding, model),
                limit=10,
            )
            assert result.parsing.parsed_event_ids == (4, 10)
            assert result.parsing.failed_count == 1
            assert result.synced_event_ids == (4, 10)
            assert (
                result.index_failed_event_ids == () and result.written_point_count == 2
            )
            points, _ = await client.scroll(settings.qdrant_collection)
            assert {point.payload["event_id"] for point in points if point.payload} == {
                4,
                10,
            }
            assert len(model.calls) == 2 and factory.active_sessions == 0
        finally:
            await client.close()

    asyncio.run(scenario())

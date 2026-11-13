"""用实际本地Qdrant验证差异同步，不触碰业务集合。"""

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import patch

import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.service.failure_vector_index import index_ci_failure_event
from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient

from backend.tests.test_failure_vector_index import (
    FakeClient,
    FakeModel,
    make_event,
    run_index,
)

SETTINGS = RetrievalSettings.model_validate(
    {"qdrant_api_key": "test-only-" + "x" * 32, "qdrant_collection": "difference-test"}
)


async def sync(event, client, model):
    return await index_ci_failure_event(
        event=event,
        github_repository_id=123,
        received_at=datetime(2026, 11, 8, tzinfo=UTC),
        client=client,
        embedding_model=cast(TextEmbedding, model),
        settings=SETTINGS,
    )


def test_repeat_change_removal_and_zero_failure_preserve_other_events():
    """重复不计算、单条变化只重算一条；清理当前事件不影响另一事件。"""

    async def scenario():
        client, model = AsyncQdrantClient(":memory:"), FakeModel()
        try:
            event = make_event(2)
            assert await sync(event, client, model) == 2
            assert await sync(event, client, model) == 0
            assert len(model.calls) == 1
            changed = event.model_copy(
                update={
                    "failures": [
                        event.failures[0],
                        event.failures[1].model_copy(
                            update={"details": "changed-error"}
                        ),
                    ]
                }
            )
            assert await sync(changed, client, model) == 1
            assert len(model.calls[-1]) == 1
            assert await sync(make_event(1, event_id=14), client, model) == 1
            calls = len(model.calls)
            assert await sync(make_event(1), client, model) == 0
            assert (
                await client.count(SETTINGS.qdrant_collection, exact=True)
            ).count == 2
            assert await sync(make_event(0), client, model) == 0
            points, _ = await client.scroll(
                SETTINGS.qdrant_collection, with_payload=True
            )
            assert len(points) == 1
            assert points[0].payload is not None
            assert points[0].payload["event_id"] == 14
            assert len(model.calls) == calls
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"index_version": "obsolete"},
        {"github_repository_id": 999},
        {"received_at": "2000-01-01T00:00:00+00:00"},
    ],
)
def test_changed_source_metadata_is_repaired(payload):
    """文本没变但版本或来源变化，也必须修复，不能仅按文本摘要跳过。"""

    async def scenario():
        client, model = AsyncQdrantClient(":memory:"), FakeModel()
        try:
            event = make_event(1)
            await sync(event, client, model)
            points, _ = await client.scroll(SETTINGS.qdrant_collection)
            await client.set_payload(
                SETTINGS.qdrant_collection, payload, points=[points[0].id], wait=True
            )
            assert await sync(event, client, model) == 1
            assert len(model.calls) == 2
            assert await sync(event, client, model) == 0
        finally:
            await client.close()

    asyncio.run(scenario())


def test_failed_upsert_does_not_delete_stale_points():
    """新增内容没写成功时，不得先删除旧位置。"""

    async def scenario():
        client, model = AsyncQdrantClient(":memory:"), FakeModel()
        try:
            event = make_event(2)
            await sync(event, client, model)
            changed = event.model_copy(
                update={
                    "failures": [
                        event.failures[0].model_copy(update={"details": "new-error"})
                    ]
                }
            )

            async def fail(**kwargs):
                raise RuntimeError("test-only-write-failure")

            with patch.object(client, "upsert", fail), pytest.raises(RuntimeError):
                await sync(changed, client, model)
            assert (
                await client.count(SETTINGS.qdrant_collection, exact=True)
            ).count == 2
            assert await sync(changed, client, model) == 1
            assert (
                await client.count(SETTINGS.qdrant_collection, exact=True)
            ).count == 1
        finally:
            await client.close()

    asyncio.run(scenario())


def test_failed_delete_can_retry_without_recomputing_vectors():
    """删除失败可再次补偿，已正确写入的部分无需重算。"""

    async def scenario():
        client, model = AsyncQdrantClient(":memory:"), FakeModel()
        try:
            await sync(make_event(2), client, model)

            async def fail(**kwargs):
                raise RuntimeError("test-only-delete-failure")

            with patch.object(client, "delete", fail), pytest.raises(RuntimeError):
                await sync(make_event(1), client, model)
            assert (
                await client.count(SETTINGS.qdrant_collection, exact=True)
            ).count == 2
            assert await sync(make_event(1), client, model) == 0
            assert len(model.calls) == 1
            assert (
                await client.count(SETTINGS.qdrant_collection, exact=True)
            ).count == 1
        finally:
            await client.close()

    asyncio.run(scenario())


def test_partial_batch_retry_only_computes_missing_point():
    client, model = FakeClient(fail_second_write=True), FakeModel()
    event = make_event(33)
    with pytest.raises(RuntimeError):
        run_index(event, client, model)
    client.fail_second_write = False
    assert run_index(event, client, model) == 1
    assert len(model.calls[-1]) == 1 and len(client.stored) == 33

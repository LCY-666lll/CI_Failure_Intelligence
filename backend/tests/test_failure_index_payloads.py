"""验证单事件元数据读取的分页、隔离和故障传播。"""

import asyncio
from typing import cast
from unittest.mock import AsyncMock

import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.service.failure_vector_index import (
    _load_event_index_payloads,
)
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ApiException


def test_payload_scroll_reads_all_pages_and_only_target_event(monkeypatch):
    """用实际本地引擎读取跨两页数据，并检查每次请求不返回向量。"""

    async def scenario():
        client = AsyncQdrantClient(":memory:")
        settings = RetrievalSettings.model_validate(
            {
                "qdrant_api_key": "test-only-" + "x" * 32,
                "qdrant_collection": "payload-test",
            }
        )
        try:
            await client.create_collection(
                settings.qdrant_collection,
                vectors_config=models.VectorParams(
                    size=2, distance=models.Distance.COSINE
                ),
            )
            await client.upsert(
                settings.qdrant_collection,
                points=[
                    models.PointStruct(
                        id=i,
                        vector=[1.0, 0.0],
                        payload={
                            "event_id": 18 if i < 130 else 19,
                            "content_sha256": "test-digest",
                        },
                    )
                    for i in range(131)
                ],
                wait=True,
            )
            original_scroll = client.scroll
            calls = []

            async def tracked_scroll(**kwargs):
                calls.append(kwargs)
                points, cursor = await original_scroll(**kwargs)
                assert all(point.vector is None for point in points)
                return points, cursor

            monkeypatch.setattr(client, "scroll", tracked_scroll)
            result = await _load_event_index_payloads(
                event_id=18, client=client, settings=settings
            )
            assert set(result) == {str(i) for i in range(130)}
            assert all(payload["event_id"] == 18 for payload in result.values())
            assert len(calls) == 2 and calls[0]["offset"] is None
            assert calls[1]["offset"] is not None
            assert all(call["with_vectors"] is False for call in calls)
            assert (
                await _load_event_index_payloads(
                    event_id=999, client=client, settings=settings
                )
                == {}
            )
        finally:
            await client.close()

    asyncio.run(scenario())


def test_scroll_failure_is_not_empty_or_partial_success():
    """第二页失败时整次读取失败，不能将已读第一页误当成完整索引。"""
    error = ApiException("test-only-read-failure")
    client = AsyncMock(spec=AsyncQdrantClient)
    client.scroll.side_effect = [
        ([models.Record(id=1, payload={"event_id": 18})], 2),
        error,
    ]
    settings = RetrievalSettings.model_validate(
        {"qdrant_api_key": "test-only-" + "x" * 32}
    )
    with pytest.raises(ApiException) as caught:
        asyncio.run(
            _load_event_index_payloads(
                event_id=18, client=cast(AsyncQdrantClient, client), settings=settings
            )
        )
    assert caught.value is error
    assert client.scroll.await_count == 2

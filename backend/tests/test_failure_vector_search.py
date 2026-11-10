"""用Qdrant本地引擎验证历史候选的实际过滤行为。"""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from typing import cast

import numpy as np
import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureDetailResponse
from ci_failure_intelligence.service.failure_vector_search import (
    search_failure_candidates,
)
from fastembed import TextEmbedding
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient, models


class FakeModel:
    def __init__(self, empty: bool = False) -> None:
        self.empty = empty

    def embed(self, documents, batch_size):
        assert batch_size == 1
        assert "test-secret-value" not in documents[0]
        if not self.empty:
            yield np.asarray([1.0] + [0.0] * 383, dtype=np.float32)


NOW = datetime(2026, 11, 10, tzinfo=UTC)
SETTINGS = RetrievalSettings(
    qdrant_api_key=SecretStr("test-only-qdrant-key-with-at-least-32-characters"),
    qdrant_collection="history-test",
)
DETAIL = CiFailureDetailResponse(
    id=1,
    testcase_index=0,
    issue_index=0,
    suite_name=None,
    classname=None,
    test_name="test_order",
    issue_type="failure",
    message="token=test-secret-value",
    details="AssertionError",
)


def point(event_id, *, repository=123, received_at=None, version="v1", score=1.0):
    return models.PointStruct(
        id=event_id,
        vector=[score, math.sqrt(1.0 - score**2)] + [0.0] * 382,
        payload={
            "event_id": event_id,
            "github_repository_id": repository,
            "received_at": (received_at or NOW - timedelta(days=1)).isoformat(),
            "index_version": version,
        },
    )


async def query(client, **overrides):
    arguments = {
        "detail": DETAIL,
        "current_event_id": 20,
        "github_repository_id": 123,
        "received_at": NOW,
        "client": client,
        "embedding_model": cast(TextEmbedding, FakeModel()),
        "settings": SETTINGS,
    }
    return await search_failure_candidates(**(arguments | overrides))


def test_history_filters_apply_repository_version_time_and_similarity() -> None:
    async def scenario():
        client = AsyncQdrantClient(":memory:")
        try:
            await client.create_collection(
                SETTINGS.qdrant_collection,
                vectors_config=models.VectorParams(
                    size=384, distance=models.Distance.COSINE
                ),
            )
            await client.upsert(
                SETTINGS.qdrant_collection,
                points=[
                    point(1),
                    point(2, received_at=NOW, score=0.9),
                    point(3, repository=999),
                    point(4, received_at=NOW + timedelta(days=1)),
                    point(5, version="v0"),
                    point(6, score=0.5),
                    point(20, received_at=NOW),
                    point(21, received_at=NOW),
                    point(25),
                ],
                wait=True,
            )
            hits = await query(client)
            assert {hit.id for hit in hits} == {1, 2, 25}
            assert all(hit.payload is not None and hit.vector is None for hit in hits)
            assert [hit.score for hit in hits] == sorted(
                (hit.score for hit in hits), reverse=True
            )
        finally:
            await client.close()

    asyncio.run(scenario())


def test_candidate_count_is_bounded() -> None:
    async def scenario():
        client = AsyncQdrantClient(":memory:")
        try:
            await client.create_collection(
                SETTINGS.qdrant_collection,
                vectors_config=models.VectorParams(
                    size=384, distance=models.Distance.COSINE
                ),
            )
            await client.upsert(
                SETTINGS.qdrant_collection,
                points=[point(i) for i in range(1, 36)],
                wait=True,
            )
            hits = await query(client, current_event_id=100)
            assert len(hits) == 20
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid",
    [
        {"current_event_id": 0},
        {"github_repository_id": 0},
        {"received_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_boundary_rejected_before_model_or_query(invalid):
    with pytest.raises(ValueError):
        asyncio.run(query(cast(AsyncQdrantClient, object()), **invalid))


def test_wrong_vector_count_is_not_treated_as_empty_history() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            query(
                cast(AsyncQdrantClient, object()),
                embedding_model=cast(TextEmbedding, FakeModel(empty=True)),
            )
        )

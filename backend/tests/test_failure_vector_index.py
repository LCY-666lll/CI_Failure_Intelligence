"""验证单事件向量写入的分批、来源绑定与失败边界。"""

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import cast

import numpy as np
import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureQueryResponse
from ci_failure_intelligence.service.failure_index import build_failure_index_text
from ci_failure_intelligence.service.failure_vector_index import index_ci_failure_event
from fastembed import TextEmbedding
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient, models


class FakeModel:
    def __init__(self, short_output: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.short_output = short_output

    def embed(self, texts: list[str], batch_size: int):
        assert batch_size == 1
        self.calls.append(list(texts))
        for _ in texts[:-1] if self.short_output else texts:
            yield np.ones(384, dtype=np.float32)


class FakeClient:
    def __init__(self, fail_second_write: bool = False) -> None:
        self.created = False
        self.batches: list[list[models.PointStruct]] = []
        self.stored: dict[object, models.PointStruct] = {}
        self.fail_second_write = fail_second_write

    async def collection_exists(self, collection_name: str) -> bool:
        return self.created

    async def create_collection(self, **kwargs: object) -> None:
        self.created = True

    async def upsert(
        self, *, collection_name: str, points: list[models.PointStruct], wait: bool
    ) -> None:
        assert wait is True
        if self.fail_second_write and len(self.batches) == 1:
            raise RuntimeError("test-only Qdrant failure")
        self.batches.append(points)
        for point in points:
            self.stored[point.id] = point


def make_event(count: int, event_id: int = 13) -> CiFailureQueryResponse:
    return CiFailureQueryResponse.model_validate(
        {
            "event_id": event_id,
            "repository_full_name": "team/backend",
            "run_id": 1001,
            "run_attempt": 1,
            "job_key": "backend-tests",
            "workflow_name": "Backend CI",
            "job_name": "tests",
            "commit_sha": "a" * 40,
            "head_branch": "main",
            "parse_status": "parsed",
            "parse_error_code": None,
            "parse_error_message": None,
            "parse_finished_at": datetime(2026, 11, 8, tzinfo=UTC),
            "failures": [
                {
                    "id": i + 1,
                    "testcase_index": i,
                    "issue_index": 0,
                    "suite_name": None,
                    "classname": None,
                    "test_name": f"test_{i}",
                    "issue_type": "failure",
                    "message": "token=test-secret-value",
                    "details": "AssertionError",
                }
                for i in range(count)
            ],
        }
    )


def run_index(
    event: CiFailureQueryResponse, client: FakeClient, model: FakeModel
) -> int:
    return asyncio.run(
        index_ci_failure_event(
            event=event,
            github_repository_id=123,
            received_at=datetime(2026, 11, 8, tzinfo=UTC),
            client=cast(AsyncQdrantClient, client),
            embedding_model=cast(TextEmbedding, model),
            settings=RetrievalSettings(
                qdrant_api_key=SecretStr(
                    "test-only-qdrant-key-with-at-least-32-characters"
                ),
            ),
        )
    )


def test_write_batches_bind_vectors_to_their_actual_source_and_text() -> None:
    event, client, model = make_event(33), FakeClient(), FakeModel()

    assert run_index(event, client, model) == 33
    assert [len(batch) for batch in client.batches] == [32, 1]
    for batch, inputs in zip(client.batches, model.calls, strict=True):
        for point, text in zip(batch, inputs, strict=True):
            assert point.payload is not None
            position = point.payload["testcase_index"]
            assert text == build_failure_index_text(event.failures[position])
            assert "test-secret-value" not in text
            assert point.payload["event_id"] == 13
            assert point.payload["github_repository_id"] == 123
            assert (
                point.payload["content_sha256"]
                == hashlib.sha256(text.encode("utf-8")).hexdigest()
            )
            assert "details" not in point.payload
            # 本项目写入稠密向量列表，先确认具体类型再检查维度。
            assert isinstance(point.vector, list)
            assert len(point.vector) == 384


def test_repeated_event_does_not_duplicate_but_new_event_keeps_history() -> None:
    client, model = FakeClient(), FakeModel()
    event = make_event(1)
    run_index(event, client, model)
    run_index(event, client, model)
    assert len(client.stored) == 1
    run_index(event.model_copy(update={"event_id": 14}), client, model)
    assert len(client.stored) == 2


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_unparsed_event_stops_before_embedding_or_qdrant(status: str) -> None:
    client, model = FakeClient(), FakeModel()
    with pytest.raises(ValueError):
        run_index(
            make_event(1).model_copy(update={"parse_status": status}), client, model
        )
    assert not client.created and not client.batches and not model.calls


def test_zero_failure_event_needs_no_embedding_or_collection() -> None:
    client, model = FakeClient(), FakeModel()
    assert run_index(make_event(0), client, model) == 0
    assert not client.created and not model.calls


def test_vector_count_mismatch_cannot_silently_drop_a_failure() -> None:
    client, model = FakeClient(), FakeModel(short_output=True)
    with pytest.raises(ValueError):
        run_index(make_event(2), client, model)
    assert not client.batches


def test_second_batch_failure_is_propagated_with_first_batch_already_written() -> None:
    client, model = FakeClient(fail_second_write=True), FakeModel()
    with pytest.raises(RuntimeError, match="Qdrant failure"):
        run_index(make_event(33), client, model)
    assert len(client.stored) == 32

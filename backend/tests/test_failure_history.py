"""验证历史回查的去重、陈旧内容拒绝及查询资源边界。"""

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import NamedTuple, cast

import pytest
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_detail import (
    CiFailureDetailModel,
)
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureDetailResponse
from ci_failure_intelligence.schemas.failure_history import HistoricalFailureResponse
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryUnavailableError,
)
from ci_failure_intelligence.service.failure_history import load_verified_history
from ci_failure_intelligence.service.failure_index import build_failure_index_text
from pydantic import ValidationError
from qdrant_client import models
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 11, 10, tzinfo=UTC)


class HistoryRow(NamedTuple):
    detail: CiFailureDetailModel
    repository_full_name: str = "team/backend"
    run_id: int = 1001
    run_attempt: int = 1
    job_key: str = "backend-tests"
    received_at: datetime = datetime(2026, 11, 9, tzinfo=UTC)


def make_row(event_id: int) -> HistoryRow:
    return HistoryRow(
        CiFailureDetailModel(
            id=event_id,
            event_id=event_id,
            testcase_index=0,
            issue_index=0,
            suite_name=None,
            classname=None,
            test_name="test_order",
            issue_type="failure",
            message="expected 200, got 500",
            details="AssertionError",
        )
    )


def make_candidate(event_id: int, score: float = 0.9) -> models.ScoredPoint:
    detail = CiFailureDetailResponse.model_validate(
        make_row(event_id)[0], from_attributes=True
    )
    digest = hashlib.sha256(
        build_failure_index_text(detail).encode("utf-8")
    ).hexdigest()
    return models.ScoredPoint(
        id=event_id,
        version=1,
        score=score,
        payload={
            "event_id": event_id,
            "testcase_index": 0,
            "issue_index": 0,
            "content_sha256": digest,
            "index_version": "v1",
        },
    )


class FakeSession:
    def __init__(self, rows=None, failure=False):
        self.rows = rows or []
        self.failure = failure
        self.statement = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def execute(self, statement):
        self.statement = statement
        if self.failure:
            raise SQLAlchemyError("test database error")
        return self

    def all(self):
        return self.rows


def run_history(candidates, session=None, **overrides):
    factory = (lambda: session) if session is not None else object()
    args = {
        "candidates": candidates,
        "current_event_id": 1000,
        "github_repository_id": 123,
        "received_at": NOW,
        "session_factory": cast(async_sessionmaker[AsyncSession], factory),
    }
    return asyncio.run(load_verified_history(**(args | overrides)))


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": True},
        {"testcase_index": False},
        {"issue_index": -1},
        {"index_version": "v0"},
        {"event_id": None},
    ],
)
def test_invalid_payload_is_discarded_without_database_access(changes):
    candidate = make_candidate(1)
    candidate = candidate.model_copy(
        update={"payload": (candidate.payload or {}) | changes}
    )
    assert run_history([candidate]) == []


def test_empty_candidates_do_not_open_a_session() -> None:
    assert run_history([]) == []


def test_dedup_keeps_highest_score_and_rejects_stale_or_missing_details() -> None:
    stale = make_candidate(2)
    stale = stale.model_copy(
        update={"payload": (stale.payload or {}) | {"content_sha256": "0" * 64}}
    )
    session = FakeSession([make_row(1), make_row(2)])
    result = run_history(
        [make_candidate(1), make_candidate(1, 0.95), stale, make_candidate(3)], session
    )
    assert session.closed
    assert len(result) == 1
    assert result[0].event_id == 1 and result[0].similarity_score == 0.95
    assert result[0].failure.details == "AssertionError"


def test_candidate_query_and_final_results_are_bounded_and_stable() -> None:
    session = FakeSession([make_row(i) for i in range(1, 31)])
    result = run_history([make_candidate(i) for i in range(30, 0, -1)], session)
    assert [row.event_id for row in result] == [1, 2, 3, 4, 5]
    assert session.statement is not None
    parameters = session.statement.compile().params
    positions = next(value for value in parameters.values() if isinstance(value, list))
    assert positions == [(i, 0, 0) for i in range(1, 21)]
    where_sql = str(session.statement.whereclause)
    assert "github_repository_id" in where_sql and "parse_status" in where_sql
    assert "(ci_failure_events.received_at, ci_failure_events.id) <" in where_sql
    assert 123 in parameters.values() and "parsed" in parameters.values()


def test_query_failure_is_not_returned_as_empty_history() -> None:
    session = FakeSession(failure=True)
    with pytest.raises(CiFailureQueryUnavailableError):
        run_history([make_candidate(1)], session)
    assert session.closed


@pytest.mark.parametrize(
    "boundary",
    [
        {"current_event_id": 0},
        {"github_repository_id": 0},
        {"received_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_current_boundary_is_rejected(boundary):
    with pytest.raises(ValueError):
        run_history([], **boundary)


def test_missing_database_rows_are_a_valid_empty_result() -> None:
    session = FakeSession()
    assert run_history([make_candidate(1)], session) == []
    assert session.closed


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1])
def test_history_response_rejects_invalid_similarity_scores(score: float) -> None:
    row = make_row(1)
    with pytest.raises(ValidationError):
        HistoricalFailureResponse(
            event_id=1,
            repository_full_name=row.repository_full_name,
            run_id=row.run_id,
            run_attempt=row.run_attempt,
            job_key=row.job_key,
            received_at=row.received_at,
            similarity_score=score,
            failure=CiFailureDetailResponse.model_validate(
                row[0], from_attributes=True
            ),
        )

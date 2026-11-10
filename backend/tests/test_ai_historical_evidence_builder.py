"""验证历史证据转换复用现有脱敏边界并保持真实来源。"""

from datetime import UTC, datetime

import pytest
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureDetailResponse
from ci_failure_intelligence.schemas.failure_history import HistoricalFailureResponse
from ci_failure_intelligence.service import ai_evidence


def make_history() -> HistoricalFailureResponse:
    """创建正文未脱敏且超过AI单条上限的历史数据库响应。"""
    return HistoricalFailureResponse(
        event_id=13,
        repository_full_name="team/backend",
        run_id=1001,
        run_attempt=1,
        job_key="backend-tests",
        received_at=datetime(2026, 11, 2, tzinfo=UTC),
        similarity_score=0.9,
        failure=CiFailureDetailResponse(
            id=6,
            testcase_index=3,
            issue_index=0,
            suite_name="token=suite-secret",
            classname="password=class-secret",
            test_name="secret=test-secret",
            issue_type="error",
            message="api_key=message-secret",
            details="Authorization: Bearer details-secret\n" + "错" * 5000,
        ),
    )


def test_history_builder_reuses_current_failure_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = make_history()
    original_builder = ai_evidence.build_ai_failure_evidence
    calls: list[CiFailureDetailResponse] = []

    def track_conversion(detail: CiFailureDetailResponse):
        calls.append(detail)
        return original_builder(detail)

    monkeypatch.setattr(ai_evidence, "build_ai_failure_evidence", track_conversion)
    result = ai_evidence.build_ai_historical_failure_evidence(history)

    assert calls == [history.failure]
    assert result.evidence_id == "history:13:3:0"
    assert result.event_id == history.event_id
    assert result.run_id == history.run_id
    assert result.similarity_score == history.similarity_score
    assert "id" not in result.model_dump()
    assert "received_at" not in result.model_dump()


def test_history_builder_redacts_bounds_text_and_leaves_database_input_unchanged() -> (
    None
):
    history = make_history()
    before = history.model_dump()
    result = ai_evidence.build_ai_historical_failure_evidence(history)
    serialized = result.model_dump_json()

    for secret in (
        "suite-secret",
        "class-secret",
        "test-secret",
        "message-secret",
        "details-secret",
    ):
        assert secret not in serialized
    assert result.content_truncated is True
    assert len(result.details) == ai_evidence.MAX_DETAILS_CHARACTERS
    assert result.details.endswith(ai_evidence.TRUNCATION_MARKER)
    assert history.model_dump() == before


def test_history_identity_uses_event_and_xml_position_instead_of_detail_row_id() -> (
    None
):
    history = make_history()
    initial = ai_evidence.build_ai_historical_failure_evidence(history)
    reparsed = history.model_copy(
        update={"failure": history.failure.model_copy(update={"id": 999})}
    )
    different_event = history.model_copy(update={"event_id": 17})

    assert ai_evidence.build_ai_historical_failure_evidence(reparsed) == initial
    assert (
        ai_evidence.build_ai_historical_failure_evidence(different_event).evidence_id
        == "history:17:3:0"
    )

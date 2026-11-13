"""验证历史查询接口的状态与来源契约。"""

from datetime import UTC, datetime

import pytest
from ci_failure_intelligence.schemas.failure_history import (
    CiFailureHistoryResponse,
    HistoricalFailureResponse,
)
from pydantic import ValidationError

from backend.tests.test_ai_diagnosis_orchestration import build_query_response


def test_history_response_preserves_source_and_xml_position():
    """前端能够从响应取得历史事件与该事件内的错误位置。"""
    history = HistoricalFailureResponse(
        event_id=13,
        repository_full_name="team/backend",
        run_id=999,
        run_attempt=1,
        job_key="backend",
        received_at=datetime(2026, 11, 10, tzinfo=UTC),
        similarity_score=0.9,
        failure=build_query_response().failures[0],
    )
    response = CiFailureHistoryResponse(
        event_id=17,
        parse_status="parsed",
        retrieval_status="available",
        current_failures_truncated=False,
        history=[history],
    )
    restored = CiFailureHistoryResponse.model_validate_json(response.model_dump_json())
    assert restored == response
    assert restored.history[0].event_id == 13
    assert restored.history[0].failure.testcase_index == 0


def test_history_response_rejects_unbounded_results_and_unknown_status():
    """错误状态或超过五条历史不能进入HTTP响应契约。"""
    payload = {
        "event_id": 17,
        "parse_status": "parsed",
        "retrieval_status": "unknown",
        "current_failures_truncated": False,
        "history": [],
    }
    with pytest.raises(ValidationError):
        CiFailureHistoryResponse.model_validate(payload)
    history = HistoricalFailureResponse(
        event_id=13,
        repository_full_name="team/backend",
        run_id=999,
        run_attempt=1,
        job_key="backend",
        received_at=datetime(2026, 11, 10, tzinfo=UTC),
        similarity_score=0.9,
        failure=build_query_response().failures[0],
    )
    with pytest.raises(ValidationError):
        CiFailureHistoryResponse.model_validate(
            payload | {"retrieval_status": "available", "history": [history] * 6}
        )

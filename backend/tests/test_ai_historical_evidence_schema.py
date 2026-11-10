"""验证历史AI证据能够准确标识来源，并保留现有输入边界。"""

import pytest
from ci_failure_intelligence.schemas.ai_evidence import (
    AiFailureEvidence,
    AiHistoricalFailureEvidence,
)
from pydantic import ValidationError


def make_payload(**changes: object) -> dict[str, object]:
    """构造事件13位置3:0的独立测试输入。"""
    payload: dict[str, object] = {
        "evidence_id": "history:13:3:0",
        "event_id": 13,
        "repository_full_name": "team/backend",
        "run_id": 1001,
        "run_attempt": 1,
        "job_key": "backend-tests",
        "similarity_score": 0.9,
        "testcase_index": 3,
        "issue_index": 0,
        "test_name": "test_create_order",
        "issue_type": "failure",
        "message": None,
        "details": "AssertionError",
        "content_truncated": False,
    }
    payload.update(changes)
    return payload


def test_history_retains_its_source_in_serialized_model_input() -> None:
    evidence = AiHistoricalFailureEvidence.model_validate(make_payload())
    restored = AiHistoricalFailureEvidence.model_validate_json(
        evidence.model_dump_json()
    )
    assert restored == evidence
    assert restored.evidence_id == "history:13:3:0"
    assert restored.event_id == 13
    assert restored.run_id == 1001


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": 14},
        {"testcase_index": 4},
        {"issue_index": 1},
        {"evidence_id": "history:13:03:0"},
    ],
)
def test_history_id_must_match_its_actual_source(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="历史证据编号"):
        AiHistoricalFailureEvidence.model_validate(make_payload(**changes))


def test_current_and_historical_id_formats_remain_distinct() -> None:
    with pytest.raises(ValidationError):
        AiHistoricalFailureEvidence.model_validate(
            make_payload(evidence_id="failure:3:0")
        )
    with pytest.raises(ValidationError):
        AiFailureEvidence.model_validate(
            {
                "evidence_id": "history:13:3:0",
                "testcase_index": 3,
                "issue_index": 0,
                "test_name": "test_create_order",
                "issue_type": "failure",
                "details": "AssertionError",
                "content_truncated": False,
            }
        )


@pytest.mark.parametrize("score", [float("inf"), float("nan"), -0.1])
def test_history_rejects_invalid_similarity_scores(score: float) -> None:
    with pytest.raises(ValidationError):
        AiHistoricalFailureEvidence.model_validate(make_payload(similarity_score=score))


@pytest.mark.parametrize(
    "changes",
    [
        {"details": "x" * 4001},
        {"unknown": "unexpected"},
        {"event_id": "13"},
    ],
)
def test_history_inherits_text_limits_extra_rejection_and_strict_types(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AiHistoricalFailureEvidence.model_validate(make_payload(**changes))

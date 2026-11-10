"""验证模型证据包的历史来源、检索状态和完整性契约。"""

import pytest
from ci_failure_intelligence.schemas.ai_evidence import (
    AiEvidencePack,
    AiHistoricalFailureEvidence,
)
from pydantic import ValidationError

from backend.tests.test_ai_evidence_schema import build_pack
from backend.tests.test_ai_historical_evidence_schema import make_payload


def make_history_pack(**changes: object) -> AiEvidencePack:
    """在既有当前证据上加入一条有效历史及检索来源。"""
    payload = build_pack().model_dump()
    payload.update(
        history=[AiHistoricalFailureEvidence.model_validate(make_payload())],
        retrieval_status="available",
        embedding_model="test-only-embedding-model",
    )
    payload.update(changes)
    return AiEvidencePack.model_validate(payload)


def test_existing_current_only_call_defaults_to_disabled_and_owns_its_list() -> None:
    first, second = build_pack(), build_pack()
    assert first.history == second.history == []
    assert first.history is not second.history
    assert first.retrieval_status == "disabled"
    assert first.embedding_model is None


def test_history_source_survives_final_pack_json_serialization() -> None:
    pack = make_history_pack()
    restored = AiEvidencePack.model_validate_json(pack.model_dump_json())
    assert restored.history[0].event_id == 13
    assert restored.history[0].evidence_id == "history:13:3:0"
    assert restored.failures[0].evidence_id == "failure:3:0"
    assert restored.history[0].run_id == 1001


@pytest.mark.parametrize("retrieval_status", ["available", "degraded"])
def test_enabled_retrieval_can_have_zero_history(retrieval_status: str) -> None:
    pack = make_history_pack(history=[], retrieval_status=retrieval_status)
    assert pack.retrieval_status == retrieval_status
    assert not pack.history


@pytest.mark.parametrize("retrieval_status", ["disabled", "degraded"])
def test_nonavailable_retrieval_cannot_carry_history(retrieval_status: str) -> None:
    with pytest.raises(ValidationError, match="只有检索正常"):
        make_history_pack(retrieval_status=retrieval_status)


@pytest.mark.parametrize("retrieval_status", ["available", "degraded"])
def test_enabled_retrieval_requires_its_model_name(retrieval_status: str) -> None:
    with pytest.raises(ValidationError, match="必须记录"):
        make_history_pack(
            history=[], retrieval_status=retrieval_status, embedding_model=None
        )


def test_disabled_retrieval_cannot_claim_it_used_an_embedding_model() -> None:
    with pytest.raises(ValidationError, match="未启用"):
        make_history_pack(history=[], retrieval_status="disabled")


def test_repeated_history_and_current_event_as_history_are_rejected() -> None:
    history = AiHistoricalFailureEvidence.model_validate(make_payload())
    with pytest.raises(ValidationError, match="不能重复"):
        make_history_pack(history=[history, history])
    with pytest.raises(ValidationError, match="自己的历史"):
        make_history_pack(event_id=13)


def test_history_text_clipping_requires_its_own_truncation_flag() -> None:
    history = AiHistoricalFailureEvidence.model_validate(
        make_payload(content_truncated=True)
    )
    with pytest.raises(ValidationError, match="history_truncated"):
        make_history_pack(history=[history])
    pack = make_history_pack(history=[history], history_truncated=True)
    assert pack.truncated is False
    assert pack.history_truncated is True


def test_more_than_five_distinct_history_items_are_rejected() -> None:
    histories = [
        AiHistoricalFailureEvidence.model_validate(
            make_payload(event_id=i, evidence_id=f"history:{i}:3:0")
        )
        for i in range(10, 16)
    ]
    with pytest.raises(ValidationError):
        make_history_pack(history=histories)

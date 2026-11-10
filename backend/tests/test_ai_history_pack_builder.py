"""验证当前优先、历史剩余预算及最终模型JSON的统一字节边界。"""

from datetime import UTC, datetime

import pytest
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureQueryResponse
from ci_failure_intelligence.schemas.failure_history import HistoricalFailureResponse
from ci_failure_intelligence.service import ai_evidence
from ci_failure_intelligence.service.ai_diagnosis import serialize_ai_evidence_pack

from backend.tests.test_ai_evidence_pack import build_detail, build_event

EMBEDDING_MODEL = "test-only-embedding-model"


def make_history(
    event_id: int = 13,
    *,
    details: str = "AssertionError: expected 200 but received 500",
    score: float = 0.9,
) -> HistoricalFailureResponse:
    """构造按检索排序交给builder的一条已核实历史。"""
    return HistoricalFailureResponse(
        event_id=event_id,
        repository_full_name="owner/repository",
        run_id=1000 + event_id,
        run_attempt=1,
        job_key="backend_tests",
        received_at=datetime(2026, 11, 2, tzinfo=UTC),
        similarity_score=score,
        failure=build_detail(details=details).model_copy(
            update={"message": "token=history-secret-value"}
        ),
    )


def make_pack(
    event: CiFailureQueryResponse,
    history: list[HistoricalFailureResponse],
):
    return ai_evidence.build_ai_evidence_pack(
        event,
        history=history,
        retrieval_status="available",
        embedding_model=EMBEDDING_MODEL,
    )


def test_current_and_history_are_separate_redacted_and_keep_source() -> None:
    pack = make_pack(build_event(), [make_history()])

    assert pack.failures[0].evidence_id == "failure:0:0"
    assert pack.history[0].evidence_id == "history:13:0:0"
    assert pack.history[0].run_id == 1013
    assert pack.history[0].message == "token=[REDACTED]"
    assert not pack.truncated and not pack.history_truncated
    assert "history-secret-value" not in serialize_ai_evidence_pack(pack)


def test_only_first_five_history_items_are_included_and_omission_is_declared() -> None:
    pack = make_pack(build_event(), [make_history(i) for i in range(10, 16)])
    assert [item.event_id for item in pack.history] == [10, 11, 12, 13, 14]
    assert pack.history_truncated
    assert not pack.truncated


def test_history_never_displaces_current_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = build_event(
        failures=[build_detail(testcase_index=0), build_detail(testcase_index=1)]
    )
    current_only = make_pack(event, [])
    budget = len(serialize_ai_evidence_pack(current_only).encode("utf-8"))
    monkeypatch.setattr(ai_evidence, "MAX_EVIDENCE_PACK_BYTES", budget)

    pack = make_pack(event, [make_history(details="历史错误" * 100)])

    assert pack.included_failure_count == 2
    assert not pack.truncated
    assert pack.history == [] and pack.history_truncated
    assert len(serialize_ai_evidence_pack(pack).encode("utf-8")) <= budget


def test_overflowing_later_history_preserves_accepted_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = build_event()
    first = make_history(13, details="short history")
    with_first = make_pack(event, [first])
    budget = len(serialize_ai_evidence_pack(with_first).encode("utf-8"))
    monkeypatch.setattr(ai_evidence, "MAX_EVIDENCE_PACK_BYTES", budget)

    pack = make_pack(event, [first, make_history(14, details="x" * 4000)])

    assert [item.event_id for item in pack.history] == [13]
    assert pack.history_truncated
    assert len(serialize_ai_evidence_pack(pack).encode("utf-8")) <= budget


def test_exact_final_json_boundary_includes_unicode_escaping_and_float_notation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = build_event()
    history = [make_history(details='数据库连接失败\n"\t' * 30, score=1e-7)]
    complete = make_pack(event, history)
    serialized = serialize_ai_evidence_pack(complete)
    budget = len(serialized.encode("utf-8"))
    assert budget > len(serialized)

    monkeypatch.setattr(ai_evidence, "MAX_EVIDENCE_PACK_BYTES", budget)
    assert len(make_pack(event, history).history) == 1

    monkeypatch.setattr(ai_evidence, "MAX_EVIDENCE_PACK_BYTES", budget - 1)
    clipped = make_pack(event, history)
    assert not clipped.history and clipped.history_truncated
    assert len(serialize_ai_evidence_pack(clipped).encode("utf-8")) <= budget - 1


def test_retrieval_metadata_is_counted_before_accepting_first_current_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = build_event()
    disabled = ai_evidence.build_ai_evidence_pack(event)
    budget = len(serialize_ai_evidence_pack(disabled).encode("utf-8"))
    monkeypatch.setattr(ai_evidence, "MAX_EVIDENCE_PACK_BYTES", budget)

    with pytest.raises(ai_evidence.AiEvidencePackTooLargeError):
        ai_evidence.build_ai_evidence_pack(
            event,
            retrieval_status="available",
            embedding_model="x" * 200,
        )


def test_historical_text_clipping_sets_its_own_flag() -> None:
    pack = make_pack(build_event(), [make_history(details="错" * 5000)])
    assert pack.history[0].content_truncated
    assert pack.history_truncated and not pack.truncated
    assert len(serialize_ai_evidence_pack(pack).encode("utf-8")) <= 32 * 1024


def test_builder_does_not_mutate_current_event_or_history_input() -> None:
    event, history = build_event(), [make_history()]
    before_event, before_history = event.model_dump(), history[0].model_dump()
    make_pack(event, history)
    assert event.model_dump() == before_event
    assert history[0].model_dump() == before_history


@pytest.mark.parametrize("status", ["degraded", "disabled"])
def test_nonavailable_retrieval_rejects_supplied_history(status: str) -> None:
    with pytest.raises(ValueError):
        ai_evidence.build_ai_evidence_pack(
            build_event(),
            history=[make_history()],
            retrieval_status=status,  # pyright: ignore[reportArgumentType]
            embedding_model=EMBEDDING_MODEL if status == "degraded" else None,
        )


@pytest.mark.parametrize("status", ["available", "degraded"])
def test_empty_history_keeps_actual_retrieval_status(status: str) -> None:
    pack = ai_evidence.build_ai_evidence_pack(
        build_event(),
        retrieval_status=status,  # pyright: ignore[reportArgumentType]
        embedding_model=EMBEDDING_MODEL,
    )
    assert pack.history == [] and not pack.history_truncated
    assert pack.retrieval_status == status


def test_current_and_history_quantity_limits_share_one_final_budget() -> None:
    event = build_event(failures=[build_detail(testcase_index=i) for i in range(21)])
    pack = make_pack(event, [make_history()])
    assert pack.total_failure_count == 21 and pack.included_failure_count == 20
    assert pack.truncated and len(pack.history) == 1
    assert len(serialize_ai_evidence_pack(pack).encode("utf-8")) <= 32 * 1024

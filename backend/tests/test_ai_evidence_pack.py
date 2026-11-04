"""验证完整 AI evidence pack 的状态、数量、顺序和字节边界。"""

from typing import Literal

import pytest
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
    CiFailureQueryResponse,
)
from ci_failure_intelligence.service import ai_evidence
from ci_failure_intelligence.service.ai_evidence import (
    MAX_EVIDENCE_PACK_BYTES,
    AiEvidenceEmptyError,
    AiEvidenceNotReadyError,
    AiEvidencePackTooLargeError,
    build_ai_evidence_pack,
)


def build_detail(
    *,
    detail_id: int = 1,
    testcase_index: int = 0,
    issue_index: int = 0,
    details: str = "AssertionError: expected 200 but received 500",
) -> CiFailureDetailResponse:
    """构造一条通过查询响应校验的测试失败明细。"""

    return CiFailureDetailResponse(
        id=detail_id,
        testcase_index=testcase_index,
        issue_index=issue_index,
        suite_name="backend",
        classname="tests.test_orders",
        test_name=f"test_order_{testcase_index}_{issue_index}",
        issue_type="failure",
        message="expected 200 but received 500",
        details=details,
    )


def build_event(
    *,
    parse_status: Literal["pending", "parsed", "failed"] = "parsed",
    failures: list[CiFailureDetailResponse] | None = None,
) -> CiFailureQueryResponse:
    """构造包含运行身份和可替换失败列表的查询响应。"""

    return CiFailureQueryResponse(
        event_id=42,
        repository_full_name="owner/repository",
        run_id=1001,
        run_attempt=1,
        job_key="backend_tests",
        workflow_name="Backend CI",
        job_name="Backend tests (Python 3.12)",
        commit_sha="a" * 40,
        head_branch="main",
        parse_status=parse_status,
        parse_error_code=None,
        parse_error_message=None,
        parse_finished_at=None,
        failures=failures if failures is not None else [build_detail()],
    )


def test_parsed_event_builds_stably_ordered_complete_pack() -> None:
    """数据库结果即使乱序，模型证据也应恢复原 XML 位置顺序。"""

    event = build_event(
        failures=[
            build_detail(detail_id=3, testcase_index=4, issue_index=1),
            build_detail(detail_id=1, testcase_index=1, issue_index=1),
            build_detail(detail_id=2, testcase_index=4, issue_index=0),
        ]
    )

    pack = build_ai_evidence_pack(event)

    assert [failure.evidence_id for failure in pack.failures] == [
        "failure:1:1",
        "failure:4:0",
        "failure:4:1",
    ]
    assert pack.total_failure_count == 3
    assert pack.included_failure_count == 3
    assert pack.truncated is False
    assert len(pack.model_dump_json().encode("utf-8")) <= MAX_EVIDENCE_PACK_BYTES


@pytest.mark.parametrize("parse_status", ["pending", "failed"])
def test_event_without_successful_parse_is_rejected(
    parse_status: Literal["pending", "failed"],
) -> None:
    """尚无可信结构化结果的事件不能进入后续付费模型调用。"""

    event = build_event(parse_status=parse_status)

    with pytest.raises(AiEvidenceNotReadyError) as error_info:
        build_ai_evidence_pack(event)

    assert error_info.value.event_id == 42
    assert error_info.value.parse_status == parse_status


def test_parsed_event_without_failures_is_rejected() -> None:
    """合法零失败报告不应伪装成可执行 AI 诊断的事件。"""

    event = build_event(failures=[])

    with pytest.raises(AiEvidenceEmptyError) as error_info:
        build_ai_evidence_pack(event)

    assert error_info.value.event_id == 42


def test_only_first_twenty_failures_are_included() -> None:
    """超过第一版数量预算的节点必须省略并声明证据不完整。"""

    failures = [
        build_detail(
            detail_id=index + 1,
            testcase_index=index,
        )
        for index in range(21)
    ]

    pack = build_ai_evidence_pack(build_event(failures=failures))

    assert pack.total_failure_count == 21
    assert pack.included_failure_count == 20
    assert len(pack.failures) == 20
    assert pack.failures[-1].evidence_id == "failure:19:0"
    assert pack.truncated is True


def test_overflowing_second_failure_keeps_first_accepted_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后续节点使整包超限时，应返回此前已通过检查的完整前缀。"""

    event = build_event(
        failures=[
            build_detail(detail_id=1, testcase_index=0, details="short"),
            build_detail(
                detail_id=2,
                testcase_index=1,
                details="x" * 4000,
            ),
        ]
    )
    monkeypatch.setattr(ai_evidence, "MAX_EVIDENCE_PACK_BYTES", 1000)

    pack = build_ai_evidence_pack(event)

    assert pack.total_failure_count == 2
    assert pack.included_failure_count == 1
    assert pack.failures[0].evidence_id == "failure:0:0"
    assert pack.truncated is True
    assert len(pack.model_dump_json().encode("utf-8")) <= 1000


def test_first_failure_above_byte_budget_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连第一条证据都无法容纳时，不能返回没有诊断价值的空包。"""

    monkeypatch.setattr(ai_evidence, "MAX_EVIDENCE_PACK_BYTES", 100)

    with pytest.raises(AiEvidencePackTooLargeError) as error_info:
        build_ai_evidence_pack(build_event())

    assert error_info.value.event_id == 42
    assert error_info.value.actual_bytes > 100


def test_multibyte_text_is_measured_as_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文证据必须按真实 UTF-8 字节计算，不能按 Python 字符数放行。"""

    event = build_event(
        failures=[
            build_detail(details="数据库连接失败" * 200),
        ]
    )
    pack = build_ai_evidence_pack(event)
    serialized_json = pack.model_dump_json()
    actual_bytes = len(serialized_json.encode("utf-8"))

    assert actual_bytes > len(serialized_json)

    monkeypatch.setattr(
        ai_evidence,
        "MAX_EVIDENCE_PACK_BYTES",
        actual_bytes - 1,
    )

    with pytest.raises(AiEvidencePackTooLargeError) as error_info:
        build_ai_evidence_pack(event)

    assert error_info.value.actual_bytes == actual_bytes

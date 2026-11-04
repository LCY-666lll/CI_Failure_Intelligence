"""验证 AI evidence pack 的字段和内部一致性契约。"""

import pytest
from ci_failure_intelligence.schemas.ai_evidence import (
    AiEvidencePack,
    AiFailureEvidence,
)
from pydantic import ValidationError


def build_failure(**overrides: object) -> AiFailureEvidence:
    """构造一条默认完整且未截断的测试失败证据。"""

    payload: dict[str, object] = {
        "evidence_id": "failure:3:0",
        "testcase_index": 3,
        "issue_index": 0,
        "suite_name": "backend",
        "classname": "tests.test_orders",
        "test_name": "test_create_order",
        "issue_type": "failure",
        "message": "expected 200 but received 500",
        "details": "test-only traceback",
        "content_truncated": False,
    }
    payload.update(overrides)
    return AiFailureEvidence.model_validate(payload)


def build_pack(**overrides: object) -> AiEvidencePack:
    """构造只包含一条完整证据的默认测试 evidence pack。"""

    payload: dict[str, object] = {
        "event_id": 42,
        "repository_full_name": "owner/repository",
        "run_id": 1001,
        "run_attempt": 1,
        "job_key": "backend_tests",
        "workflow_name": "Backend CI",
        "job_name": "Backend tests (Python 3.12)",
        "commit_sha": "a" * 40,
        "head_branch": "main",
        "total_failure_count": 1,
        "included_failure_count": 1,
        "truncated": False,
        "failures": [build_failure()],
    }
    payload.update(overrides)
    return AiEvidencePack.model_validate(payload)


def test_complete_evidence_pack_is_valid() -> None:
    """完整的一条证据应保留稳定位置编号和未截断语义。"""

    pack = build_pack()

    assert pack.failures[0].evidence_id == "failure:3:0"
    assert pack.included_failure_count == len(pack.failures)
    assert pack.truncated is False


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("evidence_id", "detail:3:0"),
        ("evidence_id", "failure:-1:0"),
        ("issue_type", "skipped"),
        ("test_name", ""),
    ],
)
def test_failure_evidence_rejects_invalid_identity_or_type(
    field_name: str,
    invalid_value: object,
) -> None:
    """模型证据不能使用未知编号、负位置或非失败节点。"""

    with pytest.raises(ValidationError):
        build_failure(**{field_name: invalid_value})


def test_pack_rejects_included_count_different_from_actual_list() -> None:
    """included 数量不能与实际发送的证据列表不一致。"""

    with pytest.raises(ValidationError, match="included_failure_count"):
        build_pack(included_failure_count=2)


def test_pack_rejects_included_count_above_total_count() -> None:
    """实际发送数量不能超过报告记录的总失败数量。"""

    failures = [
        build_failure(),
        build_failure(
            evidence_id="failure:7:0",
            testcase_index=7,
        ),
    ]

    with pytest.raises(ValidationError, match="不能超过"):
        build_pack(
            total_failure_count=1,
            included_failure_count=2,
            truncated=True,
            failures=failures,
        )


def test_pack_requires_truncated_when_some_failures_are_omitted() -> None:
    """报告还有未选入的失败节点时必须明确声明证据不完整。"""

    with pytest.raises(ValidationError, match="truncated"):
        build_pack(total_failure_count=2)


def test_pack_requires_truncated_when_evidence_text_is_clipped() -> None:
    """任何单条证据文本被截断时，整个 evidence pack 必须标记截断。"""

    with pytest.raises(ValidationError, match="truncated"):
        build_pack(failures=[build_failure(content_truncated=True)])


def test_pack_accepts_explicitly_truncated_partial_evidence() -> None:
    """明确标记后可以发送部分失败以及被截断的单条文本。"""

    pack = build_pack(
        total_failure_count=3,
        truncated=True,
        failures=[build_failure(content_truncated=True)],
    )

    assert pack.total_failure_count == 3
    assert pack.included_failure_count == 1
    assert pack.truncated is True


def test_pack_rejects_more_than_twenty_evidence_items() -> None:
    """模型输入不能绕过第一版最多二十条失败的资源边界。"""

    failures = [
        build_failure(
            evidence_id=f"failure:{testcase_index}:0",
            testcase_index=testcase_index,
        )
        for testcase_index in range(21)
    ]

    with pytest.raises(ValidationError):
        build_pack(
            total_failure_count=21,
            included_failure_count=20,
            truncated=True,
            failures=failures,
        )

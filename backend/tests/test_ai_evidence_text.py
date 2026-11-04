"""验证 AI 失败证据的确定性脱敏和字符截断规则。"""

import pytest
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
)
from ci_failure_intelligence.service.ai_evidence import (
    MAX_DETAILS_CHARACTERS,
    REDACTED_VALUE,
    TRUNCATION_MARKER,
    build_ai_failure_evidence,
    redact_sensitive_text,
    sanitize_evidence_text,
)


@pytest.mark.parametrize(
    ("source_text", "expected_text"),
    [
        (
            "Authorization: Bearer bearer-secret-value",
            "Authorization: Bearer [REDACTED]",
        ),
        (
            "authorization=Basic basic-secret-value",
            "authorization=Basic [REDACTED]",
        ),
        ("api_key=api-secret-value", "api_key=[REDACTED]"),
        ("access-token: token-secret-value", "access-token=[REDACTED]"),
        ("password='database-secret'", "password=[REDACTED]"),
        ("secret=application-secret", "secret=[REDACTED]"),
    ],
)
def test_named_credentials_are_redacted(
    source_text: str,
    expected_text: str,
) -> None:
    """认证头和常见密钥字段应保留上下文但删除真实值。"""

    assert redact_sensitive_text(source_text) == expected_text


@pytest.mark.parametrize(
    "secret_value",
    [
        "sk-1234567890abcdef",
        "github_pat_1234567890abcdef",
        "ghp_1234567890abcdef",
        "gho_1234567890abcdef",
        "ghu_1234567890abcdef",
        "ghs_1234567890abcdef",
        "ghr_1234567890abcdef",
    ],
)
def test_known_token_prefixes_are_redacted_without_field_name(
    secret_value: str,
) -> None:
    """直接出现在 traceback 中的常见平台令牌也不能进入模型输入。"""

    result = redact_sensitive_text(f"request failed with {secret_value}")

    assert result == f"request failed with {REDACTED_VALUE}"
    assert secret_value not in result


def test_normal_traceback_text_is_preserved() -> None:
    """不含已知凭据的错误证据不应被无理由改写。"""

    source_text = "AssertionError: expected 200 but received 500"

    assert redact_sensitive_text(source_text) == source_text


def test_optional_missing_text_stays_none() -> None:
    """JUnit 可选属性缺失时不应被转换成空字符串。"""

    assert sanitize_evidence_text(None, max_characters=100) == (None, False)


def test_text_within_limit_is_redacted_without_truncation() -> None:
    """脱敏后的短文本应保留完整上下文并报告未截断。"""

    result, truncated = sanitize_evidence_text(
        "token=real-secret-value request failed",
        max_characters=100,
    )

    assert result == "token=[REDACTED] request failed"
    assert truncated is False


def test_long_text_is_truncated_to_exact_character_limit() -> None:
    """长文本应包含明确标记，并且最终长度不超过调用方上限。"""

    result, truncated = sanitize_evidence_text(
        "错" * 100,
        max_characters=40,
    )

    assert result is not None
    assert len(result) == 40
    assert result.endswith(TRUNCATION_MARKER)
    assert truncated is True


def test_redaction_happens_before_truncation() -> None:
    """长凭据必须先完整删除，不能因截断残留部分秘密。"""

    secret_value = "sk-" + "a" * 80
    result, truncated = sanitize_evidence_text(
        f"provider key={secret_value} followed by diagnostics",
        max_characters=35,
    )

    assert result is not None
    assert secret_value not in result
    assert "sk-" not in result
    assert result.endswith(TRUNCATION_MARKER)
    assert truncated is True


def test_limit_must_leave_room_for_truncation_marker() -> None:
    """配置上限过小时应明确失败，不能返回超过上限的截断结果。"""

    with pytest.raises(ValueError, match="截断标记"):
        sanitize_evidence_text(
            "test-only evidence",
            max_characters=len(TRUNCATION_MARKER),
        )


def test_failure_detail_is_mapped_to_stable_ai_evidence() -> None:
    """证据编号应来自 XML 位置，并完整保留允许发送的失败字段。"""

    detail = CiFailureDetailResponse(
        id=999,
        testcase_index=3,
        issue_index=1,
        suite_name="backend",
        classname="tests.test_orders",
        test_name="test_create_order",
        issue_type="error",
        message="teardown failed",
        details="RuntimeError: database cleanup failed",
    )

    evidence = build_ai_failure_evidence(detail)

    assert evidence.model_dump() == {
        "evidence_id": "failure:3:1",
        "testcase_index": 3,
        "issue_index": 1,
        "suite_name": "backend",
        "classname": "tests.test_orders",
        "test_name": "test_create_order",
        "issue_type": "error",
        "message": "teardown failed",
        "details": "RuntimeError: database cleanup failed",
        "content_truncated": False,
    }
    assert "999" not in evidence.evidence_id


def test_failure_evidence_redacts_every_text_field() -> None:
    """数据库明细中的常见凭据不能通过任一文本字段进入模型证据。"""

    detail = CiFailureDetailResponse(
        id=1,
        testcase_index=0,
        issue_index=0,
        suite_name="token=suite-secret",
        classname="password=class-secret",
        test_name="secret=test-secret",
        issue_type="failure",
        message="api_key=message-secret",
        details="Authorization: Bearer details-secret",
    )

    evidence = build_ai_failure_evidence(detail)
    serialized_evidence = evidence.model_dump_json()

    assert serialized_evidence.count(REDACTED_VALUE) == 5
    for secret_value in (
        "suite-secret",
        "class-secret",
        "test-secret",
        "message-secret",
        "details-secret",
    ):
        assert secret_value not in serialized_evidence


def test_long_failure_details_are_bounded_and_marked_as_truncated() -> None:
    """超长堆栈必须精确限长，并向上游声明当前证据并不完整。"""

    detail = CiFailureDetailResponse(
        id=1,
        testcase_index=0,
        issue_index=0,
        suite_name=None,
        classname=None,
        test_name="test_large_traceback",
        issue_type="failure",
        message=None,
        details="错" * (MAX_DETAILS_CHARACTERS + 100),
    )

    evidence = build_ai_failure_evidence(detail)

    assert evidence.suite_name is None
    assert evidence.classname is None
    assert evidence.message is None
    assert len(evidence.details) == MAX_DETAILS_CHARACTERS
    assert evidence.details.endswith(TRUNCATION_MARKER)
    assert evidence.content_truncated is True

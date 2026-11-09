"""验证检索文本契约、脱敏和对持久化身份的独立性。"""

from ci_failure_intelligence.schemas.ci_failure_query import CiFailureDetailResponse
from ci_failure_intelligence.service.failure_index import build_failure_index_text


def make_detail(**overrides: object) -> CiFailureDetailResponse:
    data: dict[str, object] = {
        "id": 1,
        "testcase_index": 0,
        "issue_index": 0,
        "suite_name": "backend",
        "classname": "tests.test_orders",
        "test_name": "test_create_order",
        "issue_type": "failure",
        "message": "Expected 200, received 500",
        "details": "AssertionError: order creation failed",
    }
    return CiFailureDetailResponse.model_validate(data | overrides)


def test_index_text_preserves_fixed_order_and_error_context() -> None:
    assert build_failure_index_text(make_detail()) == (
        "issue_type: failure\n"
        "message: Expected 200, received 500\n"
        "test_name: test_create_order\n"
        "classname: tests.test_orders\n"
        "suite_name: backend\n"
        "details: AssertionError: order creation failed"
    )


def test_index_text_redacts_all_text_fields_without_changing_source() -> None:
    detail = make_detail(
        suite_name="token=suite-secret",
        classname="password=class-secret",
        test_name="secret=test-secret",
        message="api_key=message-secret",
        details="Authorization: Bearer details-secret",
    )
    original = detail.model_dump()

    text = build_failure_index_text(detail)

    assert text.count("[REDACTED]") == 5
    for value in (
        "suite-secret",
        "class-secret",
        "test-secret",
        "message-secret",
        "details-secret",
    ):
        assert value not in text
    assert detail.model_dump() == original


def test_index_text_omits_missing_attributes_and_ignores_row_identity() -> None:
    detail = make_detail(suite_name=None, classname=None, message=None)

    text = build_failure_index_text(detail)

    assert "suite_name:" not in text
    assert "classname:" not in text
    assert "message:" not in text
    assert text == build_failure_index_text(
        detail.model_copy(update={"id": 999, "testcase_index": 42, "issue_index": 3})
    )


def test_index_text_reuses_field_limits_for_long_multilingual_evidence() -> None:
    text = build_failure_index_text(
        make_detail(message="错" * 3000, details="栈" * 5000)
    )
    lines = text.split("\ndetails: ")

    assert len(lines) == 2
    assert len(lines[1]) == 4000
    assert lines[1].endswith("...[TRUNCATED]")
    assert text.count("[TRUNCATED]") == 2

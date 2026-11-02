"""验证 CI 失败详情查询接口的响应契约。"""

from datetime import UTC, datetime

import pytest
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
    CiFailureQueryResponse,
)
from pydantic import ValidationError


def build_valid_detail() -> dict[str, object]:
    """构造一条符合查询响应契约的失败明细。"""
    return {
        "id": 100,
        "testcase_index": 3,
        "issue_index": 0,
        "suite_name": "backend",
        "classname": "tests.test_orders",
        "test_name": "test_create_order",
        "issue_type": "failure",
        "message": "预期 200，实际 500",
        "details": "AssertionError: expected 200, got 500",
    }


def build_valid_response() -> dict[str, object]:
    """构造一份已完成解析且包含一条失败的事件响应。"""
    return {
        "event_id": 42,
        "repository_full_name": "owner/repository",
        "run_id": 1001,
        "run_attempt": 1,
        "job_key": "backend_tests",
        "workflow_name": "Backend CI",
        "job_name": "Backend tests (Python 3.12)",
        "commit_sha": "a" * 40,
        "head_branch": "main",
        "parse_status": "parsed",
        "parse_error_code": None,
        "parse_error_message": None,
        "parse_finished_at": datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
        "failures": [build_valid_detail()],
    }


def test_query_response_serializes_event_and_failure_evidence() -> None:
    """合法模型应稳定输出事件状态和完整失败证据。"""
    response = CiFailureQueryResponse.model_validate(build_valid_response())

    payload = response.model_dump(mode="json")

    assert payload["event_id"] == 42
    assert payload["parse_status"] == "parsed"
    assert payload["parse_finished_at"] == "2026-11-01T08:30:00Z"
    assert payload["failures"] == [build_valid_detail()]


@pytest.mark.parametrize("parse_status", ["pending", "parsed", "failed"])
def test_query_response_accepts_each_supported_parse_status(
    parse_status: str,
) -> None:
    """响应契约应能表达三种现有解析状态和空明细。"""
    payload = build_valid_response()
    payload["parse_status"] = parse_status
    payload["failures"] = []

    response = CiFailureQueryResponse.model_validate(payload)

    assert response.parse_status == parse_status
    assert response.failures == []


def test_detail_rejects_unknown_issue_type() -> None:
    """结构化明细不能把 skipped 或拼写错误伪装成失败。"""
    payload = build_valid_detail()
    payload["issue_type"] = "skipped"

    with pytest.raises(ValidationError):
        CiFailureDetailResponse.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("id", 0),
        ("testcase_index", -1),
        ("issue_index", -1),
    ],
)
def test_detail_rejects_invalid_database_identity_or_xml_position(
    field_name: str,
    invalid_value: int,
) -> None:
    """数据库主键必须为正数，XML 位置下标不能为负数。"""
    payload = build_valid_detail()
    payload[field_name] = invalid_value

    with pytest.raises(ValidationError):
        CiFailureDetailResponse.model_validate(payload)


def test_query_response_rejects_unknown_parse_status() -> None:
    """查询响应不能返回数据库契约之外的解析状态。"""
    payload = build_valid_response()
    payload["parse_status"] = "processing"

    with pytest.raises(ValidationError):
        CiFailureQueryResponse.model_validate(payload)


def test_query_response_rejects_undeclared_fields() -> None:
    """额外字段应暴露服务端拼写或映射错误，而不是被静默忽略。"""
    payload = build_valid_response()
    payload["unexpected"] = "should not be accepted"

    with pytest.raises(ValidationError):
        CiFailureQueryResponse.model_validate(payload)


def test_query_response_requires_failures_field() -> None:
    """即使没有失败明细，响应也必须显式返回空列表。"""
    payload = build_valid_response()
    del payload["failures"]

    with pytest.raises(ValidationError):
        CiFailureQueryResponse.model_validate(payload)

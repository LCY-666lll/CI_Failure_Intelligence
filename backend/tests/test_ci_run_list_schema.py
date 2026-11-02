"""验证最近 CI 事件列表的响应契约。"""

from datetime import UTC, datetime

import pytest
from ci_failure_intelligence.schemas.ci_run_list import (
    CiRunListItem,
    CiRunListResponse,
)
from pydantic import ValidationError


def build_item_data() -> dict[str, object]:
    """构造一条符合列表契约的 CI 事件摘要。"""

    return {
        "event_id": 13,
        "repository_full_name": "LCY-666lll/CI_Failure_Intelligence",
        "run_id": 1793602473,
        "run_attempt": 1,
        "job_key": "backend-tests-failure-detail-manual",
        "workflow_name": "manual-failure-detail-verification",
        "job_name": "backend-tests-python-3.12-failure-manual",
        "commit_sha": "a" * 40,
        "head_branch": "main",
        "received_at": datetime(2026, 11, 2, 6, 54, 50, tzinfo=UTC),
        "parse_status": "parsed",
        "failure_count": 1,
    }


def test_list_response_serializes_event_summary() -> None:
    """合法摘要应保留事件身份、状态、数量和带时区时间。"""

    response = CiRunListResponse(
        items=[CiRunListItem.model_validate(build_item_data())]
    )

    payload = response.model_dump(mode="json")

    assert payload["items"][0]["event_id"] == 13
    assert payload["items"][0]["parse_status"] == "parsed"
    assert payload["items"][0]["failure_count"] == 1
    assert payload["items"][0]["received_at"] == "2026-11-02T06:54:50Z"
    assert "junit_xml" not in payload["items"][0]
    assert "details" not in payload["items"][0]


def test_list_response_accepts_empty_items() -> None:
    """数据库没有事件时应形成合法空列表。"""

    response = CiRunListResponse(items=[])

    assert response.items == []


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("event_id", 0),
        ("event_id", "13"),
        ("parse_status", "unknown"),
        ("failure_count", -1),
    ],
)
def test_list_item_rejects_invalid_contract_values(
    field_name: str,
    invalid_value: object,
) -> None:
    """事件摘要不能接受非法身份、状态、数量或隐式类型转换。"""

    item_data = build_item_data()
    item_data[field_name] = invalid_value

    with pytest.raises(ValidationError):
        CiRunListItem.model_validate(item_data)


def test_list_item_rejects_undeclared_fields() -> None:
    """列表接口不能意外暴露模型未声明的数据库字段。"""

    item_data = build_item_data()
    item_data["junit_xml"] = "<testsuite />"

    with pytest.raises(ValidationError):
        CiRunListItem.model_validate(item_data)

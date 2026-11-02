"""验证 CI 失败详情查询服务的读取、排序和异常边界。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Self, cast

import pytest
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
    get_ci_failure_details,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


class FakeMappingResult:
    """提供查询服务使用的最小 mappings 结果接口。"""

    def __init__(
        self,
        *,
        one_row: dict[str, object] | None = None,
        all_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.one_row = one_row
        self.all_rows = all_rows or []

    def mappings(self) -> Self:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.one_row

    def all(self) -> list[dict[str, object]]:
        return self.all_rows


class FakeSession:
    """按调用顺序返回查询结果，并记录实际生成的 SQL 语句。"""

    def __init__(self, results: list[FakeMappingResult | SQLAlchemyError]) -> None:
        self.results = results
        self.executions: list[object] = []

    async def execute(
        self,
        statement: object,
        parameters: Any = None,
    ) -> FakeMappingResult:
        self.executions.append(statement)
        result = self.results.pop(0)

        if isinstance(result, SQLAlchemyError):
            raise result

        return result


def build_event_row(
    *,
    parse_status: str = "parsed",
    parse_finished_at: datetime | None = None,
) -> dict[str, object]:
    """构造查询事件语句应返回的字段映射。"""
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
        "parse_status": parse_status,
        "parse_error_code": None,
        "parse_error_message": None,
        "parse_finished_at": parse_finished_at,
    }


def build_detail_row(
    *,
    detail_id: int,
    testcase_index: int,
    issue_index: int,
    issue_type: str,
) -> dict[str, object]:
    """构造查询明细语句应返回的一条失败证据。"""
    return {
        "id": detail_id,
        "testcase_index": testcase_index,
        "issue_index": issue_index,
        "suite_name": "backend",
        "classname": "tests.test_orders",
        "test_name": f"test_order_{testcase_index}",
        "issue_type": issue_type,
        "message": "test-only failure message",
        "details": "test-only traceback",
    }


def test_query_returns_event_and_ordered_failure_evidence() -> None:
    """服务应组合事件与明细，并在 SQL 中固定 XML 位置顺序。"""
    finished_at = datetime(2026, 11, 2, 8, 30, tzinfo=UTC)
    session = FakeSession(
        [
            FakeMappingResult(one_row=build_event_row(parse_finished_at=finished_at)),
            FakeMappingResult(
                all_rows=[
                    build_detail_row(
                        detail_id=100,
                        testcase_index=3,
                        issue_index=0,
                        issue_type="failure",
                    ),
                    build_detail_row(
                        detail_id=101,
                        testcase_index=7,
                        issue_index=0,
                        issue_type="error",
                    ),
                ]
            ),
        ]
    )

    result = asyncio.run(
        get_ci_failure_details(cast(AsyncSession, session), event_id=42)
    )

    assert result.event_id == 42
    assert result.parse_status == "parsed"
    assert result.parse_finished_at == finished_at
    assert [detail.id for detail in result.failures] == [100, 101]
    assert [detail.issue_type for detail in result.failures] == ["failure", "error"]
    assert len(session.executions) == 2

    event_query = str(session.executions[0])
    assert "ci_failure_events.id =" in event_query
    assert "junit_xml" not in event_query
    assert "payload_sha256" not in event_query

    detail_query = str(session.executions[1])
    assert "ci_failure_details.event_id =" in detail_query
    assert (
        "ORDER BY ci_failure_details.testcase_index, "
        "ci_failure_details.issue_index" in detail_query
    )


def test_query_returns_existing_pending_event_with_empty_failures() -> None:
    """事件存在但尚未解析时，应返回 pending 和显式空列表。"""
    session = FakeSession(
        [
            FakeMappingResult(one_row=build_event_row(parse_status="pending")),
            FakeMappingResult(all_rows=[]),
        ]
    )

    result = asyncio.run(
        get_ci_failure_details(cast(AsyncSession, session), event_id=42)
    )

    assert result.parse_status == "pending"
    assert result.parse_finished_at is None
    assert result.failures == []
    assert len(session.executions) == 2


def test_query_raises_not_found_without_reading_details() -> None:
    """事件不存在时应停止查询，不能把它伪装成合法空明细。"""
    session = FakeSession([FakeMappingResult(one_row=None)])

    with pytest.raises(CiFailureQueryNotFoundError) as error_info:
        asyncio.run(get_ci_failure_details(cast(AsyncSession, session), event_id=404))

    assert error_info.value.event_id == 404
    assert len(session.executions) == 1


@pytest.mark.parametrize("failure_query_index", [0, 1])
def test_query_translates_database_errors(
    failure_query_index: int,
) -> None:
    """事件或明细查询失败都应转换成稳定的查询不可用异常。"""
    results: list[FakeMappingResult | SQLAlchemyError] = [
        FakeMappingResult(one_row=build_event_row()),
        FakeMappingResult(all_rows=[]),
    ]
    results[failure_query_index] = SQLAlchemyError("test-only database failure")
    session = FakeSession(results)

    with pytest.raises(CiFailureQueryUnavailableError) as error_info:
        asyncio.run(get_ci_failure_details(cast(AsyncSession, session), event_id=42))

    assert "event_id=42" in str(error_info.value)
    assert isinstance(error_info.value.__cause__, SQLAlchemyError)

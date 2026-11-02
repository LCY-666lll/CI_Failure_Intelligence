"""验证最近 CI 事件列表服务的查询、边界和异常翻译。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Self, cast

import pytest
from ci_failure_intelligence.service.ci_run_list import (
    CiRunListUnavailableError,
    list_recent_ci_runs,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ClauseElement


class FakeMappingResult:
    """提供列表服务使用的最小 mappings 结果接口。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> Self:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeSession:
    """返回固定查询结果或数据库异常，并记录生成的 SQL。"""

    def __init__(
        self,
        result: FakeMappingResult | SQLAlchemyError,
    ) -> None:
        self.result = result
        self.executions: list[object] = []

    async def execute(
        self,
        statement: object,
        parameters: Any = None,
    ) -> FakeMappingResult:
        self.executions.append(statement)

        if isinstance(self.result, SQLAlchemyError):
            raise self.result

        return self.result


def build_event_row(
    *,
    event_id: int,
    failure_count: int,
) -> dict[str, object]:
    """构造列表查询应返回的一条事件字段映射。"""

    return {
        "event_id": event_id,
        "repository_full_name": "owner/repository",
        "run_id": 1000 + event_id,
        "run_attempt": 1,
        "job_key": f"backend-tests-{event_id}",
        "workflow_name": "Backend CI",
        "job_name": "Backend tests (Python 3.12)",
        "commit_sha": "a" * 40,
        "head_branch": "main",
        "received_at": datetime(2026, 11, 2, 8, event_id, tzinfo=UTC),
        "parse_status": "parsed",
        "failure_count": failure_count,
    }


def compile_literal_sql(statement: object) -> str:
    """把 SQLAlchemy 语句转换为包含实际 LIMIT 的测试 SQL 文本。"""

    sql_statement = cast(ClauseElement, statement)
    return str(
        sql_statement.compile(
            compile_kwargs={"literal_binds": True},
        )
    )


def test_list_service_returns_summaries_with_one_bounded_query() -> None:
    """服务应通过一条有界 SQL 返回事件摘要和失败数量。"""

    session = FakeSession(
        FakeMappingResult(
            [
                build_event_row(event_id=13, failure_count=1),
                build_event_row(event_id=6, failure_count=0),
            ]
        )
    )

    result = asyncio.run(
        list_recent_ci_runs(
            cast(AsyncSession, session),
            limit=5,
        )
    )

    assert [item.event_id for item in result.items] == [13, 6]
    assert [item.failure_count for item in result.items] == [1, 0]
    assert len(session.executions) == 1

    query_text = compile_literal_sql(session.executions[0])
    assert "count(ci_failure_details.id)" in query_text
    assert "ci_failure_details.event_id = ci_failure_events.id" in query_text
    assert (
        "ORDER BY ci_failure_events.received_at DESC, "
        "ci_failure_events.id DESC" in query_text
    )
    assert "LIMIT 5" in query_text
    assert "junit_xml" not in query_text
    assert "payload_sha256" not in query_text


def test_list_service_returns_empty_response() -> None:
    """数据库没有事件时应返回合法空列表。"""

    session = FakeSession(FakeMappingResult([]))

    result = asyncio.run(
        list_recent_ci_runs(
            cast(AsyncSession, session),
        )
    )

    assert result.items == []
    assert "LIMIT 20" in compile_literal_sql(session.executions[0])


@pytest.mark.parametrize("invalid_limit", [0, 101])
def test_list_service_rejects_limit_outside_safety_boundary(
    invalid_limit: int,
) -> None:
    """非法 limit 必须在执行数据库查询前被拒绝。"""

    session = FakeSession(FakeMappingResult([]))

    with pytest.raises(ValueError):
        asyncio.run(
            list_recent_ci_runs(
                cast(AsyncSession, session),
                limit=invalid_limit,
            )
        )

    assert session.executions == []


def test_list_service_translates_database_error() -> None:
    """SQLAlchemy 查询异常应转换成稳定的列表不可用异常。"""

    database_error = SQLAlchemyError("test-only database failure")
    session = FakeSession(database_error)

    with pytest.raises(CiRunListUnavailableError) as error_info:
        asyncio.run(
            list_recent_ci_runs(
                cast(AsyncSession, session),
            )
        )

    assert isinstance(error_info.value.__cause__, SQLAlchemyError)
    assert len(session.executions) == 1

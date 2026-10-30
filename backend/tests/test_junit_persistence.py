"""验证 JUnit 解析结果使用行锁和单事务整体替换。"""

import asyncio
from types import TracebackType
from typing import Any, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.selectable import Select

from ci_failure_intelligence.service.junit_persistence import (
    CiFailureEventNotFoundError,
    JunitPersistenceStatus,
    JunitPersistenceUnavailableError,
    process_junit_event,
)


class FakeTransaction:
    """记录两段事务是否正常退出或携带异常回滚。"""

    def __init__(self) -> None:
        self.exit_error_type: type[BaseException] | None = None

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_error_type = error_type


class FakeResult:
    """提供服务读取单个标量时需要的最小 Result 接口。"""

    def __init__(self, scalar: str | int | None = None) -> None:
        self.scalar = scalar

    def scalar_one_or_none(self) -> str | int | None:
        return self.scalar


class FakeSession:
    """顺序返回数据库结果，并记录事务、语句和批量参数。"""

    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.transactions: list[FakeTransaction] = []
        self.executions: list[tuple[object, Any]] = []

    def begin(self) -> FakeTransaction:
        transaction = FakeTransaction()
        self.transactions.append(transaction)
        return transaction

    async def execute(
        self,
        statement: object,
        parameters: Any = None,
    ) -> FakeResult:
        self.executions.append((statement, parameters))
        if self.results:
            return self.results.pop(0)
        return FakeResult()


class UnavailableSession:
    """模拟读取原始事件时数据库不可用。"""

    def begin(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(
        self,
        statement: object,
        parameters: Any = None,
    ) -> FakeResult:
        raise SQLAlchemyError("test-only database failure")


def test_valid_xml_replaces_details_and_marks_event_parsed() -> None:
    """合法 XML 应锁行、整体替换两条明细并标记 parsed。"""
    xml = """<testsuite name="backend"><testcase name="failed">
    <failure message="assertion">first</failure>
    <error message="teardown">second</error>
    </testcase></testsuite>"""
    session = FakeSession(
        [
            FakeResult(xml),
            FakeResult(42),
        ]
    )

    result = asyncio.run(
        process_junit_event(cast(AsyncSession, session), event_id=42)
    )

    assert result.event_id == 42
    assert result.status is JunitPersistenceStatus.PARSED
    assert result.detail_count == 2
    assert len(session.transactions) == 2
    assert all(transaction.exit_error_type is None for transaction in session.transactions)

    statement_types = [type(statement) for statement, _ in session.executions]
    assert statement_types == [Select, Select, Delete, Insert, Update]

    lock_statement = cast(Select[Any], session.executions[1][0])
    assert lock_statement._for_update_arg is not None

    inserted_rows = session.executions[3][1]
    assert isinstance(inserted_rows, list)
    assert [row["issue_type"] for row in inserted_rows] == ["failure", "error"]
    assert all(row["event_id"] == 42 for row in inserted_rows)


def test_valid_xml_without_issues_marks_parsed_without_insert() -> None:
    """合法零错误报告应为 parsed，但不执行明细 INSERT。"""
    session = FakeSession(
        [
            FakeResult("<testsuite><testcase name='passed'/></testsuite>"),
            FakeResult(42),
        ]
    )

    result = asyncio.run(
        process_junit_event(cast(AsyncSession, session), event_id=42)
    )

    assert result.status is JunitPersistenceStatus.PARSED
    assert result.detail_count == 0
    assert [type(statement) for statement, _ in session.executions] == [
        Select,
        Select,
        Delete,
        Update,
    ]


def test_invalid_xml_removes_old_details_and_marks_event_failed() -> None:
    """解析失败不保存部分明细，并用稳定错误代码更新事件。"""
    session = FakeSession(
        [
            FakeResult("<testsuite>"),
            FakeResult(42),
        ]
    )

    result = asyncio.run(
        process_junit_event(cast(AsyncSession, session), event_id=42)
    )

    assert result.status is JunitPersistenceStatus.FAILED
    assert result.detail_count == 0
    assert [type(statement) for statement, _ in session.executions] == [
        Select,
        Select,
        Delete,
        Update,
    ]

    update_statement = cast(Update, session.executions[3][0])
    update_parameters = update_statement.compile().params
    assert update_parameters["parse_status"] == "failed"
    assert update_parameters["parse_error_code"] == "invalid_xml"


def test_missing_event_stops_before_parsing_and_writing() -> None:
    """不存在的事件 ID 应产生稳定业务异常。"""
    session = FakeSession([FakeResult(None)])

    with pytest.raises(CiFailureEventNotFoundError):
        asyncio.run(
            process_junit_event(cast(AsyncSession, session), event_id=404)
        )

    assert len(session.transactions) == 1
    assert len(session.executions) == 1


def test_database_error_is_translated_to_unavailable() -> None:
    """SQLAlchemy 异常应转换成稳定服务异常并保留原始原因。"""
    with pytest.raises(JunitPersistenceUnavailableError) as captured:
        asyncio.run(
            process_junit_event(
                cast(AsyncSession, UnavailableSession()),
                event_id=42,
            )
        )

    assert isinstance(captured.value.__cause__, SQLAlchemyError)

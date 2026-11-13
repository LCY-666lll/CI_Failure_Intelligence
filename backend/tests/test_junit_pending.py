"""验证 pending JUnit 事件批处理的选择、隔离和统计契约。"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self, cast

import pytest
from ci_failure_intelligence.service import junit_pending
from ci_failure_intelligence.service.junit_pending import (
    JunitPendingBatchResult,
    process_pending_junit_batch,
)
from ci_failure_intelligence.service.junit_persistence import (
    JunitPersistenceResult,
    JunitPersistenceStatus,
    JunitPersistenceUnavailableError,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class FakeScalarResult:
    """提供查询 pending ID 时需要的最小标量结果接口。"""

    def __init__(self, event_ids: list[int]) -> None:
        self.event_ids = event_ids

    def scalars(self) -> FakeScalarResult:
        return self

    def all(self) -> list[int]:
        return self.event_ids


class FakeSession:
    """记录查询语句，并支持 async with 自动关闭语义。"""

    def __init__(
        self,
        event_ids: list[int] | None = None,
        query_error: SQLAlchemyError | None = None,
    ) -> None:
        self.event_ids = event_ids or []
        self.query_error = query_error
        self.executions: list[object] = []
        self.closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    async def execute(
        self,
        statement: object,
        parameters: Any = None,
    ) -> FakeScalarResult:
        self.executions.append(statement)

        if self.query_error is not None:
            raise self.query_error

        return FakeScalarResult(self.event_ids)


class FakeSessionFactory:
    """第一次调用返回查询 Session，后续调用返回每个事件的独立 Session。"""

    def __init__(
        self,
        event_ids: list[int],
        query_error: SQLAlchemyError | None = None,
    ) -> None:
        self.query_session = FakeSession(event_ids, query_error)
        self.created_sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        if not self.created_sessions:
            session = self.query_session
        else:
            session = FakeSession()

        self.created_sessions.append(session)
        return session


def _as_session_factory(
    factory: FakeSessionFactory,
) -> async_sessionmaker[AsyncSession]:
    """只在测试边界把最小假工厂视为 SQLAlchemy Session 工厂。"""
    return cast(async_sessionmaker[AsyncSession], factory)


def test_pending_batch_processes_ids_in_order_and_counts_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批处理应按查询顺序逐条处理，并分别统计 parsed 与 failed。"""
    factory = FakeSessionFactory([4, 8, 10])
    processed_event_ids: list[int] = []
    processing_sessions: list[AsyncSession] = []

    async def fake_process_junit_event(
        session: AsyncSession,
        event_id: int,
    ) -> JunitPersistenceResult:
        processed_event_ids.append(event_id)
        processing_sessions.append(session)
        status = (
            JunitPersistenceStatus.FAILED
            if event_id == 8
            else JunitPersistenceStatus.PARSED
        )
        return JunitPersistenceResult(
            event_id=event_id,
            status=status,
            detail_count=0,
        )

    monkeypatch.setattr(
        junit_pending,
        "process_junit_event",
        fake_process_junit_event,
    )

    result = asyncio.run(
        process_pending_junit_batch(
            _as_session_factory(factory),
            limit=100,
        )
    )

    assert result == JunitPendingBatchResult(
        selected_count=3,
        parsed_count=2,
        failed_count=1,
        parsed_event_ids=(4, 10),
    )
    assert processed_event_ids == [4, 8, 10]

    # 一次查询 Session 加三个事件 Session；事件之间不得共享 Session。
    assert len(factory.created_sessions) == 4
    assert len({id(session) for session in processing_sessions}) == 3
    assert all(session.closed for session in factory.created_sessions)

    query = factory.query_session.executions[0]
    query_text = str(query)
    query_parameters = query.compile().params  # type: ignore[attr-defined]
    assert "ci_failure_events.parse_status" in query_text
    assert "ORDER BY ci_failure_events.id" in query_text
    assert "LIMIT" in query_text
    assert "pending" in query_parameters.values()
    assert 100 in query_parameters.values()


def test_pending_batch_without_events_returns_zero_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 pending 事件时不应创建事件处理 Session。"""
    factory = FakeSessionFactory([])

    async def unexpected_process(
        session: AsyncSession,
        event_id: int,
    ) -> JunitPersistenceResult:
        pytest.fail("空批次不应调用单事件持久化服务")

    monkeypatch.setattr(junit_pending, "process_junit_event", unexpected_process)

    result = asyncio.run(process_pending_junit_batch(_as_session_factory(factory)))

    assert result == JunitPendingBatchResult(
        selected_count=0,
        parsed_count=0,
        failed_count=0,
    )
    assert len(factory.created_sessions) == 1
    assert factory.query_session.closed is True


@pytest.mark.parametrize("invalid_limit", [0, 101])
def test_pending_batch_rejects_limit_before_database_access(
    invalid_limit: int,
) -> None:
    """批次范围错误应在创建 Session 和查询数据库前失败。"""
    factory = FakeSessionFactory([1])

    with pytest.raises(ValueError, match="1 到 100"):
        asyncio.run(
            process_pending_junit_batch(
                _as_session_factory(factory),
                limit=invalid_limit,
            )
        )

    assert factory.created_sessions == []


def test_pending_query_error_is_translated_to_unavailable() -> None:
    """查询 pending 失败应转换为稳定服务异常并保留原始原因。"""
    factory = FakeSessionFactory(
        [],
        query_error=SQLAlchemyError("test-only pending query failure"),
    )

    with pytest.raises(JunitPersistenceUnavailableError) as captured:
        asyncio.run(process_pending_junit_batch(_as_session_factory(factory)))

    assert isinstance(captured.value.__cause__, SQLAlchemyError)
    assert factory.query_session.closed is True

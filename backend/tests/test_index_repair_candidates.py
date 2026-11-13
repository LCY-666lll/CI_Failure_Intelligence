"""验证补偿分页的范围、固定终点和数据库故障边界。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryUnavailableError,
)
from ci_failure_intelligence.service.ci_processing import list_index_repair_candidates
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError


def prepare(rows=(), end=17):
    session = AsyncMock()
    session.scalar.return_value = end
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    session.execute.return_value = result
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=context)
    return factory, session, context


def sql(statement):
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_first_page_uses_parsed_max_id_and_bounded_ordered_query():
    factory, session, context = prepare((6,), 17)
    page, end = asyncio.run(
        list_index_repair_candidates(session_factory=factory, limit=1)
    )
    assert (page, end) == ([6], 17)
    maximum_query = sql(session.scalar.await_args.args[0])
    page_query = sql(session.execute.await_args.args[0])
    assert "max(ci_failure_events.id)" in maximum_query
    assert "parse_status = 'parsed'" in maximum_query
    assert "parse_status = 'parsed'" in page_query
    assert "id > 0" in page_query and "id <= 17" in page_query
    assert "ORDER BY ci_failure_events.id" in page_query and "LIMIT 1" in page_query
    context.__aexit__.assert_awaited_once()
    session.commit.assert_not_awaited()


def test_next_page_keeps_original_end_instead_of_scanning_new_traffic():
    factory, session, _ = prepare((13,))
    assert asyncio.run(
        list_index_repair_candidates(
            session_factory=factory, after_event_id=6, through_event_id=17, limit=1
        )
    ) == ([13], 17)
    session.scalar.assert_not_awaited()
    query = sql(session.execute.await_args.args[0])
    assert "id > 6" in query and "id <= 17" in query


@pytest.mark.parametrize("end", [None, 0])
def test_no_parsed_events_returns_empty_page_and_zero_end(end):
    factory, _, _ = prepare(end=end)
    assert asyncio.run(list_index_repair_candidates(session_factory=factory)) == ([], 0)


def test_exhausted_page_preserves_end_for_callers_reset_decision():
    factory, session, _ = prepare()
    assert asyncio.run(
        list_index_repair_candidates(
            session_factory=factory, after_event_id=17, through_event_id=17
        )
    ) == ([], 17)
    session.scalar.assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"after_event_id": -1},
        {"after_event_id": 10, "through_event_id": 9},
    ],
)
def test_invalid_range_never_opens_database_session(kwargs):
    factory, _, _ = prepare()
    with pytest.raises(ValueError):
        asyncio.run(list_index_repair_candidates(session_factory=factory, **kwargs))
    factory.assert_not_called()


@pytest.mark.parametrize("operation", ["scalar", "execute"])
def test_database_failure_is_not_reported_as_empty_success(operation):
    factory, session, context = prepare()
    getattr(session, operation).side_effect = SQLAlchemyError("test-only-db-failure")
    with pytest.raises(CiFailureQueryUnavailableError) as caught:
        asyncio.run(list_index_repair_candidates(session_factory=factory))
    assert isinstance(caught.value.__cause__, SQLAlchemyError)
    assert "test-only-db-failure" not in str(caught.value)
    context.__aexit__.assert_awaited_once()


def test_cancel_is_propagated_and_session_is_released():
    factory, session, context = prepare()
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(list_index_repair_candidates(session_factory=factory))
    context.__aexit__.assert_awaited_once()

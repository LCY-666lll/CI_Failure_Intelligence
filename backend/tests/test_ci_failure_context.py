"""验证索引和检索共用的事件来源读取。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureQueryResponse
from ci_failure_intelligence.service import ci_failure_query as service
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.parametrize(
    "metadata",
    [
        (123, datetime(2026, 11, 10, tzinfo=UTC)),
        None,
        SQLAlchemyError("private database information"),
    ],
)
def test_context_reuses_details_and_handles_metadata_outcome(monkeypatch, metadata):
    event = cast(CiFailureQueryResponse, object())
    statements = []

    async def execute(statement):
        statements.append(statement)
        if isinstance(metadata, Exception):
            raise metadata
        return SimpleNamespace(one_or_none=lambda: metadata)

    session = cast(AsyncSession, SimpleNamespace(execute=execute))

    async def read_details(actual_session, event_id):
        assert actual_session is session and event_id == 13
        return event

    monkeypatch.setattr(service, "get_ci_failure_details", read_details)
    if metadata is None:
        with pytest.raises(service.CiFailureQueryNotFoundError):
            asyncio.run(service.get_ci_failure_context(session, 13))
    elif isinstance(metadata, Exception):
        with pytest.raises(service.CiFailureQueryUnavailableError) as error:
            asyncio.run(service.get_ci_failure_context(session, 13))
        assert error.value.__cause__ is metadata
    else:
        assert asyncio.run(service.get_ci_failure_context(session, 13)) == (
            event,
            *metadata,
        )
    assert len(statements) == 1
    assert 13 in statements[0].compile().params.values()


def test_missing_event_does_not_attempt_metadata_query(monkeypatch):
    async def missing(session, event_id):
        raise service.CiFailureQueryNotFoundError(event_id)

    monkeypatch.setattr(service, "get_ci_failure_details", missing)
    with pytest.raises(service.CiFailureQueryNotFoundError):
        asyncio.run(service.get_ci_failure_context(cast(AsyncSession, object()), 13))

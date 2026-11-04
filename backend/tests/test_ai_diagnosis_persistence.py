"""验证 AI 诊断抢占服务的稳定契约和异常边界。"""

import asyncio
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import cast

import pytest
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisCacheIdentity,
    AiDiagnosisPersistenceUnavailableError,
    _build_claim_statement,
    claim_ai_diagnosis,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


def _cache_identity() -> AiDiagnosisCacheIdentity:
    return AiDiagnosisCacheIdentity(
        prompt_version="v1",
        output_schema_version="v1",
        requested_model="deepseek-flash",
        evidence_sha256="a" * 64,
        model_config_sha256="b" * 64,
    )


def test_claim_statement_uses_atomic_postgresql_upsert() -> None:
    """抢占语句应由 PostgreSQL 在一条带条件的 UPSERT 中完成。"""

    from uuid import UUID

    statement = _build_claim_statement(
        event_id=42,
        cache_identity=_cache_identity(),
        lease_token=UUID("00000000-0000-4000-8000-000000000001"),
    )
    compiled_sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT (event_id) DO UPDATE" in compiled_sql
    assert "WHERE" in compiled_sql
    assert "RETURNING" in compiled_sql
    assert "lease_expires_at" in compiled_sql
    assert "attempt_count" in compiled_sql


class _FailingTransaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


class _FailingSession:
    def begin(self) -> _FailingTransaction:
        return _FailingTransaction()

    async def execute(self, _: object) -> None:
        raise SQLAlchemyError("test-only database failure")


def test_claim_translates_database_error_without_exposing_sqlalchemy() -> None:
    """调用层只应依赖项目异常，同时保留底层异常链。"""

    with pytest.raises(AiDiagnosisPersistenceUnavailableError) as error_info:
        asyncio.run(
            claim_ai_diagnosis(
                cast(AsyncSession, _FailingSession()),
                event_id=42,
                cache_identity=_cache_identity(),
            )
        )

    assert isinstance(error_info.value.__cause__, SQLAlchemyError)
    assert "test-only database failure" not in str(error_info.value)

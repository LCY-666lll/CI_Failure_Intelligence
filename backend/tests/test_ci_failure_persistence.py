"""验证 CI 失败事件持久化服务的幂等与冲突语义。"""

import asyncio
from types import TracebackType
from typing import cast

import pytest
from ci_failure_intelligence.schemas.ci_ingest import CiFailureIngestRequest
from ci_failure_intelligence.service.ci_failure_payload import (
    calculate_ci_failure_payload_sha256,
)
from ci_failure_intelligence.service.ci_failure_persistence import (
    CiFailurePayloadConflictError,
    CiFailurePersistenceStatus,
    CiFailurePersistenceUnavailableError,
    persist_ci_failure_event,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


def build_ingest_request() -> CiFailureIngestRequest:
    """构造固定的合法 CI 失败请求。"""

    return CiFailureIngestRequest.model_validate(
        {
            "repository": {
                "github_repository_id": 123456,
                "full_name": "LCY-666lll/CI_Failure_Intelligence",
            },
            "run": {
                "run_id": 987654,
                "run_attempt": 1,
                "workflow_name": "CI",
                "job_key": "test:0",
                "job_name": "test-python-3.12",
                "commit_sha": "a" * 40,
                "head_branch": "main",
            },
            "junit_xml": "<testsuite></testsuite>",
        }
    )


class FakeTransaction:
    """记录服务是否通过异步事务上下文完成退出。"""

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
    """提供持久化服务本批会读取的最小 SQLAlchemy Result 接口。"""

    def __init__(
        self,
        *,
        scalar: int | None = None,
        row: tuple[int, str] | None = None,
    ) -> None:
        self.scalar = scalar
        self.row = row

    def scalar_one_or_none(self) -> int | None:
        return self.scalar

    def one(self) -> tuple[int, str]:
        if self.row is None:
            raise AssertionError("测试未配置查询结果")
        return self.row


class FakeSession:
    """按顺序返回预设数据库结果，并记录执行的语句。"""

    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.statements: list[object] = []
        self.transaction = FakeTransaction()

    def begin(self) -> FakeTransaction:
        return self.transaction

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.pop(0)


class UnavailableSession:
    """模拟执行首条数据库语句时基础设施不可用。"""

    def __init__(self) -> None:
        self.transaction = FakeTransaction()

    def begin(self) -> FakeTransaction:
        return self.transaction

    async def execute(self, statement: object) -> FakeResult:
        raise SQLAlchemyError("test-only database failure")


def test_first_business_event_is_created() -> None:
    """首次业务身份应插入新行并返回数据库事件 ID。"""

    fake_session = FakeSession([FakeResult(scalar=42)])

    result = asyncio.run(
        persist_ci_failure_event(
            cast(AsyncSession, fake_session),
            build_ingest_request(),
        )
    )

    assert result.status is CiFailurePersistenceStatus.CREATED
    assert result.event_id == 42
    assert len(fake_session.statements) == 1
    assert fake_session.transaction.exit_error_type is None


def test_same_business_event_and_payload_reuses_existing_row() -> None:
    """相同业务身份和摘要应返回原记录，不产生第二行。"""

    ingest_request = build_ingest_request()
    payload_sha256 = calculate_ci_failure_payload_sha256(ingest_request)
    existing_row = (42, payload_sha256)
    fake_session = FakeSession(
        [
            FakeResult(scalar=None),
            FakeResult(row=existing_row),
        ]
    )

    result = asyncio.run(
        persist_ci_failure_event(
            cast(AsyncSession, fake_session),
            ingest_request,
        )
    )

    assert result.status is CiFailurePersistenceStatus.EXISTING
    assert result.event_id == 42
    assert len(fake_session.statements) == 2
    assert fake_session.transaction.exit_error_type is None


def test_same_business_identity_with_changed_payload_is_conflict() -> None:
    """相同业务身份携带不同内容时应终止事务并报告冲突。"""

    fake_session = FakeSession(
        [
            FakeResult(scalar=None),
            FakeResult(row=(42, "f" * 64)),
        ]
    )

    with pytest.raises(CiFailurePayloadConflictError):
        asyncio.run(
            persist_ci_failure_event(
                cast(AsyncSession, fake_session),
                build_ingest_request(),
            )
        )

    assert len(fake_session.statements) == 2
    assert fake_session.transaction.exit_error_type is CiFailurePayloadConflictError


def test_database_error_is_translated_to_persistence_unavailable() -> None:
    """数据库异常应成为稳定的服务层异常，并保留原始原因。"""

    unavailable_session = UnavailableSession()

    with pytest.raises(CiFailurePersistenceUnavailableError) as captured_error:
        asyncio.run(
            persist_ci_failure_event(
                cast(AsyncSession, unavailable_session),
                build_ingest_request(),
            )
        )

    assert isinstance(captured_error.value.__cause__, SQLAlchemyError)
    assert unavailable_session.transaction.exit_error_type is SQLAlchemyError

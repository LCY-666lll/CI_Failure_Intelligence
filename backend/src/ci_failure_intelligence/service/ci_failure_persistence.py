"""持久化 CI 失败事件，并处理业务级重复与内容冲突。

身份没见过 → 首次事件

身份见过，内容也一样 → 网络重试

身份见过，内容却不同 → 业务冲突

"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.schemas.ci_ingest import CiFailureIngestRequest
from ci_failure_intelligence.service.ci_failure_payload import (
    calculate_ci_failure_payload_sha256,
)


class CiFailurePersistenceStatus(StrEnum):
    """描述一次持久化操作对数据库产生的结果。"""

    CREATED = "created"
    EXISTING = "existing"


@dataclass(frozen=True, slots=True)
class CiFailurePersistenceResult:
    """返回已经可靠保存或确认存在的 CI 失败事件。"""

    event_id: int
    status: CiFailurePersistenceStatus


class CiFailurePayloadConflictError(RuntimeError):
    """表示同一 CI 业务身份已经保存了不同的业务内容。"""

    def __init__(self, event_id: int) -> None:
        """记录发生内容冲突的现有事件。

        Args:
            event_id: 数据库中已经存在的 CI 失败事件主键。
        """
        self.event_id = event_id
        # 调用父类构造方法，设置异常消息
        super().__init__("同一 CI 业务身份已经保存了不同内容")


class CiFailurePersistenceUnavailableError(RuntimeError):
    """表示数据库无法可靠完成 CI 失败事件持久化。"""


async def _persist_ci_failure_event_in_transaction(
    session: AsyncSession, ingest_request: CiFailureIngestRequest
) -> CiFailurePersistenceResult:
    """在一个事务中保存或复用 CI 失败事件。

    Args:
        session: 当前业务操作独享的异步数据库 Session。
        ingest_request: 已通过 Pydantic 严格校验的 CI 失败请求。

    Returns:
        CiFailurePersistenceResult: 新建或复用的数据库事件 ID，
        以及本次操作对应的状态。

    Raises:
        CiFailurePayloadConflictError: 相同业务身份已经存在，
            但已有记录的业务内容摘要与本次请求不同。
    """

    payload_sha256 = calculate_ci_failure_payload_sha256(ingest_request)

    # 构造 PostgreSQL INSERT:直接尝试插入，由数据库唯一约束处理并发重复。
    insert_statement = (
        insert(CiFailureEventModel)
        .values(
            github_repository_id=(ingest_request.repository.github_repository_id),
            repository_full_name=ingest_request.repository.full_name,
            run_id=ingest_request.run.run_id,
            run_attempt=ingest_request.run.run_attempt,
            job_key=ingest_request.run.job_key,
            workflow_name=ingest_request.run.workflow_name,
            job_name=ingest_request.run.job_name,
            commit_sha=ingest_request.run.commit_sha,
            head_branch=ingest_request.run.head_branch,
            junit_xml=ingest_request.junit_xml,
            payload_sha256=payload_sha256,
        )
        # 如果四字段业务身份已经存在 → 不覆盖 → 不新增 → 不抛普通唯一约束异常
        .on_conflict_do_nothing(
            constraint="uq_ci_failure_events_business_identity",
        )
        # 插入成功时，让 PostgreSQL立即返回新行 ID
        .returning(CiFailureEventModel.id)
    )

    # 没有异常离开该代码块时提交事务；发生异常时自动回滚。
    async with session.begin():
        insert_result = await session.execute(insert_statement)
        # 取结果中第一列的值，要求最多一行，没有行就返回 None。
        inserted_event_id = insert_result.scalar_one_or_none()

        if inserted_event_id is not None:
            return CiFailurePersistenceResult(
                event_id=inserted_event_id,
                status=CiFailurePersistenceStatus.CREATED,
            )

        # 没有插入说明业务唯一约束已命中，读取原事件进行摘要核对。
        existing_statement = select(
            CiFailureEventModel.id,
            CiFailureEventModel.payload_sha256,
        ).where(
            CiFailureEventModel.github_repository_id
            == ingest_request.repository.github_repository_id,
            CiFailureEventModel.run_id == ingest_request.run.run_id,
            CiFailureEventModel.run_attempt == ingest_request.run.run_attempt,
            CiFailureEventModel.job_key == ingest_request.run.job_key,
        )

        existing_result = await session.execute(existing_statement)
        # one() 表示必须正好查到一行，否则报错
        existing_event_id, existing_payload_sha256 = existing_result.one()
        # 如果旧摘要和新摘要不同，抛出异常
        if existing_payload_sha256 != payload_sha256:
            raise CiFailurePayloadConflictError(existing_event_id)

        return CiFailurePersistenceResult(
            event_id=existing_event_id,
            status=CiFailurePersistenceStatus.EXISTING,
        )


async def persist_ci_failure_event(
    session: AsyncSession,
    ingest_request: CiFailureIngestRequest,
) -> CiFailurePersistenceResult:
    """保存或复用 CI 失败事件，并统一翻译数据库异常。

    Args:
        session: 当前业务操作独享的异步数据库 Session。
        ingest_request: 已通过 Pydantic 严格校验的 CI 失败请求。

    Returns:
        CiFailurePersistenceResult: 新建或复用的数据库事件 ID，
            以及本次持久化状态。

    Raises:
        CiFailurePayloadConflictError: 相同业务身份已经保存了不同内容。
        CiFailurePersistenceUnavailableError: SQLAlchemy 无法可靠完成
            数据库操作或事务提交。
    """

    try:
        return await _persist_ci_failure_event_in_transaction(
            session,
            ingest_request,
        )
    except SQLAlchemyError as error:
        # 对上层隐藏数据库框架细节，同时通过异常链保留原始原因。
        raise CiFailurePersistenceUnavailableError(
            "CI 失败事件存储暂时不可用"
        ) from error

"""查找并处理有限数量的 pending JUnit 事件。"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.service.junit_persistence import (
    JunitPersistenceStatus,
    JunitPersistenceUnavailableError,
    process_junit_event,
)

# 单次命令默认最多处理 100 条，防止一次运行时间无界增长。
DEFAULT_PENDING_BATCH_LIMIT = 100

# 第一版不允许调用方突破单次批处理上限。
MAX_PENDING_BATCH_LIMIT = 100


@dataclass(frozen=True, slots=True)
class JunitPendingBatchResult:
    """保存一次 pending 批处理已经提交的统计和成功事件。"""

    selected_count: int
    parsed_count: int
    failed_count: int

    # 仅包含本批已成功提交解析结果的事件，供后续索引使用。
    parsed_event_ids: tuple[int, ...] = ()


async def _list_pending_event_ids(
    session_factory: async_sessionmaker[AsyncSession], limit: int
) -> list[int]:
    """按数据库主键顺序查询一批 pending 事件。

    Args:
        session_factory: 已绑定项目 PostgreSQL Engine 的异步 Session 工厂。
        limit: 本次允许选取的最大事件数量，已经通过 1 到 100 校验。

    Returns:
        list[int]: 按事件主键升序排列的 pending 事件 ID。

    Raises:
        JunitPersistenceUnavailableError: PostgreSQL 无法完成查询。
    """

    try:
        # 查询完成后立即关闭这个 Session。
        # 后续 XML 解析不会长期占用查询 Session。
        async with session_factory() as session:
            result = await session.execute(
                select(CiFailureEventModel.id)
                .where(CiFailureEventModel.parse_status == "pending")
                .order_by(CiFailureEventModel.id)
                .limit(limit)
            )

            return list(result.scalars().all())

    except SQLAlchemyError as error:
        # 调用层只依赖项目稳定异常，不依赖 SQLAlchemy 具体异常类型。
        raise JunitPersistenceUnavailableError(
            "暂时无法查询待解析的 JUnit 事件"
        ) from error


async def process_pending_junit_batch(
    session_factory: async_sessionmaker[AsyncSession],
    limit: int = DEFAULT_PENDING_BATCH_LIMIT,
) -> JunitPendingBatchResult:
    """处理一批 pending JUnit 事件并统计最终状态。

    每个事件使用独立 Session。后续事件失败时，前面已经提交的事件
    不会被同一个大事务回滚。

    Args:
        session_factory: 已绑定项目 PostgreSQL Engine 的异步 Session 工厂。
        limit: 本次最多处理的事件数量，允许范围为 1 到 100。

    Returns:
        JunitPendingBatchResult: 选中数量以及 parsed、failed 数量。

    Raises:
        ValueError: limit 不在 1 到 100 范围内。
        CiFailureEventNotFoundError: 事件在查询后被其他操作删除。
        JunitPersistenceUnavailableError: PostgreSQL 无法查询或提交结果。
    """
    if not 1 <= limit <= MAX_PENDING_BATCH_LIMIT:
        raise ValueError(
            f"pending 批处理数量必须位于 1 到 {MAX_PENDING_BATCH_LIMIT} 之间"
        )

    event_ids = await _list_pending_event_ids(
        session_factory,
        limit,
    )

    parsed_count = 0
    failed_count = 0
    parsed_event_ids: list[int] = []

    for event_id in event_ids:
        # 每个事件调用一次工厂，获得相互独立的 Session。
        async with session_factory() as session:
            result = await process_junit_event(
                session,
                event_id,
            )
        if result.status is JunitPersistenceStatus.PARSED:
            parsed_count += 1
            # process_junit_event 已完成提交，才把事件交给后续索引。
            parsed_event_ids.append(event_id)
        else:
            failed_count += 1
    return JunitPendingBatchResult(
        selected_count=len(event_ids),
        parsed_count=parsed_count,
        failed_count=failed_count,
        parsed_event_ids=tuple(parsed_event_ids),
    )

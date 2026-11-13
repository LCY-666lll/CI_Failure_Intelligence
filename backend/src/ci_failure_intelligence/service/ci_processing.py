"""串联已保存 CI 报告的解析与单事件索引。"""

import asyncio
import logging
from dataclasses import dataclass

from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ApiException
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
    get_ci_failure_context,
)
from ci_failure_intelligence.service.failure_vector_index import index_ci_failure_event
from ci_failure_intelligence.service.junit_pending import (
    DEFAULT_PENDING_BATCH_LIMIT,
    MAX_PENDING_BATCH_LIMIT,
    JunitPendingBatchResult,
    process_pending_junit_batch,
)
from ci_failure_intelligence.service.junit_persistence import (
    CiFailureEventNotFoundError,
    JunitPersistenceUnavailableError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CiProcessingBatchResult:
    """保存本批解析结果，以及后续索引的完成情况。"""

    parsing: JunitPendingBatchResult
    # 哪些事件已经成功完成 Qdrant 索引同步
    synced_event_ids: tuple[int, ...]
    # 哪些事件解析成功了，但后续索引没有完成
    index_failed_event_ids: tuple[int, ...]
    # 仅统计成功事件确认写入的 Point；失败事件可能已经部分写入。
    written_point_count: int


async def list_index_repair_candidates(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    after_event_id: int = 0,
    through_event_id: int | None = None,
    limit: int = DEFAULT_PENDING_BATCH_LIMIT,
) -> tuple[list[int], int]:
    """分页读取已解析事件，供调用方检查并补齐派生索引。

    Args:
        session_factory: 调用方持有的项目数据库 Session 工厂。
        after_event_id: 上一页最后处理的 ID；新一轮从 0 开始。
        through_event_id: 首次为空，之后传回上一页返回的固定终点。
        limit: 本页最多读取 1 到 100 个 ID。

    Returns:
        tuple[list[int], int]: 升序候选 ID 和本轮终点。空页表示本轮结束；
            调用方下一轮重置起点和终点。候选不代表索引一定缺失，
            已有差异索引函数会跳过未变化内容。

    Raises:
        ValueError: 分页范围或批次数量不合法。
        CiFailureQueryUnavailableError: PostgreSQL 无法完成候选查询。
    """
    if not 1 <= limit <= MAX_PENDING_BATCH_LIMIT:
        raise ValueError("索引补偿批次数量必须位于 1 到 100 之间")
    if after_event_id < 0 or (
        through_event_id is not None and through_event_id < after_event_id
    ):
        raise ValueError("索引补偿分页范围不合法")

    try:
        async with session_factory() as session:
            # 首次固定扫描终点，新事件留给下一轮，避免本轮一直扫不完。
            if through_event_id is None:
                through_event_id = (
                    await session.scalar(
                        # 查询本轮最大事件 ID
                        select(func.max(CiFailureEventModel.id)).where(
                            CiFailureEventModel.parse_status == "parsed"
                        )
                    )
                    or 0
                )

            # 分页查询事件 ID
            result = await session.execute(
                select(CiFailureEventModel.id)
                .where(
                    CiFailureEventModel.parse_status == "parsed",
                    CiFailureEventModel.id > after_event_id,
                    CiFailureEventModel.id <= through_event_id,
                )
                # 按 ID 从小到大
                .order_by(CiFailureEventModel.id)
                .limit(limit)
            )
            return list(result.scalars().all()), through_event_id
    except SQLAlchemyError as error:
        raise CiFailureQueryUnavailableError("暂时无法查询索引补偿候选") from error


async def process_pending_ci_batch(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_semaphore: asyncio.Semaphore,
    retrieval_settings: RetrievalSettings,
    qdrant_client: AsyncQdrantClient,
    embedding_model: TextEmbedding,
    limit: int = DEFAULT_PENDING_BATCH_LIMIT,
    repair_event_ids: tuple[int, ...] = (),
) -> CiProcessingBatchResult:
    """解析待处理报告，复用同一循环同步新事件和补偿候选的索引。

    Args:
        session_factory: 调用方持有的项目数据库 Session 工厂。
        embedding_semaphore: 与其他共享模型操作使用同一实例的许可。
        retrieval_settings: 已完成校验的检索配置。
        qdrant_client: 调用方持有并负责关闭的共享客户端。
        embedding_model: 调用方已经加载的共享模型。
        limit: 本批选取的 pending 数量，由原批处理校验为1到100。
        repair_event_ids: 调用方查询的已解析候选，最多 100 个正整数 ID。
            新解析事件优先，同一 ID 在本批只处理一次。

    Returns:
        CiProcessingBatchResult: 已提交的解析统计、索引成功和失败ID，
            以及成功同步事件确认写入的 Point 数量。
            失败事件可能已经部分写入，其数量不包含在此统计中。

    Raises:
        ValueError: 批次数量或索引输入不符合约束。
        CiFailureEventNotFoundError: 解析期间事件被删除。
        JunitPersistenceUnavailableError: 无法可靠完成解析批处理。
    """
    # 输入错误必须在解析产生数据库写入之前被拒绝。
    if len(repair_event_ids) > MAX_PENDING_BATCH_LIMIT or any(
        event_id <= 0 for event_id in repair_event_ids
    ):
        raise ValueError("索引补偿候选最多 100 个，且 ID 必须为正整数")

    # 直接复用已有批处理，不重新查询 pending 或再写解析循环。
    parsing = await process_pending_junit_batch(session_factory, limit)

    synced_event_ids: list[int] = []
    index_failed_event_ids: list[int] = []
    written_point_count = 0

    # 字典保留插入顺序，同时去重；补偿不引入第二套索引实现。
    event_ids = dict.fromkeys((*parsing.parsed_event_ids, *repair_event_ids))
    for event_id in event_ids:
        try:
            # 读取已经提交的结构化明细，索引不重新解析 XML。
            async with session_factory() as session:
                event, repository_id, received_at = await get_ci_failure_context(
                    session, event_id
                )

            # 查询 Session 已关闭，模型计算不占用这次数据库连接。
            async with embedding_semaphore:
                written = await index_ci_failure_event(
                    event=event,
                    github_repository_id=repository_id,
                    received_at=received_at,
                    client=qdrant_client,
                    embedding_model=embedding_model,
                    settings=retrieval_settings,
                )

        except (
            CiFailureQueryNotFoundError,
            CiFailureQueryUnavailableError,
            ApiException,
            OSError,
        ) as error:
            # 解析已经提交，只记录索引未完成，不重置解析状态。
            # 只记录事件ID和故障类型，避免原始异常中的凭据进入日志。
            logger.warning(
                "CI事件索引未完成：event_id=%s，error_type=%s",
                event_id,
                type(error).__name__,
            )
            index_failed_event_ids.append(event_id)
            continue

        synced_event_ids.append(event_id)
        written_point_count += written

    return CiProcessingBatchResult(
        parsing=parsing,
        synced_event_ids=tuple(synced_event_ids),
        index_failed_event_ids=tuple(index_failed_event_ids),
        written_point_count=written_point_count,
    )


async def run_ci_processing(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_semaphore: asyncio.Semaphore,
    retrieval_settings: RetrievalSettings,
    qdrant_client: AsyncQdrantClient,
    embedding_model: TextEmbedding,
    batch_size: int = 10,
    interval_seconds: float = 300.0,
) -> None:
    """串行执行解析、索引和分页补偿，直到应用取消当前任务。

    Args:
        session_factory: 应用共享的数据库 Session 工厂。
        embedding_semaphore: 与请求检索共享的推理许可。
        retrieval_settings: 已校验的检索配置。
        qdrant_client: 应用负责关闭的共享 Qdrant 客户端。
        embedding_model: 应用已经加载的共享模型。
        batch_size: 每轮新报告和补偿候选各自最多处理的数量，范围 1 到 100。
        interval_seconds: 本轮完成后、下一轮开始前等待的秒数，必须为正数。
            默认等待 300 秒；启动后先执行一轮，不先等待。

    Raises:
        ValueError: 批次数量、等待时间或下层业务输入不合法。
        asyncio.CancelledError: 应用退出，当前任务被取消。
    """
    if not 1 <= batch_size <= MAX_PENDING_BATCH_LIMIT or interval_seconds <= 0:
        raise ValueError("后台批次数量必须为 1 到 100，等待时间必须大于 0")

    # 只记录扫描进度；重启从头扫描仍由差异索引跳过未变化内容。
    after_event_id = 0
    # 本轮固定终点
    through_event_id: int | None = None

    while True:
        try:
            repair_ids, scan_end = await list_index_repair_candidates(
                session_factory=session_factory,
                after_event_id=after_event_id,
                through_event_id=through_event_id,
                limit=batch_size,
            )
            # 复用原批处理，不另写解析或索引逻辑，也不调用付费诊断。
            result = await process_pending_ci_batch(
                session_factory=session_factory,
                embedding_semaphore=embedding_semaphore,
                retrieval_settings=retrieval_settings,
                qdrant_client=qdrant_client,
                embedding_model=embedding_model,
                limit=batch_size,
                repair_event_ids=tuple(repair_ids),
            )
        except (
            CiFailureQueryUnavailableError,
            CiFailureEventNotFoundError,
            JunitPersistenceUnavailableError,
        ) as error:
            # 不推进游标，下轮重试；不记录可能包含凭据的异常原文。
            logger.warning("CI后台处理暂未完成：error_type=%s", type(error).__name__)
        # else：只有 try 正常结束、没有异常时才执行
        else:
            # 单事件索引失败已记录，下一轮完整扫描还会找到它。
            # 如果本页有数据，把最后一个事件 ID 作为下一页起点。
            after_event_id = repair_ids[-1] if repair_ids else 0
            # 有数据时保留本轮固定终点
            # 没有数据时清空终点，准备下一轮重新确定
            through_event_id = scan_end if repair_ids else None
            logger.info(
                "CI后台处理：selected=%s parsed=%s failed=%s "
                "indexed=%s index_failed=%s confirmed_points=%s",
                result.parsing.selected_count,
                result.parsing.parsed_count,
                result.parsing.failed_count,
                len(result.synced_event_ids),
                len(result.index_failed_event_ids),
                result.written_point_count,
            )

        # 本轮结束后等5分钟；异步等待不阻塞HTTP，同一任务不会重叠执行。
        await asyncio.sleep(interval_seconds)

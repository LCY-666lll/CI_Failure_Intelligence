"""将向量候选核对为真实的 PostgreSQL 历史失败。"""

import hashlib
import math
from datetime import datetime

from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import literal, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_detail import (
    CiFailureDetailModel,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
    CiFailureQueryResponse,
)
from ci_failure_intelligence.schemas.failure_history import (
    HistoricalFailureResponse,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryUnavailableError,
)
from ci_failure_intelligence.service.failure_index import (
    FAILURE_VECTOR_INDEX_VERSION,
    build_failure_index_text,
)
from ci_failure_intelligence.service.failure_vector_search import (
    search_failure_candidates,
)

MAX_HISTORY_CANDIDATES_TO_VERIFY = 20
MAX_HISTORICAL_FAILURES = 5
# 限制一次事件检索使用的当前失败数量，控制模型计算和查询次数。
MAX_CURRENT_FAILURES_FOR_HISTORY = 20


async def load_verified_history(
    *,
    candidates: list[models.ScoredPoint],
    current_event_id: int,
    github_repository_id: int,
    received_at: datetime,
    session_factory: async_sessionmaker[AsyncSession],
) -> list[HistoricalFailureResponse]:
    """回查候选来源，返回有限的有效历史失败。

    Args:
        candidates: 前一步向量查询返回的有界候选列表。
        current_event_id: 当前事件的正整数数据库 ID。
        github_repository_id: 当前事件所属的稳定仓库 ID。
        received_at: 当前事件带时区的首次接收时间。
        session_factory: 绑定项目数据库、用于创建短期Session的工厂。

    Returns:
        list[HistoricalFailureResponse]: 按分数排序且去重后的历史失败，
            最多5条。缺失、过时或不符合范围的候选被跳过。

    Raises:
        ValueError: 当前事件、仓库或时间边界无效。
        CiFailureQueryUnavailableError: 数据库无法完成历史查询。
    """

    if current_event_id <= 0 or github_repository_id <= 0 or received_at.tzinfo is None:
        raise ValueError("回查需要有效来源和带时区的接收时间")

    # tuple[int, int, int]:代表一个唯一的失败来源位置
    # key                 value
    # (13, 0, 0)          ScoredPoint A
    # (18, 3, 1)          ScoredPoint C
    best: dict[tuple[int, int, int], models.ScoredPoint] = {}

    for candidate in candidates:
        payload = candidate.payload or {}
        event_id = payload.get("event_id")
        testcase_index = payload.get("testcase_index")
        issue_index = payload.get("issue_index")

        # payload不是数据库事实；先拒绝缺失或非法的位置与版本。
        # 使用type检查，也会拒绝被当作整数使用的True、False。
        if (
            type(event_id) is not int
            or event_id <= 0
            or type(testcase_index) is not int
            or testcase_index < 0
            or type(issue_index) is not int
            or issue_index < 0
            or payload.get("index_version") != FAILURE_VECTOR_INDEX_VERSION
            # 检查数字是否是有限值
            or not math.isfinite(candidate.score)
            or candidate.score < 0
        ):
            # 当前 candidate 不符合要求，直接跳过，进入下一次循环
            continue

        key = (event_id, testcase_index, issue_index)

        if key not in best:
            # 第一次遇到这个历史错误
            best[key] = candidate
        else:
            # 以前已经保存过这个位置
            old_candidate = best[key]

            if candidate.score > old_candidate.score:
                # 新候选分数更高，就替换
                # 相同历史位置只保留一条，分数高的留下
                best[key] = candidate

    if not best:
        return []

    # 分数相同时按来源位置排序，保持结果顺序稳定。
    ranked_candidates = sorted(
        best.items(),
        key=lambda item: (-item[1].score, item[0]),
    )[:MAX_HISTORY_CANDIDATES_TO_VERIFY]

    statement = (
        select(
            CiFailureDetailModel,
            CiFailureEventModel.repository_full_name,
            CiFailureEventModel.run_id,
            CiFailureEventModel.run_attempt,
            CiFailureEventModel.job_key,
            CiFailureEventModel.received_at,
        )
        .join(
            CiFailureEventModel,
            CiFailureEventModel.id == CiFailureDetailModel.event_id,
        )
        .where(
            # 只读取候选对应的位置，不读取整份XML。
            # 三列的组合必须与候选列表中的某个组合完全匹配
            tuple_(
                CiFailureDetailModel.event_id,
                CiFailureDetailModel.testcase_index,
                CiFailureDetailModel.issue_index,
            ).in_([key for key, _ in ranked_candidates]),
            # 再次使用数据库真实字段检查仓库、状态和历史边界。
            CiFailureEventModel.github_repository_id == github_repository_id,
            CiFailureEventModel.parse_status == "parsed",
            tuple_(
                CiFailureEventModel.received_at,
                CiFailureEventModel.id,
            )
            < tuple_(
                literal(received_at),
                literal(current_event_id),
            ),
        )
    )

    try:
        async with session_factory() as session:
            rows = (await session.execute(statement)).all()
    except SQLAlchemyError as error:
        raise CiFailureQueryUnavailableError("历史失败证据暂时无法读取") from error

    # 查询已经完成；后面的转换和摘要计算不占用查询Session。
    rows_by_key = {
        (
            row[0].event_id,
            row[0].testcase_index,
            row[0].issue_index,
        ): row
        for row in rows
    }

    verified: list[HistoricalFailureResponse] = []

    for key, candidate in ranked_candidates:
        row = rows_by_key.get(key)
        if row is None:
            continue

        detail = CiFailureDetailResponse.model_validate(
            row[0],
            from_attributes=True,
        )
        text_sha256 = hashlib.sha256(
            build_failure_index_text(detail).encode("utf-8")
        ).hexdigest()

        # 数据库内容已改变时，旧向量不能冒充当前证据。
        if (candidate.payload or {}).get("content_sha256") != text_sha256:
            continue

        verified.append(
            HistoricalFailureResponse(
                event_id=key[0],
                repository_full_name=row.repository_full_name,
                run_id=row.run_id,
                run_attempt=row.run_attempt,
                job_key=row.job_key,
                received_at=row.received_at,
                similarity_score=candidate.score,
                failure=detail,
            )
        )

        if len(verified) == MAX_HISTORICAL_FAILURES:
            break

    return verified


async def retrieve_event_history(
    *,
    event: CiFailureQueryResponse,
    github_repository_id: int,
    received_at: datetime,
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncQdrantClient,
    embedding_model: TextEmbedding,
    settings: RetrievalSettings,
) -> list[HistoricalFailureResponse]:
    """查询并核对一个当前事件对应的历史失败。

    Args:
        event: 调用方已经读取并校验的当前事件及失败明细。
        github_repository_id: 从当前事件读取的稳定仓库 ID。
        received_at: 当前事件带时区的首次接收时间。
        session_factory: 用于历史回查的数据库 Session 工厂。
        client: 调用方持有并负责关闭的 Qdrant 客户端。
        embedding_model: 调用方已加载、供本次检索复用的模型。
        settings: 已校验的检索配置。

    Returns:
        list[HistoricalFailureResponse]: 去重、核对并排序后的历史，
            最多5条；正常情况下可以没有历史。

    Raises:
        ValueError: 当前事件尚未解析成功，或检索输入无效。
        CiFailureQueryUnavailableError: 历史数据库查询失败。
    """

    if event.parse_status != "parsed":
        raise ValueError("当前事件尚未成功解析")

    if not event.failures:
        return []

    # 按原 XML 位置选择，避免调用方提供不同顺序影响本次查询范围。
    ordered_details = sorted(
        event.failures,
        key=lambda detail: (
            detail.testcase_index,
            detail.issue_index,
        ),
    )
    candidates: list[models.ScoredPoint] = []

    # 串行处理，并复用传入的模型和客户端。
    for detail in ordered_details[:MAX_CURRENT_FAILURES_FOR_HISTORY]:
        hits = await search_failure_candidates(
            detail=detail,
            current_event_id=event.event_id,
            github_repository_id=github_repository_id,
            received_at=received_at,
            client=client,
            embedding_model=embedding_model,
            settings=settings,
        )
        candidates.extend(hits)

    # 统一去重、限制回查数量和核对事实，不在这里复制这些规则。
    return await load_verified_history(
        candidates=candidates,
        current_event_id=event.event_id,
        github_repository_id=github_repository_id,
        received_at=received_at,
        session_factory=session_factory,
    )

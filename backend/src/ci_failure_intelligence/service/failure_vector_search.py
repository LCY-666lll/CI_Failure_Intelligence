"""根据当前失败查询同仓库的历史向量候选。"""

import asyncio
from datetime import datetime

from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient, models

from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
)
from ci_failure_intelligence.service.failure_index import (
    FAILURE_VECTOR_INDEX_VERSION,
    build_failure_index_text,
)

# 多取少量候选，供后续数据库核对和去重。
HISTORY_CANDIDATE_LIMIT = 20

# 当前有限样本下的起始门槛，不表示根因相同的概率。
MIN_HISTORY_SIMILARITY = 0.75


async def search_failure_candidates(
    *,
    detail: CiFailureDetailResponse,
    current_event_id: int,
    github_repository_id: int,
    received_at: datetime,
    client: AsyncQdrantClient,
    embedding_model: TextEmbedding,
    settings: RetrievalSettings,
) -> list[models.ScoredPoint]:
    """查询一条当前失败对应的历史候选 Point。

    Args:
        detail: 当前已解析事件中的一条失败明细。
        current_event_id: 当前事件的正整数数据库 ID。
        github_repository_id: 当前事件所属的稳定仓库 ID。
        received_at: 当前事件带时区的首次接收时间。
        client: 调用方持有并负责关闭的异步 Qdrant 客户端。
        embedding_model: 与索引使用相同配置的已加载模型。
        settings: 已校验的检索配置，提供目标集合名称。

    Returns:
        list[models.ScoredPoint]: 按相似度排序的候选，最多 20 条。
            包含来源 payload 和分数，不返回向量。
            空列表表示本次查询没有合格候选。

    Raises:
        ValueError: 来源边界无效，或模型输出数量不正确。
    """

    if current_event_id <= 0 or github_repository_id <= 0 or received_at.tzinfo is None:
        raise ValueError("检索需要有效事件、仓库 ID 和带时区的接收时间")

    # 查询和索引使用同一套文本整理规则。
    # 把当前错误变成查询文本
    text = build_failure_index_text(detail)
    vectors = await asyncio.to_thread(
        list,
        embedding_model.embed([text], batch_size=1),
    )

    if len(vectors) != 1:
        raise ValueError("单条失败查询必须生成一个向量")

    # 历史：接收时间更早，或者同一时间但事件 ID 更小。
    history_boundary = models.Filter(
        # should:至少满足一个条件
        should=[
            models.FieldCondition(
                key="received_at",
                # 处理日期时间范围:历史时间 < 当前时间
                range=models.DatetimeRange(lt=received_at),
            ),
            models.Filter(
                # 历史时间 == 当前时间 并且 历史 event_id < 当前 event_id
                must=[
                    # FieldCondition():创建一个针对 Qdrant Payload 某个字段的查询条件
                    models.FieldCondition(
                        key="received_at",
                        range=models.DatetimeRange(
                            gte=received_at,
                            lte=received_at,
                        ),
                    ),
                    # 筛选 Payload 中 event_id 小于id的 Point。
                    models.FieldCondition(
                        key="event_id",
                        range=models.Range(lt=current_event_id),
                    ),
                ],
            ),
        ],
    )

    result = await client.query_points(
        collection_name=settings.qdrant_collection,
        query=vectors[0].tolist(),
        query_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="github_repository_id",
                    # match:精确匹配
                    match=models.MatchValue(
                        value=github_repository_id,
                    ),
                ),
                models.FieldCondition(
                    key="index_version",
                    match=models.MatchValue(
                        value=FAILURE_VECTOR_INDEX_VERSION,
                    ),
                ),
                history_boundary,
            ],
            # 显式排除当前事件，避免把自身当作历史。
            # point["event_id"] != current_event_id
            must_not=[
                models.FieldCondition(
                    key="event_id",
                    match=models.MatchValue(
                        value=current_event_id,
                    ),
                ),
            ],
        ),
        limit=HISTORY_CANDIDATE_LIMIT,
        score_threshold=MIN_HISTORY_SIMILARITY,
        # 返回 Point 的来源字段，例如事件 ID、仓库 ID、错误位置
        with_payload=True,
        # 不用返回历史 Point 的 384 维向量
        with_vectors=False,
    )

    return result.points

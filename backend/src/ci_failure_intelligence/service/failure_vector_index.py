"""将一个已解析 CI 事件的失败明细写入 Qdrant。"""

import asyncio
import hashlib
from datetime import datetime
from uuid import UUID, uuid5

from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient, models

from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureQueryResponse,
)
from ci_failure_intelligence.service.failure_index import (
    FAILURE_VECTOR_INDEX_VERSION,
    build_failure_index_text,
)

# 固定编号规则；相同事件和 XML 位置生成相同 Point ID。
FAILURE_POINT_NAMESPACE = UUID("be803724-c3df-52f2-8f99-2b118b1a1fbd")

# 控制一次向 Qdrant 写入的 Point 数量。
VECTOR_WRITE_BATCH_SIZE = 32


async def index_ci_failure_event(
    *,
    event: CiFailureQueryResponse,
    github_repository_id: int,
    received_at: datetime,
    client: AsyncQdrantClient,
    embedding_model: TextEmbedding,
    settings: RetrievalSettings,
) -> int:
    """将一个已解析事件的失败明细分批写入 Qdrant。

    Args:
        event: 已由查询服务校验的事件及失败明细。
        github_repository_id: 从该事件读取的稳定仓库 ID，必须为正数。
        received_at: 从该事件读取的首次接收时间，必须带时区。
        client: 调用方创建并负责关闭的异步 Qdrant 客户端。
        embedding_model: 调用方已加载、供本次处理复用的模型。
        settings: 已校验的检索配置，提供目标集合名称。

    Returns:
        int: 本次已确认写入的失败明细数量，包含覆盖已有 Point。

    Raises:
        ValueError: 事件未解析成功、来源信息无效，
            或生成的向量数量与输入明细数量不一致。
    """

    if event.parse_status != "parsed":
        raise ValueError("只有解析成功的事件才能生成失败向量")

    if github_repository_id <= 0 or received_at.tzinfo is None:
        raise ValueError("索引需要有效仓库 ID 和带时区的接收时间")

    if not event.failures:
        return 0

    # 首次写入时创建集合；后续事件继续使用同一集合。
    if not await client.collection_exists(settings.qdrant_collection):
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=models.VectorParams(
                # Collection 接收的每个向量必须是 384 维
                size=384,
                # 指定使用余弦相似度相关的度量方式进行向量比较
                distance=models.Distance.COSINE,
            ),
        )

    written_count = 0

    for offset in range(
        0,
        len(event.failures),
        # 每次处理最多 32 条失败明细
        VECTOR_WRITE_BATCH_SIZE,
    ):
        # 切片
        batch = event.failures[offset : offset + VECTOR_WRITE_BATCH_SIZE]
        texts = [build_failure_index_text(detail) for detail in batch]

        # embed 返回可迭代结果；由工作线程执行 list，消费结果并完成计算。
        vectors = await asyncio.to_thread(
            list,
            # 调用embedding_model的计算向量方法
            embedding_model.embed(texts, batch_size=1),
        )

        points = []

        # 数量必须一一对应，避免漏掉明细或把向量绑定到错误来源。
        # zip() 会按下标，把多个可迭代对象中的元素配成一组
        for detail, text, vector in zip(
            batch,
            texts,
            vectors,
            # 防止三个列表数量不一致
            strict=True,
        ):
            identity = f"{event.event_id}:{detail.testcase_index}:{detail.issue_index}"

            points.append(
                models.PointStruct(
                    # 使用基于业务身份的 UUID v5 保证重复写入定位相同 Point
                    id=str(uuid5(FAILURE_POINT_NAMESPACE, identity)),
                    # 将 NumPy 向量转换成客户端需要的普通数字列表。
                    vector=vector.tolist(),
                    payload={
                        "event_id": event.event_id,
                        "github_repository_id": github_repository_id,
                        "testcase_index": detail.testcase_index,
                        "issue_index": detail.issue_index,
                        # datetime 转成可放入 JSON 的带时区时间文本。
                        "received_at": received_at.isoformat(),
                        # 摘要针对本次实际生成向量的同一份文本。
                        "content_sha256": hashlib.sha256(
                            text.encode("utf-8")
                            # 把摘要转换成 64 个小写十六进制字符组成的字符串
                        ).hexdigest(),
                        "index_version": FAILURE_VECTOR_INDEX_VERSION,
                    },
                )
            )

        # 真正写入 Qdrant
        await client.upsert(
            collection_name=settings.qdrant_collection,
            points=points,
            # Qdrant 等待本次修改操作实际完成，而不只是接收请求就立即返回
            wait=True,
        )

        written_count += len(points)

    return written_count

"""将一个已解析 CI 事件的失败明细写入 Qdrant。"""

import hashlib
from datetime import datetime
from uuid import UUID, uuid5

from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient, models

from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.infrastructure.ai.embedding_model import (
    run_embedding_in_thread,
)
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

# 这里只读取小型索引元数据，不读取向量。
VECTOR_READ_BATCH_SIZE = 128


async def _load_event_index_payloads(
    *,
    event_id: int,
    client: AsyncQdrantClient,
    settings: RetrievalSettings,
) -> dict[str, dict[str, object]]:
    """读取一个事件已有的 Point 身份和元数据。

    Args:
        event_id: 已校验为正整数的当前事件 ID。
        client: 调用方持有的共享客户端，目标集合已确认存在。
        settings: 已校验的检索配置，提供集合名称。

    Returns:
        dict[str, dict[str, object]]:嵌套字典 Point ID 到元数据的映射。
            不包含向量；没有该事件的 Point 时返回空字典。

    Raises:
        ApiException: Qdrant 无法完成元数据读取。
    """
    stored: dict[str, dict[str, object]] = {}
    # 表示下一次应该从哪里继续读取
    offset = None

    # 每一页使用同一个事件过滤条件，不读取其他事件的记录。
    event_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="event_id",
                match=models.MatchValue(value=event_id),
            )
        ]
    )

    while True:
        points, offset = await client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=event_filter,
            limit=VECTOR_READ_BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            # 统一为字符串身份，与已有 UUID Point ID 保持一致。
            stored[str(point.id)] = point.payload or {}

        # 下一页游标由 Qdrant 决定，None 表示已经读完。
        if offset is None:
            break

    return stored


async def index_ci_failure_event(
    *,
    event: CiFailureQueryResponse,
    github_repository_id: int,
    received_at: datetime,
    client: AsyncQdrantClient,
    embedding_model: TextEmbedding,
    settings: RetrievalSettings,
) -> int:
    """同步一个已解析事件的失败索引，只重算新增或变化的内容。

    Args:
        event: 已由查询服务校验的事件及失败明细。
        github_repository_id: 从该事件读取的稳定仓库 ID，必须为正数。
        received_at: 从该事件读取的首次接收时间，必须带时区。
        client: 调用方创建并负责关闭的异步 Qdrant 客户端。
        embedding_model: 调用方已加载、供本次处理复用的模型。
        settings: 已校验的检索配置，提供目标集合名称。

    Returns:
        int: 本次实际写入的 Point 数量，不包含未变项和删除项。

    Raises:
        ValueError: 事件未解析成功、来源信息无效，
            或生成的向量数量与输入明细数量不一致。
        ApiException: Qdrant 查询、写入或删除失败。
    """

    if event.parse_status != "parsed":
        raise ValueError("只有解析成功的事件才能生成失败向量")

    if github_repository_id <= 0 or received_at.tzinfo is None:
        raise ValueError("索引需要有效仓库 ID 和带时区的接收时间")

    # 首次写入时创建集合；后续事件继续使用同一集合。
    if not await client.collection_exists(settings.qdrant_collection):
        # 没有集合也没有失败时，无需创建空集合。
        if not event.failures:
            return 0
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=models.VectorParams(
                # Collection 接收的每个向量必须是 384 维
                size=384,
                # 指定使用余弦相似度相关的度量方式进行向量比较
                distance=models.Distance.COSINE,
            ),
        )

    # 集合存在时，即使当前已没有失败，也要读取并清理旧索引。
    stored = await _load_event_index_payloads(
        event_id=event.event_id,
        client=client,
        settings=settings,
    )
    expected_ids: set[str] = set()
    written_count = 0

    for offset in range(
        0,
        len(event.failures),
        # 每次处理最多 32 条失败明细
        VECTOR_WRITE_BATCH_SIZE,
    ):
        # 切片
        batch = event.failures[offset : offset + VECTOR_WRITE_BATCH_SIZE]
        # 每项绑定 Point ID、实际计算文本和待写元数据。
        pending: list[tuple[str, str, dict[str, object]]] = []

        for detail in batch:
            # 沿用已有 UUID 规则，不改变历史 Point 的身份。
            identity = f"{event.event_id}:{detail.testcase_index}:{detail.issue_index}"
            point_id = str(uuid5(FAILURE_POINT_NAMESPACE, identity))
            # 未变化的记录也属于当前应保留的索引。
            expected_ids.add(point_id)
            text = build_failure_index_text(detail)
            payload: dict[str, object] = {
                "event_id": event.event_id,
                "github_repository_id": github_repository_id,
                "testcase_index": detail.testcase_index,
                "issue_index": detail.issue_index,
                # datetime 转成可放入 JSON 的带时区时间文本。
                "received_at": received_at.isoformat(),
                # 摘要针对实际生成向量的同一份文本。
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "index_version": FAILURE_VECTOR_INDEX_VERSION,
            }
            # 同时核对内容、来源和版本，不只比较错误文本。
            if stored.get(point_id) == payload:
                continue
            pending.append((point_id, text, payload))

        if not pending:
            continue

        texts = [text for _, text, _ in pending]

        # embed 返回可迭代结果；由工作线程执行 list，消费结果并完成计算。
        # 取消时等待实际计算结束，复用检索已有的线程执行规则。
        # 默认参数绑定本批次文本，不让闭包依赖循环变量的后续赋值。
        vectors = await run_embedding_in_thread(
            lambda texts=texts: list(embedding_model.embed(texts, batch_size=1))
        )

        points = []

        # 数量必须一一对应，避免漏掉明细或把向量绑定到错误来源。
        # zip() 会按下标，把多个可迭代对象中的元素配成一组
        for (point_id, _, payload), vector in zip(
            pending,
            vectors,
            # 防止三个列表数量不一致
            strict=True,
        ):
            points.append(
                models.PointStruct(
                    # 使用基于业务身份的 UUID v5 保证重复写入定位相同 Point
                    id=point_id,
                    # 将 NumPy 向量转换成客户端需要的普通数字列表。
                    vector=vector.tolist(),
                    payload=payload,
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

    # 只清理本次读到、但当前数据库明细已不再需要的位置。
    stale_ids: list[models.ExtendedPointId] = sorted(stored.keys() - expected_ids)
    # 所有写入成功后才清理，前面失败时不会提前删除旧记录。
    for offset in range(0, len(stale_ids), VECTOR_WRITE_BATCH_SIZE):
        await client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=models.PointIdsList(
                points=stale_ids[offset : offset + VECTOR_WRITE_BATCH_SIZE],
            ),
            wait=True,
        )

    return written_count

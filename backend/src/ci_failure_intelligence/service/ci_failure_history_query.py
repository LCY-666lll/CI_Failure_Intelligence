"""组织指定 CI 事件的只读历史检索。"""

import asyncio
import logging
from typing import Literal

from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.schemas.failure_history import (
    CiFailureHistoryResponse,
    HistoricalFailureResponse,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryUnavailableError,
    get_ci_failure_context,
)
from ci_failure_intelligence.service.failure_history import (
    MAX_CURRENT_FAILURES_FOR_HISTORY,
    retrieve_event_history,
)

logger = logging.getLogger(__name__)


async def get_ci_failure_history(
    *,
    event_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    embedding_semaphore: asyncio.Semaphore,
    retrieval_settings: RetrievalSettings | None,
    qdrant_client: AsyncQdrantClient | None,
    embedding_model: TextEmbedding | None,
) -> CiFailureHistoryResponse:
    """读取当前事件，并返回历史检索状态和核实后的结果。

    Args:
        event_id: 已校验为正整数的当前 CI 事件主键。
        session_factory: 应用共享的数据库 Session 工厂。
        embedding_semaphore: 与诊断图共用的模型推理许可。
        retrieval_settings: 已校验的检索配置；为空表示未启用检索。
        qdrant_client: 应用持有并负责管理的共享检索客户端。
        embedding_model: 应用启动阶段加载的共享模型。

    Returns:
        CiFailureHistoryResponse: 当前解析状态、检索状态和最多五条历史。

    Raises:
        CiFailureQueryNotFoundError: 当前事件不存在。
        CiFailureQueryUnavailableError: 当前事件或来源信息无法读取。
        ValueError: 检索输入、向量或历史来源数据不符合约束。
    """
    # 当前事实读取失败必须向上传播，不能伪装成历史检索降级。
    async with session_factory() as session:
        event, repository_id, received_at = await get_ci_failure_context(
            session,
            event_id,
        )

    # 离开上面的作用域后，当前查询 Session 已关闭。
    history: list[HistoricalFailureResponse] = []
    retrieval_status: Literal[
        "not_ready",
        "available",
        "degraded",
        "disabled",
    ]

    if event.parse_status != "parsed":
        retrieval_status = "not_ready"
    elif retrieval_settings is None:
        retrieval_status = "disabled"
    elif not event.failures:
        # 已成功解析且没有错误，无需执行模型计算。
        retrieval_status = "available"
    elif qdrant_client is None or embedding_model is None:
        retrieval_status = "degraded"
        logger.warning(
            "历史查询降级：event_id=%s，reason=resource_not_ready",
            event_id,
        )
    else:
        try:
            # 与 AI 诊断共用同一许可，不能在这里另建 Semaphore。
            async with embedding_semaphore:
                history = await retrieve_event_history(
                    event=event,
                    github_repository_id=repository_id,
                    received_at=received_at,
                    session_factory=session_factory,
                    client=qdrant_client,
                    embedding_model=embedding_model,
                    settings=retrieval_settings,
                )
            retrieval_status = "available"
        except (
            ApiException,
            CiFailureQueryUnavailableError,
            OSError,
        ) as error:
            # 这里只处理历史阶段的基础设施故障，不输出原始异常内容。
            logger.warning(
                "历史查询降级：event_id=%s，error_type=%s",
                event_id,
                type(error).__name__,
            )
            retrieval_status = "degraded"

    return CiFailureHistoryResponse(
        event_id=event.event_id,
        parse_status=event.parse_status,
        retrieval_status=retrieval_status,
        current_failures_truncated=(
            len(event.failures) > MAX_CURRENT_FAILURES_FOR_HISTORY
        ),
        history=history,
    )

"""查询指定CI事件对应的有效历史失败。"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from qdrant_client.http.exceptions import ApiException
from sqlalchemy.exc import SQLAlchemyError

from ci_failure_intelligence.core.config import (
    PostgresSettings,
    RetrievalSettings,
)
from ci_failure_intelligence.infrastructure.ai.embedding_model import (
    create_embedding_model,
)
from ci_failure_intelligence.infrastructure.database import (
    create_database_engine,
    create_database_session_factory,
)
from ci_failure_intelligence.infrastructure.qdrant_client import (
    create_qdrant_client,
)
from ci_failure_intelligence.schemas.failure_history import (
    HistoricalFailureResponse,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
    get_ci_failure_context,
)
from ci_failure_intelligence.service.failure_history import (
    retrieve_event_history,
)


async def _run_event_history(
    event_id: int,
    postgres_settings: PostgresSettings,
    retrieval_settings: RetrievalSettings,
) -> list[HistoricalFailureResponse]:
    """组织资源，查询一个事件对应的真实历史。

    Args:
        event_id: 已校验为正整数的当前事件ID。
        postgres_settings: 已校验的数据库配置。
        retrieval_settings: 已校验的检索配置。

    Returns:
        list[HistoricalFailureResponse]: 核对后的历史列表，最多5条。

    Raises:
        CiFailureQueryNotFoundError: 当前事件不存在。
        CiFailureQueryUnavailableError: 数据库查询失败。
        ValueError: 事件状态或模型输入不符合要求。
        ApiException: Qdrant请求失败。
    """

    engine = create_database_engine(postgres_settings)
    client = None

    try:
        session_factory = create_database_session_factory(engine)
        async with session_factory() as session:
            event, repository_id, received_at = await get_ci_failure_context(
                session, event_id
            )

        # 读取Session已经关闭，模型计算不占用这次数据库连接。
        if event.parse_status != "parsed":
            raise ValueError("事件尚未成功解析")

        if not event.failures:
            return []

        model = await asyncio.to_thread(
            create_embedding_model,
            retrieval_settings,
        )
        client = create_qdrant_client(retrieval_settings)

        return await retrieve_event_history(
            event=event,
            github_repository_id=repository_id,
            received_at=received_at,
            session_factory=session_factory,
            client=client,
            embedding_model=model,
            settings=retrieval_settings,
        )

    finally:
        try:
            if client is not None:
                await client.close()
        finally:
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数，运行检索并输出JSON。

    Args:
        argv: 可选参数列表；省略时读取终端参数。

    Returns:
        int: 正常查询为0，运行失败为1。

    Raises:
        SystemExit: argparse处理帮助或拒绝非法参数时退出。
    """

    parser = argparse.ArgumentParser(
        description="查询指定CI事件的有效历史失败",
    )
    parser.add_argument(
        "event_id",
        type=int,
        help="当前CI事件的数据库ID",
    )
    arguments = parser.parse_args(argv)

    if arguments.event_id <= 0:
        parser.error("event_id必须大于0")

    try:
        # 配置来自环境，沿用已有CLI的类型检查处理方式。
        postgres_settings = PostgresSettings()  # pyright: ignore[reportCallIssue]
        retrieval_settings = RetrievalSettings()  # pyright: ignore[reportCallIssue]

        # 用 Runner 创建和管理事件循环，让它执行协程
        with asyncio.Runner(
            loop_factory=asyncio.SelectorEventLoop,
        ) as runner:
            history = runner.run(
                _run_event_history(
                    arguments.event_id,
                    postgres_settings,
                    retrieval_settings,
                )
            )

    except ValidationError:
        print("历史检索失败：配置或事件数据校验失败", file=sys.stderr)
        return 1

    except CiFailureQueryNotFoundError:
        print("历史检索失败：当前CI事件不存在", file=sys.stderr)
        return 1

    except (CiFailureQueryUnavailableError, SQLAlchemyError):
        print("历史检索失败：数据库暂时不可用", file=sys.stderr)
        return 1

    except ApiException:
        print("历史检索失败：Qdrant请求未成功", file=sys.stderr)
        return 1

    except (ValueError, OSError, RuntimeError):
        print(
            "历史检索失败：事件状态、模型缓存或运行资源不符合要求",
            file=sys.stderr,
        )
        return 1

    result = {
        "event_id": arguments.event_id,
        # 将datetime和嵌套响应转换成JSON可表示的数据。
        "history": [item.model_dump(mode="json") for item in history],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

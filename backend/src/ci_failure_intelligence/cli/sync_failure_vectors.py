"""将指定 CI 事件的失败明细写入 Qdrant。"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from qdrant_client.http.exceptions import ApiException
from sqlalchemy import select
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
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.infrastructure.qdrant_client import (
    create_qdrant_client,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
    get_ci_failure_details,
)
from ci_failure_intelligence.service.failure_vector_index import (
    index_ci_failure_event,
)


async def _run_event_index(
    event_id: int,
    postgres_settings: PostgresSettings,
    retrieval_settings: RetrievalSettings,
) -> int:
    """读取一个事件，并组织模型和 Qdrant 完成写入。

    Args:
        event_id: 已校验为正整数的数据库事件 ID。
        postgres_settings: 已校验的 PostgreSQL 配置。
        retrieval_settings: 已校验的检索配置。

    Returns:
        int: 本次确认写入的失败明细数量，包含覆盖已有 Point。

    Raises:
        CiFailureQueryNotFoundError: 事件不存在。
        CiFailureQueryUnavailableError: 详情查询失败。
        SQLAlchemyError: 来源信息查询失败。
        ValueError: 事件状态不允许索引或模型无法正常加载。
        ApiException: Qdrant 请求失败。
    """

    engine = create_database_engine(postgres_settings)
    client = None

    try:
        session_factory = create_database_session_factory(engine)

        async with session_factory() as session:
            # 复用已有查询服务，不复制事件和明细查询逻辑。
            event = await get_ci_failure_details(session, event_id)

            # 读取检索过滤和历史时间边界需要的内部来源信息。
            metadata_result = await session.execute(
                select(
                    CiFailureEventModel.github_repository_id,
                    CiFailureEventModel.received_at,
                ).where(CiFailureEventModel.id == event_id)
            )
            repository_id, received_at = metadata_result.one()

        # Session 已关闭，后续模型计算不占用这次数据库连接。
        if event.parse_status != "parsed":
            raise ValueError("事件尚未成功解析")

        if not event.failures:
            return 0

        # 模型加载会读文件并初始化推理环境，放到工作线程执行。
        model = await asyncio.to_thread(
            create_embedding_model,
            retrieval_settings,
        )
        client = create_qdrant_client(retrieval_settings)

        return await index_ci_failure_event(
            event=event,
            github_repository_id=repository_id,
            received_at=received_at,
            client=client,
            embedding_model=model,
            settings=retrieval_settings,
        )

    finally:
        try:
            if client is not None:
                await client.close()
        finally:
            # 即使客户端关闭失败，也继续释放数据库连接池。
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """解析终端参数并运行单事件索引。

    Args:
        argv: 测试或其他调用方提供的参数；省略时读取终端参数。

    Returns:
        int: 成功为 0，运行失败为 1。

    Raises:
        SystemExit: argparse 处理帮助请求或拒绝非法参数时退出。
    """

    parser = argparse.ArgumentParser(
        description="将指定 CI 事件的失败明细写入 Qdrant",
    )
    parser.add_argument(
        "event_id",
        type=int,
        help="已解析 CI 事件的数据库 ID",
    )
    arguments = parser.parse_args(argv)

    # 在读取密钥、创建连接和加载模型之前拒绝非法 ID。
    if arguments.event_id <= 0:
        parser.error("event_id 必须大于 0")

    try:
        # 配置由环境动态提供，沿用已有 CLI 的类型检查处理方式。
        postgres_settings = PostgresSettings()  # pyright: ignore[reportCallIssue]
        retrieval_settings = RetrievalSettings()  # pyright: ignore[reportCallIssue]

        # 与已有 pending CLI 保持一致，兼容当前 Windows Psycopg 环境。
        with asyncio.Runner(
            loop_factory=asyncio.SelectorEventLoop,
        ) as runner:
            written = runner.run(
                _run_event_index(
                    arguments.event_id,
                    postgres_settings,
                    retrieval_settings,
                )
            )

    except ValidationError:
        print(
            "向量写入失败：运行配置或事件数据校验失败",
            file=sys.stderr,
        )
        return 1

    except CiFailureQueryNotFoundError:
        print("向量写入失败：CI 事件不存在", file=sys.stderr)
        return 1

    except (CiFailureQueryUnavailableError, SQLAlchemyError):
        print("向量写入失败：数据库暂时不可用", file=sys.stderr)
        return 1

    except ApiException:
        print("向量写入失败：Qdrant 请求未成功", file=sys.stderr)
        return 1

    except (ValueError, OSError, RuntimeError):
        print(
            "向量写入失败：事件状态、模型缓存或运行资源不符合要求",
            file=sys.stderr,
        )
        return 1

    print(f"CI 失败向量写入完成：event_id={arguments.event_id} written={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

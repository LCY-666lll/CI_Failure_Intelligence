"""主动处理一批 pending JUnit 事件。"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from ci_failure_intelligence.core.config import PostgresSettings
from ci_failure_intelligence.infrastructure.database import (
    create_database_engine,
    create_database_session_factory,
)
from ci_failure_intelligence.service.junit_pending import (
    DEFAULT_PENDING_BATCH_LIMIT,
    MAX_PENDING_BATCH_LIMIT,
    JunitPendingBatchResult,
    process_pending_junit_batch,
)
from ci_failure_intelligence.service.junit_persistence import (
    JunitPersistenceUnavailableError,
)


def _parse_limit(raw_value: str) -> int:
    """解析并校验单次 pending 批处理数量。

    Args:
        raw_value: argparse 从命令行读取的原始文本。

    Returns:
        int: 位于 1 到 100 范围内的批处理数量。

    Raises:
        argparse.ArgumentTypeError: 输入不是整数或超出允许范围。
    """
    try:
        limit = int(raw_value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit 必须是整数") from error

    if not 1 <= limit <= MAX_PENDING_BATCH_LIMIT:
        raise argparse.ArgumentTypeError(
            f"limit 必须位于 1 到 {MAX_PENDING_BATCH_LIMIT} 之间"
        )

    return limit


def _build_parser() -> argparse.ArgumentParser:
    """创建 pending JUnit 管理命令的参数解析器。"""
    parser = argparse.ArgumentParser(
        description="处理 PostgreSQL 中尚未解析的 JUnit 事件",
    )
    parser.add_argument(
        "--limit",
        type=_parse_limit,
        default=DEFAULT_PENDING_BATCH_LIMIT,
        help=(
            "本次最多处理的 pending 事件数量，"
            f"允许 1 到 {MAX_PENDING_BATCH_LIMIT}，"
            f"默认 {DEFAULT_PENDING_BATCH_LIMIT}"
        ),
    )
    return parser


async def _run_pending_batch(
    settings: PostgresSettings,
    limit: int,
) -> JunitPendingBatchResult:
    """创建数据库资源并执行一次 pending 批处理。

    Args:
        settings: 已从运行环境读取并通过校验的 PostgreSQL 配置。
        limit: 已由 argparse 校验为 1 到 100 的批处理数量。

    Returns:
        JunitPendingBatchResult: 已提交的批次处理统计。

    Raises:
        JunitPersistenceUnavailableError: PostgreSQL 无法可靠完成处理。
    """
    engine = create_database_engine(settings)

    try:
        session_factory = create_database_session_factory(engine)

        return await process_pending_junit_batch(
            session_factory,
            limit,
        )
    finally:
        # 成功、数据库异常或未预期异常都必须释放连接池。
        await engine.dispose()


def _run_pending_batch_with_selector_loop(
    settings: PostgresSettings,
    limit: int,
) -> JunitPendingBatchResult:
    """使用兼容 Psycopg 的事件循环执行异步批处理。

    Args:
        settings: 已通过校验的 PostgreSQL 配置。
        limit: 已通过命令行校验的批处理数量。

    Returns:
        JunitPendingBatchResult: 异步批处理最终提交的统计。
    """
    with asyncio.Runner(
        loop_factory=asyncio.SelectorEventLoop,
    ) as runner:
        return runner.run(
            _run_pending_batch(
                settings,
                limit,
            )
        )


def main(argv: Sequence[str] | None = None) -> int:
    """运行 pending JUnit 管理命令。

    Args:
        argv: 可选命令行参数。省略时由 argparse 读取系统参数。

    Returns:
        int: 成功为 0；配置或数据库故障为 1。
    """
    parser = _build_parser()

    # 参数校验先于配置和数据库创建。
    # --limit 非法时，命令不会读取密码，也不会连接 PostgreSQL。
    arguments = parser.parse_args(argv)

    try:
        # 密码由 BaseSettings 从 CFI_POSTGRES_PASSWORD 或 .env 动态注入；
        # Pyright 无法识别运行时注入，因此只忽略这一处构造签名误报。
        settings = PostgresSettings()  # pyright: ignore[reportCallIssue]

        result = _run_pending_batch_with_selector_loop(
            settings,
            arguments.limit,
        )

    except ValidationError:
        # 不直接打印 ValidationError，避免配置错误信息包含敏感输入。
        print(
            "JUnit pending 处理失败：PostgreSQL 配置无效",
            file=sys.stderr,
        )
        return 1

    except JunitPersistenceUnavailableError:
        # 数据库驱动错误可能包含连接信息，因此只输出稳定业务摘要。
        print(
            "JUnit pending 处理失败：数据库暂时不可用",
            file=sys.stderr,
        )
        return 1

    print(
        "JUnit pending 处理完成："
        f"selected={result.selected_count} "
        f"parsed={result.parsed_count} "
        f"failed={result.failed_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

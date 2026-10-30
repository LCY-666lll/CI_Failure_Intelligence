"""使用真实 PostgreSQL 验证 JUnit 明细的整体替换事务。"""

import asyncio
import os
import time
from uuid import uuid4

import pytest
from ci_failure_intelligence.core.config import PostgresSettings
from ci_failure_intelligence.infrastructure.database import (
    create_database_engine,
    create_database_session_factory,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_detail import (
    CiFailureDetailModel,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.service.junit_persistence import (
    JunitPersistenceStatus,
    process_junit_event,
)
from sqlalchemy import delete, func, insert, select

# 普通单元测试和 GitHub Runner 不依赖真实 PostgreSQL。
# 本机只有明确打开开关时，才执行会写入临时数据库记录的集成测试。
pytestmark = pytest.mark.skipif(
    os.environ.get("CFI_RUN_POSTGRES_INTEGRATION") != "1",
    reason="需要显式启用本机 PostgreSQL 集成测试",
)


JUNIT_XML_WITH_TWO_ISSUES = """\
<testsuite name="backend">
  <testcase classname="tests.test_example" name="test_example">
    <failure message="assertion failed">failure details</failure>
    <error message="teardown failed">error details</error>
  </testcase>
</testsuite>
"""


async def _run_real_postgres_scenario() -> None:
    """写入临时事件并验证首次解析、重新解析和级联清理。"""
    # 密码由 BaseSettings 从 CFI_POSTGRES_PASSWORD 动态注入；
    # Pyright 无法识别运行时环境变量，因此只忽略这一处构造签名误报。
    settings = PostgresSettings()  # pyright: ignore[reportCallIssue]
    engine = create_database_engine(settings)
    session_factory = create_database_session_factory(engine)
    event_id: int | None = None

    try:
        # 纳秒时间戳和随机 job_key 共同隔离并行或重复执行的测试数据。
        async with session_factory.begin() as session:
            event_id = (
                await session.execute(
                    insert(CiFailureEventModel)
                    .values(
                        github_repository_id=9_999_999,
                        repository_full_name="test-only/integration",
                        run_id=time.time_ns(),
                        run_attempt=1,
                        job_key=f"junit-integration-{uuid4().hex}",
                        workflow_name="JUnit persistence integration",
                        job_name="real-postgres",
                        commit_sha="a" * 40,
                        head_branch="test-only",
                        junit_xml=JUNIT_XML_WITH_TWO_ISSUES,
                        payload_sha256="b" * 64,
                    )
                    .returning(CiFailureEventModel.id)
                )
            ).scalar_one()

        # 第一次处理应写入 failure 和 error 两条明细。
        async with session_factory() as session:
            first_result = await process_junit_event(session, event_id)

        assert first_result.status is JunitPersistenceStatus.PARSED
        assert first_result.detail_count == 2

        # 再次处理同一事件必须整体替换，不能追加成四条。
        async with session_factory() as session:
            second_result = await process_junit_event(session, event_id)

        assert second_result.status is JunitPersistenceStatus.PARSED
        assert second_result.detail_count == 2

        async with session_factory() as session:
            event_state = (
                await session.execute(
                    select(
                        CiFailureEventModel.parse_status,
                        CiFailureEventModel.parse_error_code,
                        CiFailureEventModel.parse_finished_at,
                    ).where(CiFailureEventModel.id == event_id)
                )
            ).one()
            issue_types = (
                (
                    await session.execute(
                        select(CiFailureDetailModel.issue_type)
                        .where(CiFailureDetailModel.event_id == event_id)
                        .order_by(CiFailureDetailModel.issue_index)
                    )
                )
                .scalars()
                .all()
            )

        assert event_state[0] == "parsed"
        assert event_state[1] is None
        assert event_state[2] is not None
        assert issue_types == ["failure", "error"]

    finally:
        if event_id is not None:
            # 删除临时父事件；外键 ON DELETE CASCADE 应同步清理明细。
            async with session_factory.begin() as cleanup_session:
                await cleanup_session.execute(
                    delete(CiFailureEventModel).where(
                        CiFailureEventModel.id == event_id
                    )
                )

            async with session_factory() as verification_session:
                remaining_details = (
                    await verification_session.execute(
                        select(func.count())
                        .select_from(CiFailureDetailModel)
                        .where(CiFailureDetailModel.event_id == event_id)
                    )
                ).scalar_one()

            assert remaining_details == 0

        await engine.dispose()


def test_real_postgres_replaces_junit_details_atomically() -> None:
    """真实 PostgreSQL 应保存两条明细，并在重新解析时整体替换。"""
    # Psycopg 异步连接在 Windows 上需要 SelectorEventLoop；
    # Linux 同样支持该事件循环，因此测试入口可以保持一致。
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(_run_real_postgres_scenario())

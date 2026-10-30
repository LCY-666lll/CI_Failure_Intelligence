"""使用真实 PostgreSQL 验证 pending JUnit 批处理完整链路。"""

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
from ci_failure_intelligence.service.junit_pending import process_pending_junit_batch
from sqlalchemy import delete, func, insert, select

# 普通单元测试和 GitHub Runner 不依赖真实 PostgreSQL。
# 本机只有明确打开开关时，才执行会写入临时数据库记录的集成测试。
pytestmark = pytest.mark.skipif(
    os.environ.get("CFI_RUN_POSTGRES_INTEGRATION") != "1",
    reason="需要显式启用本机 PostgreSQL 集成测试",
)


VALID_JUNIT_XML = """\
<testsuite name="backend">
  <testcase classname="tests.test_pending" name="test_valid_event">
    <failure message="assertion failed">failure details</failure>
  </testcase>
</testsuite>
"""

INVALID_JUNIT_XML = "<testsuite><testcase>"


def _event_values(*, run_id: int, job_key: str, junit_xml: str) -> dict[str, object]:
    """构造只供真实数据库集成测试使用的临时事件字段。"""
    return {
        "github_repository_id": 9_999_998,
        "repository_full_name": "test-only/pending-integration",
        "run_id": run_id,
        "run_attempt": 1,
        "job_key": job_key,
        "workflow_name": "JUnit pending integration",
        "job_name": "real-postgres",
        "commit_sha": "c" * 40,
        "head_branch": "test-only",
        "junit_xml": junit_xml,
        "payload_sha256": "d" * 64,
    }


async def _run_real_pending_batch_scenario() -> None:
    """写入两个临时 pending 事件并验证批处理的最终数据库状态。"""
    settings = PostgresSettings()  # pyright: ignore[reportCallIssue]
    engine = create_database_engine(settings)
    session_factory = create_database_session_factory(engine)
    event_ids: list[int] = []

    try:
        async with session_factory() as precondition_session:
            existing_pending_count = (
                await precondition_session.execute(
                    select(func.count())
                    .select_from(CiFailureEventModel)
                    .where(CiFailureEventModel.parse_status == "pending")
                )
            ).scalar_one()

        # 批处理会按主键选择所有 pending 事件。若开发库已有真实待处理数据，
        # 跳过测试以免自动化验证改变用户尚未处理的业务事件。
        if existing_pending_count:
            pytest.skip("开发数据库已有 pending 事件，拒绝在集成测试中处理")

        run_id = time.time_ns()
        unique_suffix = uuid4().hex

        async with session_factory.begin() as insert_session:
            result = await insert_session.execute(
                insert(CiFailureEventModel)
                .values(
                    [
                        _event_values(
                            run_id=run_id,
                            job_key=f"pending-valid-{unique_suffix}",
                            junit_xml=VALID_JUNIT_XML,
                        ),
                        _event_values(
                            run_id=run_id + 1,
                            job_key=f"pending-invalid-{unique_suffix}",
                            junit_xml=INVALID_JUNIT_XML,
                        ),
                    ]
                )
                .returning(CiFailureEventModel.id)
            )
            event_ids = list(result.scalars().all())

        batch_result = await process_pending_junit_batch(
            session_factory,
            limit=2,
        )

        assert batch_result.selected_count == 2
        assert batch_result.parsed_count == 1
        assert batch_result.failed_count == 1

        async with session_factory() as verification_session:
            event_states = (
                await verification_session.execute(
                    select(
                        CiFailureEventModel.id,
                        CiFailureEventModel.parse_status,
                        CiFailureEventModel.parse_error_code,
                    )
                    .where(CiFailureEventModel.id.in_(event_ids))
                    .order_by(CiFailureEventModel.id)
                )
            ).all()
            detail_count = (
                await verification_session.execute(
                    select(func.count())
                    .select_from(CiFailureDetailModel)
                    .where(CiFailureDetailModel.event_id.in_(event_ids))
                )
            ).scalar_one()

        assert event_states == [
            (event_ids[0], "parsed", None),
            (event_ids[1], "failed", "invalid_xml"),
        ]
        assert detail_count == 1

    finally:
        if event_ids:
            # 删除临时父事件，数据库通过 ON DELETE CASCADE 清理失败明细。
            async with session_factory.begin() as cleanup_session:
                await cleanup_session.execute(
                    delete(CiFailureEventModel).where(
                        CiFailureEventModel.id.in_(event_ids)
                    )
                )

        await engine.dispose()


def test_real_postgres_processes_pending_events_as_one_bounded_batch() -> None:
    """真实批处理应分别提交合法报告和损坏报告的最终状态。"""
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(_run_real_pending_batch_scenario())

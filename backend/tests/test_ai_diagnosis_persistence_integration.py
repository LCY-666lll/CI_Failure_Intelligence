"""使用真实 PostgreSQL 验证 AI 诊断任务的原子抢占语义。"""

import asyncio
import os
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from ci_failure_intelligence.core.config import PostgresSettings
from ci_failure_intelligence.infrastructure.database import (
    create_database_engine,
    create_database_session_factory,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_diagnosis import (
    CiFailureDiagnosisModel,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis
from ci_failure_intelligence.service.ai_diagnosis import AiDiagnosisGenerationResult
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisCacheIdentity,
    AiDiagnosisClaimResult,
    AiDiagnosisClaimStatus,
    AiDiagnosisFinalStatus,
    AiDiagnosisLeaseLostError,
    claim_ai_diagnosis,
    complete_ai_diagnosis,
    fail_ai_diagnosis,
)
from sqlalchemy import delete, func, insert, select, update

# 普通单元测试和 GitHub Runner 不依赖真实 PostgreSQL。
# 只有显式打开开关时，才写入随后会清理的临时事件。
pytestmark = pytest.mark.skipif(
    os.environ.get("CFI_RUN_POSTGRES_INTEGRATION") != "1",
    reason="需要显式启用本机 PostgreSQL 集成测试",
)


def _cache_identity(
    evidence_character: str = "a",
) -> AiDiagnosisCacheIdentity:
    return AiDiagnosisCacheIdentity(
        prompt_version="v1",
        output_schema_version="v1",
        requested_model="deepseek-flash",
        evidence_sha256=evidence_character * 64,
        model_config_sha256="b" * 64,
    )


def _generation_result(event_id: int) -> AiDiagnosisGenerationResult:
    """构造已经通过项目输出契约的测试诊断。"""

    diagnosis = AiDiagnosis.model_validate(
        {
            "summary": "测试断言失败导致 CI Job 失败",
            "root_causes": [
                {
                    "title": "测试断言失败",
                    "explanation": "failure:0:0 包含测试断言失败证据",
                    "confidence": "high",
                    "evidence_ids": ["failure:0:0"],
                }
            ],
            "recommended_actions": ["检查失败断言和对应业务逻辑"],
            "needs_more_information": False,
            "missing_information": [],
        }
    )
    return AiDiagnosisGenerationResult(
        event_id=event_id,
        diagnosis=diagnosis,
        # provider 允许不返回实际模型名。
        model_name=None,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
    )


async def _run_real_postgres_scenario() -> None:
    """验证并发、缓存、重试、租约过期与级联清理。"""

    settings = PostgresSettings()  # pyright: ignore[reportCallIssue]
    engine = create_database_engine(settings)
    session_factory = create_database_session_factory(engine)
    event_id: int | None = None

    async def claim(
        cache_identity: AiDiagnosisCacheIdentity,
    ) -> AiDiagnosisClaimResult:
        async with session_factory() as session:
            return await claim_ai_diagnosis(
                session=session,
                event_id=event_id,  # type: ignore[arg-type]
                cache_identity=cache_identity,
            )

    try:
        async with session_factory.begin() as session:
            event_id = (
                await session.execute(
                    insert(CiFailureEventModel)
                    .values(
                        github_repository_id=9_999_999,
                        repository_full_name="test-only/ai-diagnosis-integration",
                        run_id=time.time_ns(),
                        run_attempt=1,
                        job_key=f"ai-diagnosis-integration-{uuid4().hex}",
                        workflow_name="AI diagnosis persistence integration",
                        job_name="real-postgres",
                        commit_sha="a" * 40,
                        head_branch="test-only",
                        junit_xml="<testsuite></testsuite>",
                        payload_sha256="c" * 64,
                    )
                    .returning(CiFailureEventModel.id)
                )
            ).scalar_one()

        original_identity = _cache_identity()

        # 两个真实连接并发抢占时，只能有一个取得付费调用资格。
        concurrent_results = await asyncio.gather(
            claim(original_identity),
            claim(original_identity),
        )
        statuses = {result.status for result in concurrent_results}

        assert statuses == {
            AiDiagnosisClaimStatus.CLAIMED,
            AiDiagnosisClaimStatus.IN_PROGRESS,
        }

        claimed_result = next(
            result
            for result in concurrent_results
            if result.status is AiDiagnosisClaimStatus.CLAIMED
        )
        in_progress_result = next(
            result
            for result in concurrent_results
            if result.status is AiDiagnosisClaimStatus.IN_PROGRESS
        )

        assert claimed_result.lease_token is not None
        assert claimed_result.attempt_count == 1
        assert in_progress_result.lease_token is None
        assert in_progress_result.attempt_count == 1

        claimed_token = claimed_result.lease_token
        generation_result = _generation_result(event_id)

        # event_id 正确但 token 错误时，旧执行者不能写回结果。
        async with session_factory() as session:
            with pytest.raises(AiDiagnosisLeaseLostError):
                await complete_ai_diagnosis(
                    session=session,
                    lease_token=uuid4(),
                    generation_result=generation_result,
                )

        # 当前有效租约可以提交完成态；provider model_name 允许为空。
        async with session_factory() as session:
            completed_result = await complete_ai_diagnosis(
                session=session,
                lease_token=claimed_token,
                generation_result=generation_result,
            )

        assert completed_result.status is AiDiagnosisFinalStatus.COMPLETED

        async with session_factory() as session:
            completed_row = (
                await session.execute(
                    select(CiFailureDiagnosisModel).where(
                        CiFailureDiagnosisModel.event_id == event_id
                    )
                )
            ).scalar_one()

        assert completed_row.status == "completed"
        assert completed_row.lease_token is None
        assert completed_row.diagnosis_json == generation_result.diagnosis.model_dump(
            mode="json"
        )
        assert completed_row.model_name is None
        assert completed_row.total_tokens == 150

        cached_result = await claim(original_identity)

        assert cached_result.status is AiDiagnosisClaimStatus.CACHED
        assert cached_result.diagnosis_json == generation_result.diagnosis.model_dump(
            mode="json"
        )
        assert cached_result.model_name is None
        assert cached_result.lease_token is None

        # 已完成结果的缓存身份变化后，允许生成新诊断。
        changed_identity = _cache_identity("d")
        changed_result = await claim(changed_identity)

        assert changed_result.status is AiDiagnosisClaimStatus.CLAIMED
        assert changed_result.attempt_count == 2
        assert changed_result.lease_token is not None
        changed_token = changed_result.lease_token

        # 新任务租约仍有效时，另一个不同缓存请求也不能抢走执行权。
        unexpired_result = await claim(_cache_identity("e"))

        assert unexpired_result.status is AiDiagnosisClaimStatus.IN_PROGRESS
        assert unexpired_result.attempt_count == 2

        # processing 租约过期后允许恢复，并增加尝试次数。
        async with session_factory.begin() as session:
            await session.execute(
                update(CiFailureDiagnosisModel)
                .where(CiFailureDiagnosisModel.event_id == event_id)
                .values(
                    lease_expires_at=func.now() - timedelta(seconds=1),
                    updated_at=func.now(),
                )
            )

        # token 即使仍匹配，只要租约过期也必须立即失去写回权。
        async with session_factory() as session:
            with pytest.raises(AiDiagnosisLeaseLostError):
                await complete_ai_diagnosis(
                    session=session,
                    lease_token=changed_token,
                    generation_result=generation_result,
                )

        expired_result = await claim(changed_identity)

        assert expired_result.status is AiDiagnosisClaimStatus.CLAIMED
        assert expired_result.attempt_count == 3
        assert expired_result.lease_token not in {
            claimed_result.lease_token,
            changed_result.lease_token,
        }
        expired_token = expired_result.lease_token
        assert expired_token is not None

        # 当前租约可以保存失败状态，并把排查摘要限制为数据库上限。
        async with session_factory() as session:
            failed_result = await fail_ai_diagnosis(
                session=session,
                event_id=event_id,
                lease_token=expired_token,
                error_code="  test_only_failure  ",
                error_message="x" * 2_100,
            )

        assert failed_result.status is AiDiagnosisFinalStatus.FAILED

        async with session_factory() as session:
            failed_row = (
                await session.execute(
                    select(CiFailureDiagnosisModel).where(
                        CiFailureDiagnosisModel.event_id == event_id
                    )
                )
            ).scalar_one()

        assert failed_row.status == "failed"
        assert failed_row.diagnosis_json is None
        assert failed_row.error_code == "test_only_failure"
        assert failed_row.error_message == "x" * 2_000

        # failed 任务允许下一次显式请求重新抢占。
        failed_retry_result = await claim(changed_identity)

        assert failed_retry_result.status is AiDiagnosisClaimStatus.CLAIMED
        assert failed_retry_result.attempt_count == 4

    finally:
        if event_id is not None:
            async with session_factory.begin() as cleanup_session:
                await cleanup_session.execute(
                    delete(CiFailureEventModel).where(
                        CiFailureEventModel.id == event_id
                    )
                )

            async with session_factory() as verification_session:
                remaining_diagnosis = (
                    await verification_session.execute(
                        select(func.count())
                        .select_from(CiFailureDiagnosisModel)
                        .where(CiFailureDiagnosisModel.event_id == event_id)
                    )
                ).scalar_one()

            assert remaining_diagnosis == 0

        await engine.dispose()


def test_real_postgres_claims_ai_diagnosis_atomically() -> None:
    """真实 PostgreSQL 应只授予一个租约并按状态机处理后续请求。"""

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(_run_real_postgres_scenario())

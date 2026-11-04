"""验证 AI 诊断只能引用本次 evidence pack 中的真实证据。"""

import pytest
from ci_failure_intelligence.schemas.ai_diagnosis import (
    AiDiagnosis,
    AiRootCause,
)
from ci_failure_intelligence.schemas.ai_evidence import (
    AiEvidencePack,
    AiFailureEvidence,
)
from ci_failure_intelligence.service.ai_diagnosis_validation import (
    AiDiagnosisEvidenceReferenceError,
    validate_ai_diagnosis_evidence,
)


def build_evidence(
    *,
    testcase_index: int,
    issue_index: int = 0,
) -> AiFailureEvidence:
    """构造一条具有稳定 XML 位置编号的模型证据。"""

    return AiFailureEvidence(
        evidence_id=f"failure:{testcase_index}:{issue_index}",
        testcase_index=testcase_index,
        issue_index=issue_index,
        suite_name="backend",
        classname="tests.test_orders",
        test_name=f"test_order_{testcase_index}_{issue_index}",
        issue_type="failure",
        message="expected 200 but received 500",
        details="AssertionError: expected 200 but received 500",
        content_truncated=False,
    )


def build_evidence_pack() -> AiEvidencePack:
    """构造包含两条可引用失败证据的完整输入包。"""

    failures = [
        build_evidence(testcase_index=3),
        build_evidence(testcase_index=7),
    ]
    return AiEvidencePack(
        event_id=42,
        repository_full_name="owner/repository",
        run_id=1001,
        run_attempt=1,
        job_key="backend_tests",
        workflow_name="Backend CI",
        job_name="Backend tests (Python 3.12)",
        commit_sha="a" * 40,
        head_branch="main",
        total_failure_count=2,
        included_failure_count=2,
        truncated=False,
        failures=failures,
    )


def build_diagnosis(
    evidence_ids: list[str],
    *,
    additional_root_causes: list[AiRootCause] | None = None,
) -> AiDiagnosis:
    """构造引用指定证据编号的结构化诊断。"""

    root_causes = [
        AiRootCause(
            title="PostgreSQL 服务不可用",
            explanation="失败证据显示数据库连接被拒绝",
            confidence="high",
            evidence_ids=evidence_ids,
        )
    ]
    if additional_root_causes:
        root_causes.extend(additional_root_causes)

    return AiDiagnosis(
        summary="数据库连接失败导致测试无法完成",
        root_causes=root_causes,
        recommended_actions=["检查 PostgreSQL 容器健康状态"],
        needs_more_information=False,
        missing_information=[],
    )


def test_valid_references_return_original_diagnosis() -> None:
    """全部引用存在时应返回原诊断，不改写已经校验的模型内容。"""

    evidence_pack = build_evidence_pack()
    diagnosis = build_diagnosis(["failure:3:0"])

    result = validate_ai_diagnosis_evidence(diagnosis, evidence_pack)

    assert result is diagnosis


def test_diagnosis_may_reference_only_relevant_subset() -> None:
    """模型不必机械引用包内所有证据，只要引用编号真实存在即可。"""

    evidence_pack = build_evidence_pack()
    diagnosis = build_diagnosis(["failure:7:0"])

    result = validate_ai_diagnosis_evidence(diagnosis, evidence_pack)

    assert result.root_causes[0].evidence_ids == ["failure:7:0"]


def test_unknown_reference_is_rejected_with_event_context() -> None:
    """格式正确但本次未发送的编号必须被视为虚构证据。"""

    evidence_pack = build_evidence_pack()
    diagnosis = build_diagnosis(["failure:999:0"])

    with pytest.raises(AiDiagnosisEvidenceReferenceError) as error_info:
        validate_ai_diagnosis_evidence(diagnosis, evidence_pack)

    assert error_info.value.event_id == 42
    assert error_info.value.unknown_evidence_ids == ("failure:999:0",)


def test_multiple_unknown_references_are_deduplicated_and_sorted() -> None:
    """跨根因重复出现的未知编号应形成稳定、无重复的错误记录。"""

    evidence_pack = build_evidence_pack()
    diagnosis = build_diagnosis(
        ["failure:9:0"],
        additional_root_causes=[
            AiRootCause(
                title="另一个未经证据支持的原因",
                explanation="测试构造的第二个根因",
                confidence="low",
                evidence_ids=["failure:8:0", "failure:9:0"],
            )
        ],
    )

    with pytest.raises(AiDiagnosisEvidenceReferenceError) as error_info:
        validate_ai_diagnosis_evidence(diagnosis, evidence_pack)

    assert error_info.value.unknown_evidence_ids == (
        "failure:8:0",
        "failure:9:0",
    )


def test_reference_error_requires_at_least_one_unknown_id() -> None:
    """没有未知引用时不能错误地产生引用失败异常。"""

    with pytest.raises(ValueError, match="不能为空"):
        AiDiagnosisEvidenceReferenceError(
            event_id=42,
            unknown_evidence_ids=[],
        )

"""检查 AI 的每一个证据引用，是否真的来自本次发送给它的证据包。"""

from collections.abc import Iterable

from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis
from ci_failure_intelligence.schemas.ai_evidence import AiEvidencePack


class AiDiagnosisEvidenceReferenceError(ValueError):
    """表示模型诊断引用了本次输入中不存在的证据。"""

    def __init__(
        self,
        event_id: int,
        unknown_evidence_ids: Iterable[str],
    ) -> None:
        """记录事件及模型虚构或越界引用的证据编号。

        Args:
            event_id: 当前诊断对应的 CI 失败事件主键。
            unknown_evidence_ids: 诊断引用、但不属于本次 evidence pack
                的证据编号。

        Raises:
            ValueError: unknown_evidence_ids 为空，说明调用方错误地
                构造了“未知引用”异常。
        """

        normalized_ids = tuple(sorted(set(unknown_evidence_ids)))

        if not normalized_ids:
            raise ValueError("未知 evidence_id 列表不能为空")

        self.event_id = event_id
        self.unknown_evidence_ids = normalized_ids

        super().__init__(
            "AI 诊断引用了不存在的失败证据："
            f"event_id={event_id}, "
            f"evidence_ids={','.join(normalized_ids)}"
        )


def validate_ai_diagnosis_evidence(
    diagnosis: AiDiagnosis,
    evidence_pack: AiEvidencePack,
) -> AiDiagnosis:
    """确认模型诊断只引用本次实际发送给模型的失败证据。

    Args:
        diagnosis: 已通过 Pydantic 结构校验的模型诊断。
        evidence_pack: 本次模型调用实际使用的有限、脱敏证据包。

    Returns:
        AiDiagnosis: 全部证据引用真实存在时，返回原诊断对象。

    Raises:
        AiDiagnosisEvidenceReferenceError: 任一根因引用的 evidence_id
            不属于本次 evidence pack。
    """

    available_evidence_ids = {failure.evidence_id for failure in evidence_pack.failures}

    referenced_evidence_ids = {
        evidence_id
        for root_cause in diagnosis.root_causes
        for evidence_id in root_cause.evidence_ids
    }

    # 找出 AI 引用了哪些不存在的证据
    unknown_evidence_ids = referenced_evidence_ids - available_evidence_ids

    if unknown_evidence_ids:
        raise AiDiagnosisEvidenceReferenceError(
            event_id=evidence_pack.event_id,
            unknown_evidence_ids=unknown_evidence_ids,
        )

    return diagnosis

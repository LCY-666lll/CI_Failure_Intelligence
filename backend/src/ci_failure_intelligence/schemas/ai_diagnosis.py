"""定义 DeepSeek CI 失败诊断的严格结构化输出契约。"""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# Annotated：把类型和额外的校验信息组合起来
DiagnosisSummary = Annotated[
    str,
    # 给字符串增加校验规则
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=1000,
    ),
]

RootCauseTitle = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=200,
    ),
]

RootCauseExplanation = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=2000,
    ),
]

RecommendedAction = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=500,
    ),
]

MissingInformation = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=500,
    ),
]

EvidenceId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^failure:[0-9]+:[0-9]+$",
    ),
]


class AiRootCause(BaseModel):
    """描述一个由模型提出、且必须引用证据的可能根因。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    title: RootCauseTitle
    explanation: RootCauseExplanation
    confidence: Literal["low", "medium", "high"]

    # 一个根因最多引用 10 条本次 evidence pack 中的失败证据。
    evidence_ids: list[EvidenceId] = Field(
        min_length=1,
        max_length=10,
    )

    @field_validator("evidence_ids")
    @classmethod
    def validate_unique_evidence_ids(
        cls,
        evidence_ids: list[str],
    ) -> list[str]:
        """拒绝同一根因重复引用相同证据。

        Args:
            evidence_ids: 已通过格式和数量校验的证据编号列表。

        Returns:
            list[str]: 没有重复编号的原证据列表。

        Raises:
            ValueError: 同一个 evidence_id 在当前根因中出现多次。
        """

        # set()：自动去重
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("同一根因不能重复引用相同 evidence_id")

        return evidence_ids


class AiDiagnosis(BaseModel):
    """描述一次可以被前端展示和后续持久化的 AI 诊断。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    summary: DiagnosisSummary

    root_causes: list[AiRootCause] = Field(
        min_length=1,
        max_length=5,
    )

    recommended_actions: list[RecommendedAction] = Field(
        min_length=1,
        max_length=10,
    )

    needs_more_information: bool

    missing_information: list[MissingInformation] = Field(
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_missing_information_consistency(self) -> Self:
        """确保“是否缺少信息”和缺失信息列表表达同一含义。

        Returns:
            Self: 状态与列表一致的当前诊断对象。

        Raises:
            ValueError: 缺少信息标记与 missing_information 列表矛盾。
        """

        if self.needs_more_information and not self.missing_information:
            raise ValueError("needs_more_information 为 True 时必须说明缺少的信息")

        if not self.needs_more_information and self.missing_information:
            raise ValueError("needs_more_information 为 False 时缺失信息必须为空")

        return self

"""定义发送给 AI 诊断服务的有限失败证据契约。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AiFailureEvidence(BaseModel):
    """一条失败证据：描述一个允许发送给外部模型的 failure/error 证据。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    # 证据编号由原 XML 位置生成，不依赖可能变化的数据库明细主键。
    evidence_id: str = Field(
        pattern=r"^failure:[0-9]+:[0-9]+$",
    )

    testcase_index: int = Field(ge=0)
    issue_index: int = Field(ge=0)

    # 这些展示字段在 builder 中会先截断，再进入当前模型。
    suite_name: str | None = Field(
        default=None,
        max_length=500,
    )
    classname: str | None = Field(
        default=None,
        max_length=500,
    )
    test_name: str = Field(
        min_length=1,
        max_length=1000,
    )

    issue_type: Literal["failure", "error"]

    message: str | None = Field(
        default=None,
        max_length=2000,
    )
    details: str = Field(
        max_length=4000,
    )

    # 表示当前证据的某个文本字段因模型输入限制被截断。
    content_truncated: bool


class AiEvidencePack(BaseModel):
    """描述一次 AI 诊断允许使用的完整、有限证据包。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    event_id: int = Field(gt=0)
    repository_full_name: str = Field(
        min_length=3,
        max_length=200,
    )

    run_id: int = Field(gt=0)
    run_attempt: int = Field(ge=1)
    job_key: str = Field(
        min_length=1,
        max_length=255,
    )

    workflow_name: str = Field(
        min_length=1,
        max_length=255,
    )
    job_name: str = Field(
        min_length=1,
        max_length=255,
    )

    commit_sha: str = Field(
        pattern=r"^[0-9a-fA-F]{40}$",
    )
    head_branch: str = Field(
        min_length=1,
        max_length=255,
    )

    total_failure_count: int = Field(ge=1)
    # 本次发送了多少条失败消息
    included_failure_count: int = Field(
        ge=1,
        le=20,
    )

    # 只要遗漏失败节点或截断任一文本字段，就必须设为 True。
    truncated: bool

    failures: list[AiFailureEvidence] = Field(
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_evidence_consistency(self) -> Self:
        """确保计数字段和截断标记与实际证据列表一致。
            mode="after" 表示：
            所有字段先完成自己的类型校验
            → 形成 AiEvidencePack
            → 再检查字段之间的关系

        Returns:
            Self: 通过数量和截断一致性校验的当前证据包，返回当前类自己的实例。

        Raises:
            ValueError: 计数与列表不一致，或截断标记无法描述实际内容。
        """

        actual_count = len(self.failures)

        if self.included_failure_count != actual_count:
            raise ValueError("included_failure_count 必须等于 failures 实际数量")

        if self.included_failure_count > self.total_failure_count:
            raise ValueError("included_failure_count 不能超过 total_failure_count")

        # 只要有一条失败证据的文本被截断，整个数据包也必须标记为不完整
        has_truncated_content = any(
            failure.content_truncated for failure in self.failures
        )

        if not self.truncated and (
            self.included_failure_count < self.total_failure_count
            or has_truncated_content
        ):
            raise ValueError("证据不完整时 truncated 必须为 True")

        return self

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

class AiHistoricalFailureEvidence(AiFailureEvidence):
    """描述一条允许发送给模型、且能够追溯来源的历史失败证据。"""

    # 覆盖父类的当前证据编号规则，增加历史事件ID。
    evidence_id: str = Field(
        pattern=r"^history:[1-9][0-9]*:[0-9]+:[0-9]+$",
    )

    event_id: int = Field(gt=0)
    repository_full_name: str = Field(min_length=3, max_length=200)
    run_id: int = Field(gt=0)
    run_attempt: int = Field(ge=1)
    job_key: str = Field(min_length=1, max_length=255)

    # 检索分数表达文本相似程度，不是根因判断的置信度。
    similarity_score: float = Field(
        ge=0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_history_source(self) -> Self:
        """确认历史证据编号与其真实来源位置一致。

        Returns:
            Self: 来源信息一致的当前历史证据。

        Raises:
            ValueError: 证据编号与事件ID或错误位置不一致。
        """

        expected_id = (
            f"history:{self.event_id}:{self.testcase_index}:{self.issue_index}"
        )

        if self.evidence_id != expected_id:
            raise ValueError("历史证据编号必须与事件及错误位置一致")

        return self

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

    # 当前失败有遗漏，或当前失败任一文本字段被截断时为True。
    truncated: bool

    failures: list[AiFailureEvidence] = Field(
        min_length=1,
        max_length=20,
    )

    history:list[AiHistoricalFailureEvidence] = Field(
        default_factory=list,
        max_length=5,
    )

    # 历史被省略或任一历史正文被截断时，必须明确告知模型。
    history_truncated: bool = False

    retrieval_status: Literal[
        "available",
        "degraded",
        "disabled",
    ] = "disabled"

    # 检索规则改变时更新版本，使缓存能够区分输入语义。
    retrieval_version: Literal["v1"] = "v1"

    # 未启用检索时为空；启用后记录实际配置的向量模型名称。
    embedding_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
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

    @model_validator(mode="after")
    def validate_historical_evidence_consistency(self) -> Self:
        """确保历史证据、检索状态和完整性标记一致。

        Returns:
            Self: 历史来源与检索上下文一致的证据包。

        Raises:
            ValueError: 历史内容与状态、来源或截断标记矛盾。
        """

        if self.retrieval_status != "available" and self.history:
            raise ValueError("只有检索正常时才能携带历史证据")

        if self.retrieval_status == "disabled":
            if self.embedding_model is not None:
                raise ValueError("未启用检索时不能声明Embedding模型")
        elif self.embedding_model is None:
            raise ValueError("启用检索时必须记录Embedding模型")

        history_ids = [item.evidence_id for item in self.history]

        if len(history_ids) != len(set(history_ids)):
            raise ValueError("历史证据编号不能重复")

        if any(item.event_id == self.event_id for item in self.history):
            raise ValueError("当前事件不能作为自己的历史证据")

        if not self.history_truncated and any(
            item.content_truncated for item in self.history
        ):
            raise ValueError("历史正文被截断时必须声明history_truncated")

        return self

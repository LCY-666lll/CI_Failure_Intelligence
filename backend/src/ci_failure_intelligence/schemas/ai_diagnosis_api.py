"""定义 AI 诊断 HTTP 接口的响应契约。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis


class AiDiagnosisResponseBase(BaseModel):
    """定义AI诊断HTTP响应共享的结果字段。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    event_id: int = Field(gt=0)

    diagnosis: AiDiagnosis | None = None
    model_name: str | None = None

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    lease_expires_at: datetime | None = None


class AiDiagnosisResponse(AiDiagnosisResponseBase):
    """描述一次付费触发请求的诊断结果。"""

    status: Literal[
        "generated",
        "cached",
        "in_progress",
    ]

    # POST只可能处理已经开始过的任务，因此至少为1。
    attempt_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_status_fields(self) -> Self:
        """确保触发响应字段与本次请求结果保持一致。

        Returns:
            Self: 字段组合符合触发结果语义的当前响应。

        Raises:
            ValueError: 完成状态缺少诊断，或处理中状态泄露诊断结果。
        """

        if self.status in {"generated", "cached"}:
            if self.diagnosis is None:
                raise ValueError("已完成的 AI 诊断响应必须包含 diagnosis")

            if self.lease_expires_at is not None:
                raise ValueError("已完成的 AI 诊断响应不能包含租约到期时间")

            return self

        if self.diagnosis is not None:
            raise ValueError("正在处理的 AI 诊断响应不能包含 diagnosis")

        if self.lease_expires_at is None:
            raise ValueError("正在处理的 AI 诊断响应必须包含租约到期时间")

        if any(
            value is not None
            for value in (
                self.model_name,
                self.input_tokens,
                self.output_tokens,
                self.total_tokens,
            )
        ):
            raise ValueError("正在处理的 AI 诊断响应不能包含模型调用结果")

        return self


class AiDiagnosisReadResponse(AiDiagnosisResponseBase):
    """描述数据库中当前保存的AI诊断状态。"""

    status: Literal[
        "not_started",
        "processing",
        "completed",
        "failed",
    ]

    # 尚未开始时为0；数据库已有任务时至少为1。
    attempt_count: int = Field(ge=0)

    # 只有failed状态返回安全、有限的错误摘要。
    error_code: str | None = None
    error_message: str | None = None

    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None

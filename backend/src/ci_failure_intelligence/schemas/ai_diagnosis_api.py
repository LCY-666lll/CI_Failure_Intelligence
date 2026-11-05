"""定义 AI 诊断 HTTP 接口的响应契约。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis


class AiDiagnosisResponse(BaseModel):
    """描述生成、缓存命中或正在处理的 AI 诊断结果。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    event_id: int = Field(gt=0)

    status: Literal[
        "generated",
        "cached",
        "in_progress",
    ]

    attempt_count: int = Field(ge=1)

    diagnosis: AiDiagnosis | None = None
    model_name: str | None = None

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    lease_expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_status_fields(self) -> Self:
        """确保响应字段与当前诊断状态保持一致。

        Returns:
            Self: 字段组合符合状态语义的当前响应。

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

"""定义 CI 事件当前 AI 诊断及其任务租约。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ci_failure_intelligence.infrastructure.persistence.base import Base


class CiFailureDiagnosisModel(Base):
    """保存一个 CI 事件当前的 AI 诊断和任务执行状态。"""

    __tablename__ = "ci_failure_diagnoses"

    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="status_supported",
        ),
        CheckConstraint(
            "char_length(prompt_version) >= 1",
            name="prompt_version_non_empty",
        ),
        CheckConstraint(
            "char_length(output_schema_version) >= 1",
            name="output_schema_version_non_empty",
        ),
        CheckConstraint(
            "char_length(requested_model) >= 1",
            name="requested_model_non_empty",
        ),
        CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="evidence_sha256_format",
        ),
        CheckConstraint(
            "model_config_sha256 ~ '^[0-9a-f]{64}$'",
            name="model_config_sha256_format",
        ),
        CheckConstraint(
            "attempt_count >= 1",
            name="attempt_count_positive",
        ),
        CheckConstraint(
            """
            input_tokens IS NULL
            OR input_tokens >= 0
            """,
            name="input_tokens_non_negative",
        ),
        CheckConstraint(
            """
            output_tokens IS NULL
            OR output_tokens >= 0
            """,
            name="output_tokens_non_negative",
        ),
        CheckConstraint(
            """
            total_tokens IS NULL
            OR total_tokens >= 0
            """,
            name="total_tokens_non_negative",
        ),
        CheckConstraint(
            """
            error_message IS NULL
            OR char_length(error_message) <= 2000
            """,
            name="error_message_length",
        ),
        CheckConstraint(
            """
            (
                status = 'processing'
                AND lease_token IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND diagnosis_json IS NULL
                AND model_name IS NULL
                AND input_tokens IS NULL
                AND output_tokens IS NULL
                AND total_tokens IS NULL
                AND error_code IS NULL
                AND error_message IS NULL
                AND finished_at IS NULL
            )
            OR
            (
                status = 'completed'
                AND lease_token IS NULL
                AND lease_expires_at IS NULL
                AND diagnosis_json IS NOT NULL
                AND error_code IS NULL
                AND error_message IS NULL
                AND finished_at IS NOT NULL
            )
            OR
            (
                status = 'failed'
                AND lease_token IS NULL
                AND lease_expires_at IS NULL
                AND diagnosis_json IS NULL
                AND model_name IS NULL
                AND input_tokens IS NULL
                AND output_tokens IS NULL
                AND total_tokens IS NULL
                AND error_code IS NOT NULL
                AND error_message IS NOT NULL
                AND finished_at IS NOT NULL
            )
            """,
            name="state_consistency",
        ),
    )

    # 一个事件只保留一份当前诊断，因此 event_id 同时作为表主键。
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "ci_failure_events.id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )

    status: Mapped[str] = mapped_column(String(20))

    prompt_version: Mapped[str] = mapped_column(String(50))
    output_schema_version: Mapped[str] = mapped_column(String(50))
    requested_model: Mapped[str] = mapped_column(String(100))

    # 两个摘要共同判断旧诊断是否仍可复用。
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    model_config_sha256: Mapped[str] = mapped_column(String(64))

    # processing 状态下标识当前唯一合法执行者。
    lease_token: Mapped[UUID | None] = mapped_column(
        Uuid,
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    attempt_count: Mapped[int] = mapped_column(
        Integer,
        server_default=text("1"),
    )

    # 只保存通过结构和证据引用校验的诊断，不保存模型原始响应。
    diagnosis_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # requested_model 是请求名称；model_name 是 provider 实际返回名称。
    model_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    input_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    output_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    total_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    error_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

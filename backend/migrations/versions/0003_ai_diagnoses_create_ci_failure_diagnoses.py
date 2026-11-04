"""创建 AI 诊断任务、租约和当前结果表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Alembic 使用这些标识构成迁移版本链。
revision: str = "0003_ai_diagnoses"
down_revision: str | Sequence[str] | None = "0002_junit_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建每个 CI 事件当前的 AI 诊断和租约状态。"""

    # event_id 同时作为主键和级联外键，使一个事件最多保存一份当前诊断。
    op.create_table(
        "ci_failure_diagnoses",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("output_schema_version", sa.String(length=50), nullable=False),
        sa.Column("requested_model", sa.String(length=100), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "diagnosis_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\n            (\n                status = 'processing'\n                AND lease_token IS NOT NULL\n                AND lease_expires_at IS NOT NULL\n                AND diagnosis_json IS NULL\n                AND model_name IS NULL\n                AND input_tokens IS NULL\n                AND output_tokens IS NULL\n                AND total_tokens IS NULL\n                AND error_code IS NULL\n                AND error_message IS NULL\n                AND finished_at IS NULL\n            )\n            OR\n            (\n                status = 'completed'\n                AND lease_token IS NULL\n                AND lease_expires_at IS NULL\n                AND diagnosis_json IS NOT NULL\n                AND model_name IS NOT NULL\n                AND error_code IS NULL\n                AND error_message IS NULL\n                AND finished_at IS NOT NULL\n            )\n            OR\n            (\n                status = 'failed'\n                AND lease_token IS NULL\n                AND lease_expires_at IS NULL\n                AND diagnosis_json IS NULL\n                AND model_name IS NULL\n                AND input_tokens IS NULL\n                AND output_tokens IS NULL\n                AND total_tokens IS NULL\n                AND error_code IS NOT NULL\n                AND error_message IS NOT NULL\n                AND finished_at IS NOT NULL\n            )\n            ",
            name=op.f("ck_ci_failure_diagnoses_state_consistency"),
        ),
        sa.CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_ci_failure_diagnoses_evidence_sha256_format"),
        ),
        sa.CheckConstraint(
            "model_config_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_ci_failure_diagnoses_model_config_sha256_format"),
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name=op.f("ck_ci_failure_diagnoses_status_supported"),
        ),
        sa.CheckConstraint(
            "\n            error_message IS NULL\n            OR char_length(error_message) <= 2000\n            ",
            name=op.f("ck_ci_failure_diagnoses_error_message_length"),
        ),
        sa.CheckConstraint(
            "\n            input_tokens IS NULL\n            OR input_tokens >= 0\n            ",
            name=op.f("ck_ci_failure_diagnoses_input_tokens_non_negative"),
        ),
        sa.CheckConstraint(
            "\n            output_tokens IS NULL\n            OR output_tokens >= 0\n            ",
            name=op.f("ck_ci_failure_diagnoses_output_tokens_non_negative"),
        ),
        sa.CheckConstraint(
            "\n            total_tokens IS NULL\n            OR total_tokens >= 0\n            ",
            name=op.f("ck_ci_failure_diagnoses_total_tokens_non_negative"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 1",
            name=op.f("ck_ci_failure_diagnoses_attempt_count_positive"),
        ),
        sa.CheckConstraint(
            "char_length(output_schema_version) >= 1",
            name=op.f("ck_ci_failure_diagnoses_output_schema_version_non_empty"),
        ),
        sa.CheckConstraint(
            "char_length(prompt_version) >= 1",
            name=op.f("ck_ci_failure_diagnoses_prompt_version_non_empty"),
        ),
        sa.CheckConstraint(
            "char_length(requested_model) >= 1",
            name=op.f("ck_ci_failure_diagnoses_requested_model_non_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["ci_failure_events.id"],
            name=op.f("fk_ci_failure_diagnoses_event_id_ci_failure_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_ci_failure_diagnoses")),
    )


def downgrade() -> None:
    """删除 AI 诊断表，回到只有确定性失败证据的版本。"""

    # 该操作会删除全部已保存诊断，产生正式数据后不能当作普通撤销手段。
    op.drop_table("ci_failure_diagnoses")

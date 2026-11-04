"""允许已完成诊断缺少供应商实际模型名。"""

from collections.abc import Sequence

from alembic import op

# Alembic 使用这些标识构成迁移版本链。
revision: str = "0004_nullable_model_name"
down_revision: str | Sequence[str] | None = "0003_ai_diagnoses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_STATE_CONSISTENCY = """
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
"""


OLD_STATE_CONSISTENCY = """
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
    AND model_name IS NOT NULL
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
"""


def upgrade() -> None:
    """允许保存未返回供应商实际模型名的有效诊断。"""

    # op.f 表示名称已经符合项目命名约定，避免再次添加表名前缀。
    constraint_name = op.f("ck_ci_failure_diagnoses_state_consistency")

    op.drop_constraint(
        constraint_name,
        "ci_failure_diagnoses",
        type_="check",
    )
    op.create_check_constraint(
        constraint_name,
        "ci_failure_diagnoses",
        NEW_STATE_CONSISTENCY,
    )


def downgrade() -> None:
    """恢复完成态必须具有供应商实际模型名的旧规则。"""

    constraint_name = op.f("ck_ci_failure_diagnoses_state_consistency")

    op.drop_constraint(
        constraint_name,
        "ci_failure_diagnoses",
        type_="check",
    )
    op.create_check_constraint(
        constraint_name,
        "ci_failure_diagnoses",
        OLD_STATE_CONSISTENCY,
    )

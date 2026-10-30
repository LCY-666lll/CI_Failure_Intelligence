"""增加 JUnit 解析状态和结构化失败明细表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic 使用这些标识构成迁移版本链。
revision: str = "0002_junit_details"
down_revision: str | Sequence[str] | None = "0001_ci_failure_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展原始事件状态，并创建 failure/error 明细表。"""

    # 现有事件和后续新事件都先进入 pending，等待独立处理入口解析。
    op.add_column(
        "ci_failure_events",
        sa.Column(
            "parse_status",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ci_failure_events",
        sa.Column("parse_error_code", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "ci_failure_events",
        sa.Column("parse_error_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "ci_failure_events",
        sa.Column(
            "parse_finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # 自动生成无法可靠补齐已有表上的新 CHECK，因此迁移中显式创建。
    op.create_check_constraint(
        op.f("ck_ci_failure_events_parse_error_message_length"),
        "ci_failure_events",
        """
        parse_error_message IS NULL
        OR char_length(parse_error_message) <= 2000
        """,
    )
    op.create_check_constraint(
        op.f("ck_ci_failure_events_parse_state_consistency"),
        "ci_failure_events",
        """
        (
            parse_status = 'pending'
            AND parse_error_code IS NULL
            AND parse_error_message IS NULL
            AND parse_finished_at IS NULL
        )
        OR
        (
            parse_status = 'parsed'
            AND parse_error_code IS NULL
            AND parse_error_message IS NULL
            AND parse_finished_at IS NOT NULL
        )
        OR
        (
            parse_status = 'failed'
            AND parse_error_code IS NOT NULL
            AND parse_error_message IS NOT NULL
            AND parse_finished_at IS NOT NULL
        )
        """,
    )

    op.create_table(
        "ci_failure_details",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("testcase_index", sa.Integer(), nullable=False),
        sa.Column("issue_index", sa.Integer(), nullable=False),
        sa.Column("suite_name", sa.Text(), nullable=True),
        sa.Column("classname", sa.Text(), nullable=True),
        sa.Column("test_name", sa.Text(), nullable=False),
        sa.Column("issue_type", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "issue_type IN ('failure', 'error')",
            name=op.f("ck_ci_failure_details_issue_type_supported"),
        ),
        sa.CheckConstraint(
            "char_length(test_name) >= 1",
            name=op.f("ck_ci_failure_details_test_name_non_empty"),
        ),
        sa.CheckConstraint(
            "issue_index >= 0",
            name=op.f("ck_ci_failure_details_issue_index_non_negative"),
        ),
        sa.CheckConstraint(
            "testcase_index >= 0",
            name=op.f("ck_ci_failure_details_testcase_index_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["ci_failure_events.id"],
            name=op.f("fk_ci_failure_details_event_id_ci_failure_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_ci_failure_details"),
        ),
        sa.UniqueConstraint(
            "event_id",
            "testcase_index",
            "issue_index",
            name="uq_ci_failure_details_event_position",
        ),
    )


def downgrade() -> None:
    """删除解析明细和状态字段，回到原始事件表版本。"""

    # 该操作会删除全部结构化解析结果，不能作为有数据环境的普通撤销方式。
    op.drop_table("ci_failure_details")
    op.drop_constraint(
        op.f("ck_ci_failure_events_parse_state_consistency"),
        "ci_failure_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ci_failure_events_parse_error_message_length"),
        "ci_failure_events",
        type_="check",
    )
    op.drop_column("ci_failure_events", "parse_finished_at")
    op.drop_column("ci_failure_events", "parse_error_message")
    op.drop_column("ci_failure_events", "parse_error_code")
    op.drop_column("ci_failure_events", "parse_status")

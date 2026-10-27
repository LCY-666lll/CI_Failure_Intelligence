"""创建保存原始 CI 失败事件的首张业务表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic 使用这些标识构成迁移版本链。
revision: str = "0001_ci_failure_events"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 CI 失败事件表及其数据库完整性约束。"""

    # 迁移保存的是已经人工审查的历史结构，不在这里动态读取 ORM 模型。
    op.create_table(
        "ci_failure_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("github_repository_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_full_name", sa.String(length=200), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("run_attempt", sa.Integer(), nullable=False),
        sa.Column("job_key", sa.String(length=255), nullable=False),
        sa.Column("workflow_name", sa.String(length=255), nullable=False),
        sa.Column("job_name", sa.String(length=255), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("head_branch", sa.String(length=255), nullable=False),
        sa.Column("junit_xml", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "commit_sha ~ '^[0-9A-Fa-f]{40}$'",
            name=op.f("ck_ci_failure_events_commit_sha_format"),
        ),
        sa.CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_ci_failure_events_payload_sha256_format"),
        ),
        sa.CheckConstraint(
            "char_length(junit_xml) BETWEEN 1 AND 2000000",
            name=op.f("ck_ci_failure_events_junit_xml_length"),
        ),
        sa.CheckConstraint(
            "github_repository_id > 0",
            name=op.f("ck_ci_failure_events_repository_id_positive"),
        ),
        sa.CheckConstraint(
            "run_attempt >= 1", name=op.f("ck_ci_failure_events_run_attempt_positive")
        ),
        sa.CheckConstraint(
            "run_id > 0", name=op.f("ck_ci_failure_events_run_id_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ci_failure_events")),
        sa.UniqueConstraint(
            "github_repository_id",
            "run_id",
            "run_attempt",
            "job_key",
            name="uq_ci_failure_events_business_identity",
        ),
    )


def downgrade() -> None:
    """删除 CI 失败事件表，回到首份迁移之前的状态。"""

    # 该操作会连同表中数据一起删除，只能在明确允许丢弃数据时执行。
    op.drop_table("ci_failure_events")

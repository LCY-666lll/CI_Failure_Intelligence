"""验证 CI 失败原始事件表的数据库契约。"""

from typing import cast

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from sqlalchemy import CheckConstraint, Table, UniqueConstraint


def get_event_table() -> Table:
    """将 SQLAlchemy 通用 FromClause 类型收窄为当前模型的 Table。"""

    return cast(Table, CiFailureEventModel.__table__)


def test_ci_failure_event_table_has_required_columns() -> None:
    """事件表应完整保存原始 CI 业务事实，且所有字段均为必填。"""

    table = get_event_table()

    assert table.name == "ci_failure_events"
    assert set(table.columns.keys()) == {
        "id",
        "github_repository_id",
        "repository_full_name",
        "run_id",
        "run_attempt",
        "workflow_name",
        "job_key",
        "job_name",
        "commit_sha",
        "head_branch",
        "junit_xml",
        "payload_sha256",
        "received_at",
        "parse_status",
        "parse_error_code",
        "parse_error_message",
        "parse_finished_at",
    }
    assert table.c.id.primary_key is True
    assert table.c.received_at.server_default is not None
    assert table.c.parse_status.server_default is not None
    assert table.c.parse_status.nullable is False
    assert table.c.parse_error_code.nullable is True
    assert table.c.parse_error_message.nullable is True
    assert table.c.parse_finished_at.nullable is True


def test_ci_failure_event_table_has_business_identity_constraint() -> None:
    """数据库应阻止同一 CI 业务身份被并发插入多行。"""

    constraints = get_event_table().constraints
    unique_constraint = next(
        constraint
        for constraint in constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_ci_failure_events_business_identity"
    )

    assert [column.name for column in unique_constraint.columns] == [
        "github_repository_id",
        "run_id",
        "run_attempt",
        "job_key",
    ]


def test_ci_failure_event_table_has_named_check_constraints() -> None:
    """关键字段应由数据库约束提供最后一道完整性保护。"""

    check_names = {
        constraint.name
        for constraint in get_event_table().constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert check_names == {
        "ck_ci_failure_events_repository_id_positive",
        "ck_ci_failure_events_run_id_positive",
        "ck_ci_failure_events_run_attempt_positive",
        "ck_ci_failure_events_junit_xml_length",
        "ck_ci_failure_events_commit_sha_format",
        "ck_ci_failure_events_payload_sha256_format",
        "ck_ci_failure_events_parse_error_message_length",
        "ck_ci_failure_events_parse_state_consistency",
    }

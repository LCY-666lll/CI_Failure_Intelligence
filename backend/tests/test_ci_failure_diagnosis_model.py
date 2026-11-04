"""验证 AI 诊断任务表的数据库契约。"""

from typing import cast

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_diagnosis import (
    CiFailureDiagnosisModel,
)
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Table, Uuid
from sqlalchemy.dialects.postgresql import JSONB


def get_diagnosis_table() -> Table:
    """将 SQLAlchemy 通用 FromClause 类型收窄为当前模型的 Table。"""

    return cast(Table, CiFailureDiagnosisModel.__table__)


def test_diagnosis_table_has_required_columns() -> None:
    """诊断表应保存缓存身份、租约、结果、费用元数据和状态时间。"""

    table = get_diagnosis_table()

    assert table.name == "ci_failure_diagnoses"
    assert set(table.columns.keys()) == {
        "event_id",
        "status",
        "prompt_version",
        "output_schema_version",
        "requested_model",
        "evidence_sha256",
        "model_config_sha256",
        "lease_token",
        "lease_expires_at",
        "attempt_count",
        "diagnosis_json",
        "model_name",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    }
    assert table.c.event_id.primary_key is True
    assert [column.name for column in table.primary_key.columns] == ["event_id"]


def test_diagnosis_table_uses_uuid_lease_and_postgresql_jsonb() -> None:
    """租约应使用 UUID，已校验诊断应使用 PostgreSQL JSONB。"""

    table = get_diagnosis_table()

    assert isinstance(table.c.lease_token.type, Uuid)
    assert isinstance(table.c.diagnosis_json.type, JSONB)
    assert table.c.lease_token.nullable is True
    assert table.c.diagnosis_json.nullable is True


def test_diagnosis_table_references_event_with_cascade_delete() -> None:
    """删除原始事件时应由数据库同步删除其当前 AI 诊断。"""

    foreign_key = next(
        constraint
        for constraint in get_diagnosis_table().constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )

    assert foreign_key.name == ("fk_ci_failure_diagnoses_event_id_ci_failure_events")
    assert foreign_key.ondelete == "CASCADE"
    assert [element.target_fullname for element in foreign_key.elements] == [
        "ci_failure_events.id"
    ]


def test_diagnosis_table_has_required_server_defaults() -> None:
    """首次抢占次数和任务时间应具有数据库默认值。"""

    table = get_diagnosis_table()

    assert table.c.attempt_count.server_default is not None
    assert table.c.started_at.server_default is not None
    assert table.c.created_at.server_default is not None
    assert table.c.updated_at.server_default is not None
    assert table.c.finished_at.server_default is None


def test_diagnosis_table_has_named_check_constraints() -> None:
    """数据库应约束任务状态、缓存身份、token 和错误摘要。"""

    check_names = {
        constraint.name
        for constraint in get_diagnosis_table().constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert check_names == {
        "ck_ci_failure_diagnoses_status_supported",
        "ck_ci_failure_diagnoses_prompt_version_non_empty",
        "ck_ci_failure_diagnoses_output_schema_version_non_empty",
        "ck_ci_failure_diagnoses_requested_model_non_empty",
        "ck_ci_failure_diagnoses_evidence_sha256_format",
        "ck_ci_failure_diagnoses_model_config_sha256_format",
        "ck_ci_failure_diagnoses_attempt_count_positive",
        "ck_ci_failure_diagnoses_input_tokens_non_negative",
        "ck_ci_failure_diagnoses_output_tokens_non_negative",
        "ck_ci_failure_diagnoses_total_tokens_non_negative",
        "ck_ci_failure_diagnoses_error_message_length",
        "ck_ci_failure_diagnoses_state_consistency",
    }

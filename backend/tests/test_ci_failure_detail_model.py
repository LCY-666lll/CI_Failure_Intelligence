"""验证结构化 CI 失败明细表的数据库契约。"""

from typing import cast

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_detail import (
    CiFailureDetailModel,
)
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Table, UniqueConstraint


def get_detail_table() -> Table:
    """将 SQLAlchemy 通用 FromClause 类型收窄为当前模型的 Table。"""
    return cast(Table, CiFailureDetailModel.__table__)


def test_ci_failure_detail_table_has_required_columns() -> None:
    """一行明细应完整保存一个 failure/error 节点及其 XML 位置。"""
    table = get_detail_table()

    assert table.name == "ci_failure_details"
    assert set(table.columns.keys()) == {
        "id",
        "event_id",
        "testcase_index",
        "issue_index",
        "suite_name",
        "classname",
        "test_name",
        "issue_type",
        "message",
        "details",
    }
    assert table.c.id.primary_key is True
    assert table.c.suite_name.nullable is True
    assert table.c.classname.nullable is True
    assert table.c.message.nullable is True
    assert table.c.event_id.nullable is False
    assert table.c.test_name.nullable is False
    assert table.c.details.nullable is False


def test_ci_failure_detail_references_event_with_cascade_delete() -> None:
    """删除原始事件时应由数据库同步删除其派生失败明细。"""
    foreign_key = next(
        constraint
        for constraint in get_detail_table().constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )

    assert foreign_key.name == ("fk_ci_failure_details_event_id_ci_failure_events")
    assert foreign_key.ondelete == "CASCADE"
    assert [element.target_fullname for element in foreign_key.elements] == [
        "ci_failure_events.id"
    ]


def test_ci_failure_detail_has_unique_xml_position() -> None:
    """同一原始事件中的同一错误位置只能保存一条明细。"""
    unique_constraint = next(
        constraint
        for constraint in get_detail_table().constraints
        if isinstance(constraint, UniqueConstraint)
    )

    assert unique_constraint.name == "uq_ci_failure_details_event_position"
    assert [column.name for column in unique_constraint.columns] == [
        "event_id",
        "testcase_index",
        "issue_index",
    ]


def test_ci_failure_detail_has_named_check_constraints() -> None:
    """数据库应拒绝非法位置、节点类型和空测试名。"""
    check_names = {
        constraint.name
        for constraint in get_detail_table().constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert check_names == {
        "ck_ci_failure_details_testcase_index_non_negative",
        "ck_ci_failure_details_issue_index_non_negative",
        "ck_ci_failure_details_issue_type_supported",
        "ck_ci_failure_details_test_name_non_empty",
    }

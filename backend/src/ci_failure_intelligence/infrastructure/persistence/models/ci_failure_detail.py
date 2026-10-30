"""定义从 JUnit XML 提取的结构化失败明细模型。"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ci_failure_intelligence.infrastructure.persistence.base import Base


class CiFailureDetailModel(Base):
    """保存一个 failure 或 error XML 节点的确定性证据。"""

    __tablename__ = "ci_failure_details"

    __table_args__ = (
        # 两个位置序号都从 0 开始，数据库拒绝负数。
        CheckConstraint(
            "testcase_index >= 0",
            name="testcase_index_non_negative",
        ),
        CheckConstraint(
            "issue_index >= 0",
            name="issue_index_non_negative",
        ),
        # 当前结构化明细只保存真正的失败和错误节点。
        CheckConstraint(
            "issue_type IN ('failure', 'error')",
            name="issue_type_supported",
        ),
        # 测试名称是定位失败的最低身份信息，不允许空字符串。
        CheckConstraint(
            "char_length(test_name) >= 1",
            name="test_name_non_empty",
        ),
        # 同一事件中的同一个 XML 位置只能保存一条明细。
        UniqueConstraint(
            "event_id",
            "testcase_index",
            "issue_index",
            name="uq_ci_failure_details_event_position",
        ),
    )

    # PostgreSQL 生成内部明细 ID；重新解析整体替换时 ID 可以变化。
    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )

    # 当前明细表的 event_id，必须引用原始事件表中的一个有效 ID。
    # 删除原事件时，由 PostgreSQL 级联删除对应派生明细。
    event_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "ci_failure_events.id",
            # 如果 PostgreSQL 删除事件 xx，它会自动删除所属明细。
            ondelete="CASCADE",
        ),
    )

    # testcase_index 统计原 XML 中所有 testcase，包括 passed 和 skipped。
    testcase_index: Mapped[int] = mapped_column(Integer)

    # issue_index 只统计当前 testcase 的直接 failure/error 子节点。
    issue_index: Mapped[int] = mapped_column(Integer)

    # 参数化测试和嵌套 suite 名可能较长，因此使用 text，不人为截断。
    suite_name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    classname: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    test_name: Mapped[str] = mapped_column(Text)

    # 数据库 CHECK 约束保证这里只能保存 failure 或 error。
    issue_type: Mapped[str] = mapped_column(String(20))

    message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # 保存节点内完整错误文本和 traceback；空文本保存为空字符串。
    details: Mapped[str] = mapped_column(Text)

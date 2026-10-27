"""定义未经解析的 CI 失败事件持久化模型。"""

from datetime import datetime

# SQLAlchemy 数据库列类型、数据库函数和表级约束。
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)

# Mapped 描述 ORM 属性的 Python 类型；
# mapped_column 描述该属性对应的数据库列。
from sqlalchemy.orm import Mapped, mapped_column

from ci_failure_intelligence.infrastructure.persistence.base import Base


class CiFailureEventModel(Base):
    """保存一个通过校验的 CI 失败业务事件及 JUnit XML 原文。"""

    # 这个模型未来对应 PostgreSQL 中的 ci_failure_events 表。
    __tablename__ = "ci_failure_events"

    # 表级规则必须放在 tuple 中；最后的逗号表示这是单元素 tuple。
    __table_args__ = (
        CheckConstraint(
            "github_repository_id > 0",
            name="repository_id_positive",
        ),
        # GitHub Actions run ID 必须是正整数。
        CheckConstraint(
            "run_id > 0",
            name="run_id_positive",
        ),
        # GitHub Actions 第一次运行的 run_attempt 从 1 开始。
        CheckConstraint(
            "run_attempt >= 1",
            name="run_attempt_positive",
        ),
        # JUnit XML 必须非空，并保持与请求契约相同的字符上限。
        CheckConstraint(
            "char_length(junit_xml) BETWEEN 1 AND 2000000",
            name="junit_xml_length",
        ),
        # 当前请求契约要求 Git commit SHA 为 40 位十六进制文本。
        CheckConstraint(
            "commit_sha ~ '^[0-9A-Fa-f]{40}$'",
            name="commit_sha_format",
        ),
        # 业务内容摘要固定为 64 位小写 SHA-256 十六进制文本。
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="payload_sha256_format",
        ),
        # 这四个字段共同构成一个 CI 业务事件的稳定身份。
        UniqueConstraint(
            "github_repository_id",
            "run_id",
            "run_attempt",
            "job_key",
            name="uq_ci_failure_events_business_identity",
        ),
    )

    # PostgreSQL 自动生成内部主键，应用插入时不负责计算 id。
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # GitHub 的稳定仓库数字 ID，数据库使用 bigint 保存。
    github_repository_id: Mapped[int] = mapped_column(BigInteger)

    # 仓库全名用于展示，例如 LCY-666lll/CI_Failure_Intelligence。
    repository_full_name: Mapped[str] = mapped_column(
        String(200),
    )

    # GitHub Actions workflow run 的稳定数字 ID。
    run_id: Mapped[int] = mapped_column(BigInteger)

    # 同一个 run 被重新运行的次数，从 1 开始。
    run_attempt: Mapped[int] = mapped_column(Integer)

    # GitHub Actions Job 的稳定业务键，用于区分不同矩阵实例。
    job_key: Mapped[str] = mapped_column(
        String(255),
    )

    # workflow 显示名称，例如 Backend CI。
    workflow_name: Mapped[str] = mapped_column(
        String(255),
    )

    # Job 显示名称，例如 Backend tests (Python 3.12)。
    job_name: Mapped[str] = mapped_column(
        String(255),
    )

    # 实际被测试代码版本的 40 位 Git commit SHA:表明测试的是哪版代码
    commit_sha: Mapped[str] = mapped_column(
        String(40),
    )

    # 触发本次运行的分支名称。
    head_branch: Mapped[str] = mapped_column(
        String(255),
    )

    # 保存未经解析的 JUnit XML 原文，作为后续解析和诊断的原始证据。
    junit_xml: Mapped[str] = mapped_column(Text)

    # 保存稳定业务内容的 SHA-256 摘要，用于识别重复身份是否携带相同内容。
    payload_sha256: Mapped[str] = mapped_column(
        String(64),
    )

    # 由 PostgreSQL 填写首次入库时间，应用插入时不主动传值。
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

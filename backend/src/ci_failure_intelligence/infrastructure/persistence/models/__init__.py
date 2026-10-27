"""集中导入 ORM 模型，使其表注册到共享 MetaData。"""

# 导入模型类时，SQLAlchemy 会执行类定义并将表注册到 Base.metadata。
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)

# 明确声明 models 包对外公开的 ORM 模型。
__all__ = ["CiFailureEventModel"]

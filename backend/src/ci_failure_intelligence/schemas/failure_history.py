"""定义经过数据库核对的历史失败结果。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
)


class HistoricalFailureResponse(BaseModel):
    """一条真实已回数据库核对的历史错误及运行来源。"""

    model_config = ConfigDict(
        strict=True,
        extra = "forbid"
    )

    event_id: int = Field(gt=0)
    repository_full_name: str
    run_id: int = Field(gt=0)
    run_attempt: int = Field(ge=1)
    job_key: str
    received_at: datetime

    # 分数只用于表达检索相似程度，禁止无穷大和NaN。
    similarity_score: float = Field(
        ge=0,
        allow_inf_nan=False,
    )

    # 复用现有明细模型，保留测试名称、错误消息和完整堆栈。
    failure: CiFailureDetailResponse

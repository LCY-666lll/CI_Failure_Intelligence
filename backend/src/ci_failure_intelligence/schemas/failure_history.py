"""定义经过数据库核对的历史失败结果。"""

from datetime import datetime
from typing import Literal

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

class CiFailureHistoryResponse(BaseModel):
    """描述当前指定 CI 事件的历史检索状态及核实后的可有多条的历史错误。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    event_id: int = Field(gt=0)

    # 当前报告的解析状态，与历史检索是否成功是两件事。
    parse_status: Literal["pending", "parsed", "failed"]

    # 空历史列表必须配合这个状态解释，不能一律理解为没有历史。
    retrieval_status: Literal[
        "not_ready",
        "available",
        "degraded",
        "disabled",
    ]

    # 当前最多选前20条失败参与检索；超过时明确告诉前端。
    current_failures_truncated: bool

    # 复用现有历史明细结构，不重新定义来源和错误字段。
    history: list[HistoricalFailureResponse] = Field(max_length=5)

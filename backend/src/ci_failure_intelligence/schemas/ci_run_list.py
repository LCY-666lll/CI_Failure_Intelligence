"""定义最近 CI 事件列表接口的响应契约。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CiRunListItem(BaseModel):
    """描述最近事件列表中的一条 CI 事件摘要。"""

    # strict 禁止随意类型转换；extra=forbid 防止未声明字段进入响应。
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    event_id: int = Field(gt=0)
    repository_full_name: str = Field(min_length=3, max_length=200)

    run_id: int = Field(gt=0)
    run_attempt: int = Field(ge=1)
    job_key: str = Field(min_length=1, max_length=255)
    workflow_name: str = Field(min_length=1, max_length=255)
    job_name: str = Field(min_length=1, max_length=255)
    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    head_branch: str = Field(min_length=1, max_length=255)

    # 原始事件首次写入 PostgreSQL 的时间
    received_at: datetime

    # 状态与 failure_count 必须结合理解，不能只看失败数量。
    parse_status: Literal["pending", "parsed", "failed"]

    # 已经结构化保存的 failure/error 节点数量
    failure_count: int = Field(ge=0)


class CiRunListResponse(BaseModel):
    """描述最近 CI 事件列表的完整 HTTP 响应。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    # 数据库没有事件时仍返回合法空列表，而不是 404。
    items: list[CiRunListItem]

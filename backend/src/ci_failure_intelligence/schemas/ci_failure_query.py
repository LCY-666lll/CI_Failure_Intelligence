"""定义 CI 失败详情查询接口的响应契约。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CiFailureDetailResponse(BaseModel):
    """描述从一个 JUnit failure 或 error 节点提取的失败证据。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: int = Field(gt=0)

    # 两个下标共同表示明细在所属事件原始 XML 中的位置。
    testcase_index: int = Field(ge=0)
    issue_index: int = Field(ge=0)

    suite_name: str | None
    classname: str | None
    test_name: str = Field(min_length=1)
    issue_type: Literal["failure", "error"]
    message: str | None
    details: str


class CiFailureQueryResponse(BaseModel):
    """描述一个 CI 事件的解析状态和全部失败明细。"""

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

    # 状态必须与 failures 一起理解：
    # pending 表示尚未解析，parsed 表示解析完成，failed 表示解析失败。
    parse_status: Literal["pending", "parsed", "failed"]
    parse_error_code: str | None
    parse_error_message: str | None
    parse_finished_at: datetime | None

    # 该字段始终存在；空列表的业务含义由 parse_status 决定。
    failures: list[CiFailureDetailResponse]

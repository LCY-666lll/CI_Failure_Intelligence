"""定义 CI 失败接入接口使用的数据契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# PostgreSQL bigint 和 integer 的最大存储范围。
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
POSTGRES_INTEGER_MAX = 2_147_483_647

MAX_JUNIT_XML_CHARACTERS = 2_000_000


class RepositoryIdentity(BaseModel):
    """描述产生本次 CI 运行的 GitHub 仓库。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    github_repository_id: int = Field(
        gt=0,
        le=POSTGRES_BIGINT_MAX,
    )
    full_name: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[^/\s]+/[^/\s]+$",
    )


class WorkflowRunContext(BaseModel):
    """描述本次失败所属的 GitHub Actions 运行和 Job。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    run_id: int = Field(
        gt=0,
        le=POSTGRES_BIGINT_MAX,
    )
    run_attempt: int = Field(
        ge=1,
        le=POSTGRES_INTEGER_MAX,
    )
    workflow_name: str = Field(min_length=1, max_length=255)
    job_key: str = Field(min_length=1, max_length=255)
    job_name: str = Field(min_length=1, max_length=255)
    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    head_branch: str = Field(min_length=1, max_length=255)


class CiFailureIngestRequest(BaseModel):
    """描述一次完整的 CI 失败接入请求。"""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )
    # 嵌套字段
    repository: RepositoryIdentity
    run: WorkflowRunContext
    junit_xml: str = Field(
        min_length=1,
        max_length=MAX_JUNIT_XML_CHARACTERS,
    )


class CiFailureIngestResponse(BaseModel):
    """描述已经可靠保存的 CI 失败接入响应。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    # accepted 表示原始 CI 事件已经提交到 PostgreSQL，
    # 但不表示 JUnit 已解析或诊断任务已经完成。
    status: Literal["accepted"] = "accepted"
    run_id: int = Field(gt=0)
    run_attempt: int = Field(ge=1)
    job_key: str = Field(min_length=1, max_length=255)

"""提供 CI 失败数据的 HTTP 接入路由。"""

from fastapi import APIRouter,status

from ci_failure_intelligence.schemas.ci_ingest import(
    CiFailureIngestRequest,
    CiFailureIngestResponse,
)


router = APIRouter(
    prefix="/api/v1/ci-runs",
    tags=["ci-runs"]
)

@router.post(
    "/ingest",
    # 让 FastAPI按响应契约检查和序列化结果
    response_model=CiFailureIngestResponse,
    status_code=status.HTTP_200_OK,
)
async def ingest_ci_failure(ingest_request:CiFailureIngestRequest):
    """验证一次 CI 失败接入请求并回传其运行身份。

    Args:
        ingest_request: 已通过严格类型和字段约束校验的 CI 失败接入请求。

    Returns:
        CiFailureIngestResponse: 包含校验状态、运行编号、执行次数和
        Job 稳定键的响应。`validated` 只表示请求契约校验通过，
        不表示已经持久化或排队。
    """

    return CiFailureIngestResponse(
        run_id=ingest_request.run.run_id,
        run_attempt=ingest_request.run.run_attempt,
        job_key=ingest_request.run.job_key,
    )
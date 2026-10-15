"""创建并配置 CI Failure Intelligence 的 FastAPI 应用。"""

from fastapi import FastAPI, status
from ci_failure_intelligence.api.routes import ci_ingest

app = FastAPI(
    title="CI Failure Intelligence API",
    version="0.1.0",
)

app.include_router(ci_ingest.router)

@app.get(
    "/health/live",
    status_code=status.HTTP_200_OK,
    tags=["health"]
)
async def live_health_check():
    """返回 API 进程的基础存活状态。

    Returns:
        dict[str, str]: 固定包含 `status=ok` 的存活状态。
    """

    return {"status": "ok"}


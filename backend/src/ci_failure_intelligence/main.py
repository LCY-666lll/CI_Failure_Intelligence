"""创建并配置 CI Failure Intelligence 的 FastAPI 应用。"""

from fastapi import FastAPI, status

app = FastAPI(
    title="CI Failure Intelligence API",
    version="0.1.0",
)

@app.get(
    "/health/live",
    status_code=status.HTTP_200_OK,
    tags=["health"]
)
async def live_health_check():
    """提供一个轻量的进程存活接口,检查 API 进程是否能够正常处理 HTTP 请求。"""

    return {"status": "ok"}


"""提供 API 进程的存活检查路由。"""

from fastapi import APIRouter, status

router = APIRouter(
    prefix="/health",
    tags=["health"],
)


@router.get(
    "/live",
    status_code=status.HTTP_200_OK,
)
async def live_health_check():
    """返回 API 进程的基础存活状态。

    Returns:
        dict[str, str]: 固定包含 `status=ok` 的存活状态。
    """

    return {"status": "ok"}

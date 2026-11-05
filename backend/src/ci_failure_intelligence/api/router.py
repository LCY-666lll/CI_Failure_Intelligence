"""汇总 CI Failure Intelligence 的 HTTP 路由。"""

from fastapi import APIRouter

from ci_failure_intelligence.api.routes import (
    ai_diagnosis,
    ci_failure_query,
    ci_ingest,
    health,
)

# 这个路由器不拥有统一前缀，只负责组合各模块路由。
api_router = APIRouter()

# 将健康检查模块已经登记的路由复制到顶层路由器。
api_router.include_router(health.router)

# 将 CI 失败接入模块已经登记的路由复制到顶层路由器。
api_router.include_router(ci_ingest.router)

# 将受读取令牌保护的 CI 失败详情查询路由复制到顶层路由器。
api_router.include_router(ci_failure_query.router)

# 将受独立付费触发令牌保护的 AI 诊断路由注册到应用。
api_router.include_router(ai_diagnosis.router)

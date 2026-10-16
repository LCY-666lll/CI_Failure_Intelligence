"""根据运行配置创建 CI Failure Intelligence 应用。"""

from fastapi import FastAPI

from ci_failure_intelligence.api.router import api_router
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.middleware.ci_ingest_security import CiIngestSecurityMiddleware


def create_app(
    # 星号表示后面的参数必须使用 settings=... 的名字传入，
    # 避免调用者只传一个位置参数却看不出这个对象代表什么。
    *,
    settings: Settings,
) -> FastAPI:
    """使用已经校验的运行配置创建独立 FastAPI 应用。

    Args:
        settings: 已完成环境读取和字段校验的后端运行配置。
            函数不会在内部重新读取 `.env`。

    Returns:
        FastAPI: 已保存当前配置并完成路由注册的应用实例。
    """

    # 每次调用都创建新的应用实例，因此不同测试可以使用不同配置。
    app = FastAPI(
        title="CI Failure Intelligence API",
        version="0.1.0",
    )

    # 当前环境的真实密钥字节
    # SecretStr 只负责避免密钥在日志和调试输出中被直接显示。
    ingest_hmac_secret = (
        settings.ingest_hmac_secret.get_secret_value().encode("utf-8")
    )

    # 传递中间件类：add_middleware(self, middleware_class, *args, **kwargs)
    app.add_middleware(
        CiIngestSecurityMiddleware,
        secret=ingest_hmac_secret,
        max_body_bytes=settings.ingest_max_body_bytes,
        timestamp_tolerance_seconds=settings.ingest_timestamp_tolerance_seconds
    )

    # state用于保存属于当前FastAPI实例的共享对象。
    # 这里保存的是SecretStr等已经校验的配置，不会自动形成HTTP响应。
    # app.state → 属于某一个FastAPI实例 → 每次create_app()可以拥有不同配置
    app.state.settings = settings

    # 把已经汇总的健康检查和CI接入路由注册到当前应用实例。
    app.include_router(api_router)

    # 调用者需要取得这个实例，交给Uvicorn或TestClient使用。
    return app

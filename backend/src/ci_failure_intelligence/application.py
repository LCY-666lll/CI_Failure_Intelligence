"""根据运行配置创建 CI Failure Intelligence 应用。"""

# 给下面带 yield 的异步 lifespan() 写返回类型
from collections.abc import AsyncGenerator
# 把“yield 之前做启动工作、之后做关闭工作”的异步函数变成上下文管理器
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ci_failure_intelligence.api.router import api_router
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.infrastructure.redis_client import (
    create_redis_client,
)
from ci_failure_intelligence.infrastructure.redis_nonce_store import (
    RedisNonceStore,
)
from ci_failure_intelligence.middleware.ci_ingest_security import (
    CiIngestSecurityMiddleware,
)
from ci_failure_intelligence.security.nonce_store import NonceStore

def create_app(
    # 星号表示后面的参数必须使用 settings=... 的名字传入，
    # 避免调用者只传一个位置参数却看不出这个对象代表什么。
    *,
    settings: Settings,
    nonce_store:NonceStore | None = None,
) -> FastAPI:
    """使用已经校验的运行配置创建独立 FastAPI 应用。

    Args:
        settings: 已完成环境读取和字段校验的后端运行配置。
            函数不会在内部重新读取 `.env`。
        nonce_store: 可选的 nonce 存储实现。
            未提供时创建真实 Redis 客户端和 RedisNonceStore；
            自动化测试可以注入假存储，避免访问本地或生产 Redis。

    Returns:
        FastAPI: 已保存当前配置并完成路由注册的应用实例。
    """

    managed_redis_client = None

    if nonce_store is None:
        # 创建整个 FastAPI 进程共享的异步客户端和连接池。
        # 此时只创建 Python 对象，第一次 Redis 命令才建立网络连接。
        managed_redis_client = create_redis_client(settings)
        # RedisNonceStore 与其他未来 Redis 组件共享上面的客户端实例。
        resolved_nonce_store: NonceStore = RedisNonceStore(
            redis_client=managed_redis_client,
            nonce_ttl_seconds=settings.ingest_nonce_ttl_seconds,
        )
    else:
        # 测试或其他调用方可以提供符合 NonceStore 协议的实现。
        resolved_nonce_store = nonce_store

    # 进入 async with
    # → 执行到 yield
    # → FastAPI 开始服务
    # → 处理0到很多个请求
    # → 应用关闭
    # → 从 yield 后继续
    # → finally 执行 aclose()
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        """在应用退出时释放由应用工厂创建的 Redis 客户端。

        Yields:
            None: `yield` 期间 FastAPI 可以持续处理 HTTP 请求。
        """

        try:
            # 先暂停这个函数，把程序控制权交给 FastAPI；等 FastAPI 要关闭时，再回来继续执行。
            # Redis 客户端采用按需连接，因此这里不执行强制 PING。
            yield
        finally:
            # yield 之后属于应用关闭阶段。
            # 只有真实运行路径创建了客户端，测试注入路径不会进入此分支。
            if managed_redis_client is not None:
                await managed_redis_client.aclose()       
    

    # 每次调用都创建新的应用实例，因此不同测试可以使用不同配置。
    app = FastAPI(
        title="CI Failure Intelligence API",
        version="0.1.0",
        lifespan=lifespan
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
        timestamp_tolerance_seconds=settings.ingest_timestamp_tolerance_seconds,
        nonce_store=resolved_nonce_store,
    )

    # state用于保存属于当前FastAPI实例的共享对象。
    # 这里保存的是SecretStr等已经校验的配置，不会自动形成HTTP响应。
    # app.state → 属于某一个FastAPI实例 → 每次create_app()可以拥有不同配置
    app.state.settings = settings
    app.state.nonce_store = resolved_nonce_store

    # 把已经汇总的健康检查和CI接入路由注册到当前应用实例。
    app.include_router(api_router)

    # 调用者需要取得这个实例，交给Uvicorn或TestClient使用。
    return app

"""根据运行配置创建 CI Failure Intelligence 应用。"""

import asyncio

# 给下面带 yield 的异步 lifespan() 写返回类型
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial

from fastapi import FastAPI
from fastembed import TextEmbedding
from langchain_deepseek import ChatDeepSeek
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from ci_failure_intelligence.api.exception_handlers import (
    register_exception_handlers,
)
from ci_failure_intelligence.api.router import api_router
from ci_failure_intelligence.core.config import RetrievalSettings, Settings
from ci_failure_intelligence.infrastructure.ai.deepseek_client import (
    create_deepseek_client,
)
from ci_failure_intelligence.infrastructure.ai.embedding_model import (
    create_embedding_model,
    run_embedding_in_thread,
)
from ci_failure_intelligence.infrastructure.database import (
    create_database_engine,
    create_database_session_factory,
)
from ci_failure_intelligence.infrastructure.qdrant_client import create_qdrant_client
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
from ci_failure_intelligence.service.ai_diagnosis_orchestration import (
    AiDiagnosisGraphContext,
    build_ai_diagnosis_graph,
)
from ci_failure_intelligence.service.ci_processing import run_ci_processing


def create_app(
    # 星号表示后面的参数必须使用 settings=... 的名字传入，
    # 避免调用者只传一个位置参数却看不出这个对象代表什么。
    *,
    settings: Settings,
    nonce_store: NonceStore | None = None,
    database_engine: AsyncEngine | None = None,
    deepseek_client: ChatDeepSeek | None = None,
    retrieval_settings: RetrievalSettings | None = None,
    qdrant_client: AsyncQdrantClient | None = None,
    embedding_model: TextEmbedding | None = None,
) -> FastAPI:
    """使用已经校验的运行配置创建独立 FastAPI 应用。

    Args:
        settings: 已完成环境读取和字段校验的后端运行配置。
            函数不会在内部重新读取 `.env`。
        nonce_store: 可选的 nonce 存储实现。
            未提供时创建真实 Redis 客户端和 RedisNonceStore；
            自动化测试可以注入假存储，避免访问本地或生产 Redis。
        database_engine: 可选的异步数据库 Engine。
            未提供时根据 Settings 创建并由应用生命周期释放；
            自动化测试可以注入自己的 Engine。
        deepseek_client: 可选的 DeepSeek LangChain 客户端。
            未提供时根据 Settings 创建真实客户端对象；
            自动化测试可以注入假客户端，避免模型网络请求。
        retrieval_settings: 可选的历史检索配置；未提供时不启用检索。
        qdrant_client: 可选的已创建检索客户端，须同时提供检索配置；
            注入对象由调用方负责关闭。
        embedding_model: 可选的已加载模型，须同时提供检索配置；
            未提供时在应用启动阶段从本地缓存加载。

    Returns:
        FastAPI: 已保存当前配置并完成路由注册的应用实例。

    Raises:
        ValueError: 注入检索资源或启用自动处理，但没有提供检索配置。
    """

    # 防止注入资源却漏传配置，导致检索仍被标为 disabled。
    if retrieval_settings is None and (
        qdrant_client is not None or embedding_model is not None
    ):
        raise ValueError("注入检索资源时必须同时提供检索配置")

    # 自动处理需要模型和向量库配置，不能带着缺失配置启动。
    if settings.ci_processing_enabled and retrieval_settings is None:
        raise ValueError("自动处理需要提供检索配置")

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

    # 哪个Engine是本函数创建、需要本函数关闭的
    managed_database_engine = None

    if database_engine is None:
        # 正常运行时由应用工厂创建并管理整个进程共享的 Engine。
        managed_database_engine = create_database_engine(settings)
        resolved_database_engine = managed_database_engine
    else:
        # 调用方注入的 Engine 不属于应用工厂，关闭责任仍由调用方承担。
        resolved_database_engine = database_engine

    # Session工厂本身不持有独立连接，可由整个应用共享。
    database_session_factory = create_database_session_factory(resolved_database_engine)

    # 正常运行时创建配置完成但尚未发起网络请求的模型客户端。
    # 测试可以注入假客户端，验证 HTTP 和编排逻辑而不产生费用。
    if deepseek_client is None:
        resolved_deepseek_client = create_deepseek_client(settings)
    else:
        resolved_deepseek_client = deepseek_client

    # 每个应用实例构图一次；请求只运行它，不重复登记节点和连接边。
    ai_diagnosis_graph = build_ai_diagnosis_graph()

    # 进入 async with
    # → 执行到 yield
    # → FastAPI 开始服务
    # → 处理0到很多个请求
    # → 应用关闭
    # → 从 yield 后继续
    # → 清理共享资源
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """准备共享资源后一次性组装 Context，退出时清理自建资源。

        Args:
            app: FastAPI 传入的当前应用实例，接收就绪的共享 Context。

        Yields:
            None: 共享资源准备完成，FastAPI 可以持续处理 HTTP 请求。

        Raises:
            ValueError: 启用检索时，本地模型缓存缺失或无法加载。
            OSError: 本地模型文件读取或底层加载失败。
        """
        # 登记清理函数，启动失败或正常退出时都按相反顺序等待执行。
        async with AsyncExitStack() as resources:
            if managed_redis_client is not None:
                resources.push_async_callback(managed_redis_client.aclose)
            if managed_database_engine is not None:
                resources.push_async_callback(managed_database_engine.dispose)

            resolved_qdrant_client = qdrant_client
            resolved_embedding_model = embedding_model

            if retrieval_settings is not None:
                if resolved_qdrant_client is None:
                    resolved_qdrant_client = create_qdrant_client(retrieval_settings)
                    # 只关闭应用创建的客户端，注入对象仍由调用方管理。
                    resources.push_async_callback(resolved_qdrant_client.close)

                if resolved_embedding_model is None:
                    # 在线程中加载本地模型；完成前不会开始处理请求。
                    # partial 把已校验配置绑定到无参数的加载调用。
                    resolved_embedding_model = await run_embedding_in_thread(
                        partial(create_embedding_model, retrieval_settings)
                    )

            # 所有资源准备完成后，一次性构造完整 Context 并交给路由复用。
            ai_diagnosis_context = AiDiagnosisGraphContext(
                session_factory=database_session_factory,
                client=resolved_deepseek_client,
                settings=settings,
                # 同一应用的检索任务共用一个许可，不随请求重新创建。
                embedding_semaphore=asyncio.Semaphore(1),
                retrieval_settings=retrieval_settings,
                qdrant_client=resolved_qdrant_client,
                embedding_model=resolved_embedding_model,
            )
            app.state.ai_diagnosis_context = ai_diagnosis_context

            try:
                # 后台任务随应用启停，退出时先等待它清理，再关闭共享资源。
                # TaskGroup ：一组由同一个生命周期统一管理的异步任务。
                async with asyncio.TaskGroup() as tasks:
                    processing_task = None

                    if settings.ci_processing_enabled:
                        # 配置入口已校验，启用检索后资源应已准备完成。
                        # 同时让类型检查器确认传入的对象不为 None。
                        assert retrieval_settings is not None
                        assert resolved_qdrant_client is not None
                        assert resolved_embedding_model is not None

                        processing_task = tasks.create_task(
                            run_ci_processing(
                                session_factory=database_session_factory,
                                # 与 HTTP 检索共用同一许可，不再加载模型。
                                embedding_semaphore=(
                                    ai_diagnosis_context.embedding_semaphore
                                ),
                                retrieval_settings=retrieval_settings,
                                qdrant_client=resolved_qdrant_client,
                                embedding_model=resolved_embedding_model,
                            ),
                            name="ci-processing",
                        )

                    try:
                        # 后台任务已安排运行，FastAPI 可以开始处理 HTTP 请求。
                        yield
                    finally:
                        if processing_task is not None:
                            # 发出取消请求，TaskGroup 等待任务清理完成。
                            processing_task.cancel()
            finally:
                # 后台任务退出后移除入口，外层随后释放自建客户端。
                del app.state.ai_diagnosis_context

    # 每次调用都创建新的应用实例，因此不同测试可以使用不同配置。
    app = FastAPI(
        title="CI Failure Intelligence API", version="0.1.0", lifespan=lifespan
    )

    # 在应用开始处理请求前，建立项目异常到 HTTP 响应的统一映射。
    register_exception_handlers(app)

    # 当前环境的真实密钥字节
    # SecretStr 只负责避免密钥在日志和调试输出中被直接显示。
    ingest_hmac_secret = settings.ingest_hmac_secret.get_secret_value().encode("utf-8")

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

    # Engine属于连接池级共享资源，不能被当作单条请求的Session。
    app.state.database_engine = resolved_database_engine

    # 后续数据库依赖会为每次业务操作调用该工厂创建独立Session。
    app.state.database_session_factory = database_session_factory

    # AI 路由从当前应用实例取得共享客户端，不在每次请求中重复创建。
    app.state.deepseek_client = resolved_deepseek_client

    # 图在应用创建时构建；完整 Context 在 lifespan 启动阶段组装。
    app.state.ai_diagnosis_graph = ai_diagnosis_graph

    # 把已经汇总的健康检查和CI接入路由注册到当前应用实例。
    app.include_router(api_router)

    # 调用者需要取得这个实例，交给Uvicorn或TestClient使用。
    return app

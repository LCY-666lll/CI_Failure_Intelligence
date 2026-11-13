"""验证共享检索资源的单次组装、所有权和启动失败清理。"""

import asyncio
import threading
from typing import cast
from unittest.mock import AsyncMock, Mock

import ci_failure_intelligence.application as application_module
import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from fastapi.testclient import TestClient
from fastembed import TextEmbedding
from langchain_deepseek import ChatDeepSeek
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
def retrieval_settings():
    """提供固定测试配置，不读取真实检索密钥。"""
    return RetrievalSettings.model_validate({"qdrant_api_key": "test-only-" + "x" * 32})


@pytest.fixture
def resources(monkeypatch):
    """替换资源工厂，观察线程、构造次数和关闭顺序，不连接外部服务。"""
    close_order = []
    load_threads = []

    async def close_redis():
        close_order.append("redis")

    async def dispose_database():
        close_order.append("database")

    async def close_qdrant():
        close_order.append("qdrant")

    redis = Mock(aclose=AsyncMock(side_effect=close_redis))
    database = Mock(dispose=AsyncMock(side_effect=dispose_database))
    qdrant = Mock(close=AsyncMock(side_effect=close_qdrant))
    model = object()

    def load_model(_settings):
        load_threads.append(threading.get_ident())
        return model

    load = Mock(side_effect=load_model)
    create_qdrant = Mock(return_value=qdrant)
    create_context = Mock(wraps=application_module.AiDiagnosisGraphContext)
    monkeypatch.setattr(
        application_module, "create_redis_client", Mock(return_value=redis)
    )
    monkeypatch.setattr(
        application_module, "create_database_engine", Mock(return_value=database)
    )
    monkeypatch.setattr(
        application_module,
        "create_database_session_factory",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        application_module, "create_deepseek_client", Mock(return_value=object())
    )
    monkeypatch.setattr(application_module, "create_qdrant_client", create_qdrant)
    monkeypatch.setattr(application_module, "create_embedding_model", load)
    monkeypatch.setattr(application_module, "AiDiagnosisGraphContext", create_context)
    return {
        "redis": redis,
        "database": database,
        "qdrant": qdrant,
        "model": model,
        "load": load,
        "create_qdrant": create_qdrant,
        "create_context": create_context,
        "close_order": close_order,
        "load_threads": load_threads,
    }


def test_disabled_retrieval_does_not_load_optional_resources(test_settings, resources):
    """默认调用方保持关闭检索，普通启动不加载模型或创建向量库客户端。"""
    app = application_module.create_app(settings=test_settings)
    assert not hasattr(app.state, "ai_diagnosis_context")
    resources["create_context"].assert_not_called()
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        context = app.state.ai_diagnosis_context
        assert context.retrieval_settings is None
        assert context.qdrant_client is None and context.embedding_model is None
    resources["load"].assert_not_called()
    resources["create_qdrant"].assert_not_called()
    resources["create_context"].assert_called_once()
    assert resources["close_order"] == ["database", "redis"]
    assert not hasattr(app.state, "ai_diagnosis_context")


def test_resources_ready_before_single_context_construction(
    test_settings, retrieval_settings, resources, monkeypatch
):
    """资源全部准备好才构造一次完整Context；多个请求共享同一实例。"""
    loop_threads = []
    real_run = application_module.run_embedding_in_thread

    async def track_loop(work):
        loop_threads.append(threading.get_ident())
        resources["create_context"].assert_not_called()
        return await real_run(work)

    monkeypatch.setattr(application_module, "run_embedding_in_thread", track_loop)
    app = application_module.create_app(
        settings=test_settings, retrieval_settings=retrieval_settings
    )
    resources["load"].assert_not_called()
    resources["create_context"].assert_not_called()
    with TestClient(app) as client:
        context = app.state.ai_diagnosis_context
        assert context.retrieval_settings is retrieval_settings
        assert context.qdrant_client is resources["qdrant"]
        assert context.embedding_model is resources["model"]
        assert context.session_factory is app.state.database_session_factory
        assert context.client is app.state.deepseek_client
        assert resources["load_threads"][0] != loop_threads[0]
        for _ in range(2):
            assert client.get("/health/live").status_code == 200
            assert app.state.ai_diagnosis_context is context
        resources["load"].assert_called_once_with(retrieval_settings)
        resources["create_qdrant"].assert_called_once_with(retrieval_settings)
        resources["create_context"].assert_called_once()
    assert resources["close_order"] == ["qdrant", "database", "redis"]
    assert not hasattr(app.state, "ai_diagnosis_context")


def test_injected_resources_are_reused_and_not_closed(
    test_settings, nonce_store, retrieval_settings, resources
):
    """应用不重新创建或关闭调用方注入的资源。"""
    app = application_module.create_app(
        settings=test_settings,
        nonce_store=nonce_store,
        database_engine=cast(AsyncEngine, resources["database"]),
        deepseek_client=cast(ChatDeepSeek, object()),
        retrieval_settings=retrieval_settings,
        qdrant_client=cast(AsyncQdrantClient, resources["qdrant"]),
        embedding_model=cast(TextEmbedding, resources["model"]),
    )
    with TestClient(app):
        assert app.state.ai_diagnosis_context.embedding_model is resources["model"]
        assert app.state.ai_diagnosis_context.qdrant_client is resources["qdrant"]
    resources["load"].assert_not_called()
    resources["create_qdrant"].assert_not_called()
    assert resources["close_order"] == []
    resources["qdrant"].close.assert_not_awaited()
    resources["database"].dispose.assert_not_awaited()
    resources["redis"].aclose.assert_not_awaited()


@pytest.mark.parametrize("error_type", [ValueError, OSError])
def test_startup_model_failure_cleans_owned_resources_without_context(
    test_settings, retrieval_settings, resources, error_type
):
    """启动失败就不发布Context，但仍清理已创建的客户端和连接池。"""
    resources["load"].side_effect = error_type("test-only-missing-model-cache")
    app = application_module.create_app(
        settings=test_settings, retrieval_settings=retrieval_settings
    )
    with (
        pytest.raises(error_type, match="test-only-missing-model-cache"),
        TestClient(app),
    ):
        pytest.fail("模型未就绪不能开始服务")
    resources["create_context"].assert_not_called()
    assert not hasattr(app.state, "ai_diagnosis_context")
    assert resources["close_order"] == ["qdrant", "database", "redis"]


def test_qdrant_close_error_does_not_skip_other_cleanup(
    test_settings, retrieval_settings, resources
):
    """关闭向量库客户端出错仍清理其余资源，且不能吞掉该错误。"""
    resources["qdrant"].close.side_effect = OSError("test-only-close-failure")
    app = application_module.create_app(
        settings=test_settings, retrieval_settings=retrieval_settings
    )
    with pytest.raises(OSError, match="test-only-close-failure"), TestClient(app):
        pass
    resources["qdrant"].close.assert_awaited_once()
    assert resources["close_order"] == ["database", "redis"]
    assert not hasattr(app.state, "ai_diagnosis_context")


def test_injected_resource_requires_configuration(test_settings, resources):
    """配置遗漏应在创建任何基础设施之前拒绝，而非静默忽略注入。"""
    with pytest.raises(ValueError, match="注入检索资源"):
        application_module.create_app(
            settings=test_settings,
            embedding_model=cast(TextEmbedding, resources["model"]),
        )
    cast(Mock, application_module.create_redis_client).assert_not_called()
    cast(Mock, application_module.create_database_engine).assert_not_called()


def test_cancelled_startup_waits_for_model_before_cleanup(
    test_settings, retrieval_settings, resources
):
    """启动加载收到取消，也不能在后台加载结束前完成资源清理。"""
    started = threading.Event()
    release = threading.Event()

    def blocked_load(_settings):
        started.set()
        if not release.wait(3):
            raise AssertionError("测试未释放模型加载线程")
        return resources["model"]

    resources["load"].side_effect = blocked_load
    app = application_module.create_app(
        settings=test_settings, retrieval_settings=retrieval_settings
    )

    async def scenario():
        async def start():
            async with app.router.lifespan_context(app):
                pytest.fail("取消后的启动不得开始服务")

        task = asyncio.create_task(start())
        try:
            async with asyncio.timeout(3):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert resources["close_order"] == []
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    resources["create_context"].assert_not_called()
    assert resources["close_order"] == ["qdrant", "database", "redis"]

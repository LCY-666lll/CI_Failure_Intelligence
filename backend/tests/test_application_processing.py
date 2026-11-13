"""验证自动处理开关、共享资源与应用生命周期。"""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from ci_failure_intelligence import application as app_module
from ci_failure_intelligence.core.config import RetrievalSettings, Settings
from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.tests.conftest import AlwaysClaimNonceStore
from backend.tests.test_config import IsolatedSettings


@pytest.mark.parametrize("enabled", [False, True])
def test_lifespan_cancels_worker_before_resource_close(
    monkeypatch, test_settings, enabled
):
    order = []

    def resource(name, close_method):
        async def close():
            order.append(name)

        return Mock(**{close_method: AsyncMock(side_effect=close)})

    monkeypatch.setattr(
        app_module, "create_redis_client", lambda _: resource("redis", "aclose")
    )
    monkeypatch.setattr(
        app_module, "create_database_engine", lambda _: resource("database", "dispose")
    )
    factory = object()
    monkeypatch.setattr(
        app_module, "create_database_session_factory", lambda _: factory
    )
    monkeypatch.setattr(app_module, "create_deepseek_client", lambda _: object())
    qdrant = resource("qdrant", "close")
    monkeypatch.setattr(app_module, "create_qdrant_client", lambda _: qdrant)
    model = object()
    started = asyncio.Event()

    async def worker(**kwargs):
        assert kwargs["embedding_model"] is model
        assert kwargs["qdrant_client"] is qdrant
        assert kwargs["session_factory"] is factory
        assert (
            kwargs["embedding_semaphore"]
            is app.state.ai_diagnosis_context.embedding_semaphore
        )
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append("worker")

    work = AsyncMock(side_effect=worker)
    monkeypatch.setattr(app_module, "run_ci_processing", work)
    app = app_module.create_app(
        settings=test_settings.model_copy(update={"ci_processing_enabled": enabled}),
        retrieval_settings=RetrievalSettings.model_validate(
            {"qdrant_api_key": "test-only-" + "f" * 32}
        ),
        embedding_model=cast(TextEmbedding, model),
    )

    async def scenario():
        async with app.router.lifespan_context(app):
            if enabled:
                await started.wait()
            else:
                await asyncio.sleep(0)
                work.assert_not_awaited()
        assert not hasattr(app.state, "ai_diagnosis_context")

    asyncio.run(scenario())
    assert order == (["worker"] if enabled else []) + ["qdrant", "database", "redis"]
    if enabled:
        work.assert_awaited_once()


def test_enabled_without_retrieval_is_startup_configuration_error(test_settings):
    with pytest.raises(ValueError, match="自动处理需要提供检索配置"):
        app_module.create_app(
            settings=test_settings.model_copy(update={"ci_processing_enabled": True})
        )


def test_worker_program_error_is_observed_by_lifespan(monkeypatch, test_settings):
    monkeypatch.setattr(
        app_module, "create_database_session_factory", lambda _: object()
    )
    monkeypatch.setattr(app_module, "create_deepseek_client", lambda _: object())

    async def fail(**kwargs):
        raise RuntimeError("test-only-program-error")

    monkeypatch.setattr(app_module, "run_ci_processing", fail)
    app = app_module.create_app(
        settings=test_settings.model_copy(update={"ci_processing_enabled": True}),
        nonce_store=AlwaysClaimNonceStore(),
        database_engine=cast(AsyncEngine, object()),
        retrieval_settings=RetrievalSettings.model_validate(
            {"qdrant_api_key": "test-only-" + "f" * 32}
        ),
        embedding_model=cast(TextEmbedding, object()),
        qdrant_client=cast(AsyncQdrantClient, object()),
    )

    async def scenario():
        with pytest.raises(ExceptionGroup) as caught:
            async with app.router.lifespan_context(app):
                await asyncio.Event().wait()
        assert any(isinstance(error, RuntimeError) for error in caught.value.exceptions)
        assert not hasattr(app.state, "ai_diagnosis_context")

    asyncio.run(scenario())


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False)])
def test_processing_switch_can_be_read_from_environment(
    monkeypatch, test_settings, value, expected
):
    monkeypatch.setenv("CFI_CI_PROCESSING_ENABLED", value)
    settings = IsolatedSettings.model_validate(
        test_settings.model_dump(exclude={"ci_processing_enabled"})
    )
    assert settings.ci_processing_enabled is expected


def test_processing_is_disabled_by_default():
    assert Settings.model_fields["ci_processing_enabled"].default is False

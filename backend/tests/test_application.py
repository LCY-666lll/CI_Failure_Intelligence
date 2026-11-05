"""验证应用工厂对共享 Redis 客户端的生命周期管理。"""

from typing import cast

import ci_failure_intelligence.application as application_module
import pytest
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.infrastructure.redis_nonce_store import RedisNonceStore
from ci_failure_intelligence.security.nonce_store import NonceStore
from fastapi.testclient import TestClient
from langchain_deepseek import ChatDeepSeek
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)


class FakeManagedRedisClient:
    """记录应用生命周期是否关闭了工厂创建的客户端。"""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeDatabaseEngine:
    """记录应用生命周期是否释放了数据库 Engine。"""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def test_application_creates_and_stores_shared_deepseek_client(
    test_settings: Settings,
    nonce_store: NonceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认路径只创建并保存模型客户端，不应在启动时发起模型调用。"""

    fake_deepseek_client = object()
    received_settings: list[Settings] = []

    def fake_create_deepseek_client(settings: Settings) -> ChatDeepSeek:
        received_settings.append(settings)
        return cast(ChatDeepSeek, fake_deepseek_client)

    monkeypatch.setattr(
        application_module,
        "create_deepseek_client",
        fake_create_deepseek_client,
    )

    app = application_module.create_app(
        settings=test_settings,
        nonce_store=nonce_store,
    )

    assert received_settings == [test_settings]
    assert app.state.deepseek_client is fake_deepseek_client


def test_application_preserves_injected_deepseek_client(
    test_settings: Settings,
    nonce_store: NonceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试注入的假模型客户端应原样保存，不能再创建真实客户端。"""

    injected_client = object()

    def forbidden_create_deepseek_client(_: Settings) -> ChatDeepSeek:
        raise AssertionError("注入客户端后不得创建真实 DeepSeek 客户端")

    monkeypatch.setattr(
        application_module,
        "create_deepseek_client",
        forbidden_create_deepseek_client,
    )

    app = application_module.create_app(
        settings=test_settings,
        nonce_store=nonce_store,
        deepseek_client=cast(ChatDeepSeek, injected_client),
    )

    assert app.state.deepseek_client is injected_client


def test_application_lifespan_closes_factory_created_redis_client(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = FakeManagedRedisClient()

    def fake_create_redis_client(_: Settings) -> Redis:
        return cast(Redis, fake_client)

    monkeypatch.setattr(
        application_module,
        "create_redis_client",
        fake_create_redis_client,
    )

    app = application_module.create_app(settings=test_settings)

    assert isinstance(app.state.nonce_store, RedisNonceStore)
    assert fake_client.closed is False

    with TestClient(app) as client:
        response = client.get("/health/live")

        assert response.status_code == 200
        assert fake_client.closed is False

    assert fake_client.closed is True


def test_application_owns_and_disposes_factory_created_database_engine(
    test_settings: Settings,
    nonce_store: NonceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_engine = FakeDatabaseEngine()
    fake_session_factory = object()

    def fake_create_database_engine(_: Settings) -> AsyncEngine:
        return cast(AsyncEngine, fake_engine)

    def fake_create_database_session_factory(
        engine: AsyncEngine,
    ) -> async_sessionmaker[AsyncSession]:
        assert engine is fake_engine
        return cast(async_sessionmaker[AsyncSession], fake_session_factory)

    monkeypatch.setattr(
        application_module,
        "create_database_engine",
        fake_create_database_engine,
    )
    monkeypatch.setattr(
        application_module,
        "create_database_session_factory",
        fake_create_database_session_factory,
    )

    app = application_module.create_app(
        settings=test_settings,
        nonce_store=nonce_store,
    )

    assert app.state.database_engine is fake_engine
    assert app.state.database_session_factory is fake_session_factory
    assert fake_engine.disposed is False

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert fake_engine.disposed is False

    assert fake_engine.disposed is True


def test_application_does_not_dispose_injected_database_engine(
    test_settings: Settings,
    nonce_store: NonceStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected_engine = FakeDatabaseEngine()
    fake_session_factory = object()

    def fake_create_database_session_factory(
        engine: AsyncEngine,
    ) -> async_sessionmaker[AsyncSession]:
        assert engine is injected_engine
        return cast(async_sessionmaker[AsyncSession], fake_session_factory)

    monkeypatch.setattr(
        application_module,
        "create_database_session_factory",
        fake_create_database_session_factory,
    )

    app = application_module.create_app(
        settings=test_settings,
        nonce_store=nonce_store,
        database_engine=cast(AsyncEngine, injected_engine),
    )

    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200

    assert app.state.database_session_factory is fake_session_factory
    assert injected_engine.disposed is False

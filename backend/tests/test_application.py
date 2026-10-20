"""验证应用工厂对共享 Redis 客户端的生命周期管理。"""

from typing import cast

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

import ci_failure_intelligence.application as application_module
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.infrastructure.redis_nonce_store import RedisNonceStore


class FakeManagedRedisClient:
    """记录应用生命周期是否关闭了工厂创建的客户端。"""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


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

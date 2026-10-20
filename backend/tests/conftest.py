"""提供不依赖真实运行环境的后端测试夹具。"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ci_failure_intelligence.application import create_app
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.security.nonce_store import (
    NonceClaimResult,
    NonceStore,
)


class AlwaysClaimNonceStore:
    """让与 nonce 无关的应用测试稳定通过安全存储步骤。"""

    async def claim(self, nonce: str) -> NonceClaimResult:
        return NonceClaimResult.CLAIMED


@pytest.fixture
def test_hmac_secret() -> bytes:
    """提供只属于自动化测试的固定 HMAC 密钥。"""

    return b"test-only-" + b"a" * 32


@pytest.fixture
def test_settings(test_hmac_secret: bytes) -> Settings:
    """创建与本地和生产密钥隔离的固定测试配置。"""

    return Settings(
        ingest_hmac_secret=SecretStr(test_hmac_secret.decode("utf-8")),
        ingest_max_body_bytes=1024 * 1024,
        redis_password=SecretStr("test-only-redis-password-with-32-bytes"),
    )


@pytest.fixture
def nonce_store() -> NonceStore:
    """提供不会访问真实 Redis 的测试 nonce 存储。"""

    return AlwaysClaimNonceStore()


@pytest.fixture
def app(
    test_settings: Settings,
    nonce_store: NonceStore,
) -> FastAPI:
    """使用测试配置创建独立的 FastAPI 应用实例。"""

    return create_app(
        settings=test_settings,
        nonce_store=nonce_store,
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """为当前测试应用提供会正确关闭的 HTTP 客户端。"""

    with TestClient(app) as test_client:
        yield test_client

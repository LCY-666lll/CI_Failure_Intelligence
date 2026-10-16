"""提供不依赖真实运行环境的后端测试夹具。"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ci_failure_intelligence.application import create_app
from ci_failure_intelligence.core.config import Settings


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
    )


@pytest.fixture
def app(test_settings: Settings) -> FastAPI:
    """使用测试配置创建独立的 FastAPI 应用实例。"""

    return create_app(settings=test_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """为当前测试应用提供会正确关闭的 HTTP 客户端。"""

    with TestClient(app) as test_client:
        yield test_client

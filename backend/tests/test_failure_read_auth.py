"""验证 CI 失败详情读取令牌的 HTTP 认证边界。"""

from collections.abc import Iterator

import pytest
from ci_failure_intelligence.api.dependencies import (
    FAILURE_READ_UNAUTHORIZED_DETAIL,
    require_failure_read_token,
)
from ci_failure_intelligence.core.config import Settings
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def protected_client(
    test_settings: Settings,
) -> Iterator[tuple[TestClient, list[str]]]:
    """创建只验证读取认证依赖的最小 FastAPI 应用。"""

    app = FastAPI()
    app.state.settings = test_settings
    endpoint_calls: list[str] = []

    @app.get(
        "/test-only/protected",
        dependencies=[Depends(require_failure_read_token)],
    )
    def protected_endpoint() -> dict[str, str]:
        endpoint_calls.append("called")
        return {"status": "allowed"}

    with TestClient(app) as client:
        yield client, endpoint_calls


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-test-token"},
        {"Authorization": "Basic dGVzdDp0ZXN0"},
    ],
)
def test_invalid_failure_read_credentials_return_401_before_endpoint(
    protected_client: tuple[TestClient, list[str]],
    headers: dict[str, str],
) -> None:
    """缺失、错误或非 Bearer 凭据都不能进入受保护端点。"""

    client, endpoint_calls = protected_client

    response = client.get(
        "/test-only/protected",
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": FAILURE_READ_UNAUTHORIZED_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    assert endpoint_calls == []


def test_valid_failure_read_token_allows_endpoint(
    protected_client: tuple[TestClient, list[str]],
    test_settings: Settings,
) -> None:
    """正确 Bearer 令牌应允许请求继续进入受保护端点。"""

    client, endpoint_calls = protected_client
    token = test_settings.failure_read_token.get_secret_value()

    response = client.get(
        "/test-only/protected",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "allowed"}
    assert endpoint_calls == ["called"]

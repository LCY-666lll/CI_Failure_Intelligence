"""验证 AI 诊断付费触发令牌与只读权限相互隔离。"""

from collections.abc import Iterator

import pytest
from ci_failure_intelligence.api.dependencies import (
    AI_DIAGNOSIS_TRIGGER_UNAUTHORIZED_DETAIL,
    require_ai_diagnosis_trigger_token,
)
from ci_failure_intelligence.core.config import Settings
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def ai_auth_client(test_settings: Settings) -> Iterator[TestClient]:
    """创建只验证认证依赖、不接触数据库和模型的探针应用。"""

    app = FastAPI()
    app.state.settings = test_settings

    @app.post(
        "/ai-diagnosis-auth-probe",
        dependencies=[Depends(require_ai_diagnosis_trigger_token)],
    )
    async def ai_diagnosis_auth_probe() -> dict[str, str]:
        return {"status": "authorized"}

    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic dXNlcjpwYXNzd29yZA=="},
        {"Authorization": "Bearer wrong-ai-trigger-token"},
        {"Authorization": ("Bearer test-only-failure-read-token-with-32-bytes")},
    ],
)
def test_ai_trigger_rejects_missing_wrong_or_read_only_credentials(
    ai_auth_client: TestClient,
    headers: dict[str, str],
    test_settings: Settings,
) -> None:
    """缺失、格式错误、错误值和只读令牌都不能触发付费操作。"""

    response = ai_auth_client.post(
        "/ai-diagnosis-auth-probe",
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": AI_DIAGNOSIS_TRIGGER_UNAUTHORIZED_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    assert (
        test_settings.ai_diagnosis_trigger_token.get_secret_value() not in response.text
    )


def test_ai_trigger_accepts_only_dedicated_trigger_token(
    ai_auth_client: TestClient,
    test_settings: Settings,
) -> None:
    """正确的独立触发令牌应允许请求进入受保护路由。"""

    trigger_token = test_settings.ai_diagnosis_trigger_token.get_secret_value()

    response = ai_auth_client.post(
        "/ai-diagnosis-auth-probe",
        headers={"Authorization": f"Bearer {trigger_token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "authorized"}


def test_ai_trigger_token_cannot_replace_failure_read_token(
    client: TestClient,
    test_settings: Settings,
) -> None:
    """AI 触发权限不能反向读取 CI 失败详情。"""

    trigger_token = test_settings.ai_diagnosis_trigger_token.get_secret_value()

    response = client.get(
        "/api/v1/ci-runs",
        headers={"Authorization": f"Bearer {trigger_token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "失败详情读取凭据无效"}

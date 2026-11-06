"""验证已有诊断GET的读取权限、状态响应与错误边界。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from ci_failure_intelligence.api.dependencies import (
    FAILURE_READ_UNAUTHORIZED_DETAIL,
    get_database_session,
)
from ci_failure_intelligence.api.exception_handlers import (
    AI_DIAGNOSIS_SERVICE_UNAVAILABLE_DETAIL,
    CI_FAILURE_QUERY_NOT_FOUND_DETAIL,
)
from ci_failure_intelligence.api.routes import ai_diagnosis as route
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.schemas.ai_diagnosis_api import AiDiagnosisReadResponse
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisPersistenceUnavailableError,
)
from ci_failure_intelligence.service.ci_failure_query import CiFailureQueryNotFoundError
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def query_mock(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    """替换查询与Session，禁止GET进入付费编排。"""

    query = AsyncMock(
        return_value=AiDiagnosisReadResponse(
            event_id=42, status="not_started", attempt_count=0
        )
    )
    monkeypatch.setattr(route, "get_ai_diagnosis", query)
    paid_call = AsyncMock(side_effect=AssertionError("GET不得进入付费编排"))
    monkeypatch.setattr(route, "orchestrate_ai_diagnosis", paid_call)

    async def fake_session():
        yield object()

    app.dependency_overrides[get_database_session] = fake_session
    try:
        yield query
    finally:
        app.dependency_overrides.pop(get_database_session, None)
        paid_call.assert_not_awaited()


def read_headers(settings: Settings) -> dict[str, str]:
    """使用测试专用读取凭据。"""

    return {"Authorization": f"Bearer {settings.failure_read_token.get_secret_value()}"}


@pytest.mark.parametrize("status", ["not_started", "processing", "completed", "failed"])
def test_read_token_returns_saved_diagnosis_state(
    client: TestClient,
    test_settings: Settings,
    query_mock: AsyncMock,
    status: str,
) -> None:
    """普通读取令牌应能查看四种状态，读取始终返回200。"""

    payload: dict[str, object] = {
        "event_id": 42,
        "status": status,
        "attempt_count": 0 if status == "not_started" else 1,
    }
    if status == "completed":
        payload["diagnosis"] = {
            "summary": "断言失败",
            "root_causes": [
                {
                    "title": "状态码不一致",
                    "explanation": "预期200，实际500",
                    "confidence": "medium",
                    "evidence_ids": ["failure:0:0"],
                }
            ],
            "recommended_actions": ["检查服务端日志"],
            "needs_more_information": False,
            "missing_information": [],
        }
    if status == "processing":
        payload["lease_expires_at"] = datetime(2026, 11, 6, tzinfo=UTC)
    if status == "failed":
        payload.update(
            error_code="provider_unavailable", error_message="模型暂时不可用"
        )

    query_mock.return_value = AiDiagnosisReadResponse.model_validate(payload)
    response = client.get(
        "/api/v1/ci-runs/42/diagnosis", headers=read_headers(test_settings)
    )

    assert response.status_code == 200
    assert response.json() == query_mock.return_value.model_dump(mode="json")
    assert "lease_token" not in response.json()
    query_mock.assert_awaited_once()
    assert query_mock.call_args.kwargs["event_id"] == 42


@pytest.mark.parametrize("credential", ["missing", "wrong", "trigger"])
def test_invalid_read_credentials_stop_before_query(
    client: TestClient,
    test_settings: Settings,
    query_mock: AsyncMock,
    credential: str,
) -> None:
    """缺失、错误及付费触发凭据都不能替代读取凭据。"""

    token = (
        test_settings.ai_diagnosis_trigger_token.get_secret_value()
        if credential == "trigger"
        else "wrong-read-token"
    )
    headers = {} if credential == "missing" else {"Authorization": f"Bearer {token}"}
    response = client.get("/api/v1/ci-runs/42/diagnosis", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": FAILURE_READ_UNAUTHORIZED_DETAIL}
    assert response.headers["www-authenticate"] == "Bearer"
    query_mock.assert_not_awaited()


@pytest.mark.parametrize("event_id", ["0", "not-an-integer"])
def test_invalid_event_id_does_not_reach_query(
    client: TestClient,
    test_settings: Settings,
    query_mock: AsyncMock,
    event_id: str,
) -> None:
    """持有效读取令牌时，路径校验失败应返回422。"""

    response = client.get(
        f"/api/v1/ci-runs/{event_id}/diagnosis", headers=read_headers(test_settings)
    )

    assert response.status_code == 422
    query_mock.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (CiFailureQueryNotFoundError(42), 404, CI_FAILURE_QUERY_NOT_FOUND_DETAIL),
        (
            AiDiagnosisPersistenceUnavailableError("test-only database detail"),
            503,
            AI_DIAGNOSIS_SERVICE_UNAVAILABLE_DETAIL,
        ),
    ],
)
def test_query_errors_use_existing_http_handlers(
    client: TestClient,
    test_settings: Settings,
    query_mock: AsyncMock,
    error: Exception,
    status: int,
    detail: str,
) -> None:
    """复用已有404和503映射，客户端看不到数据库内部异常。"""

    query_mock.side_effect = error
    response = client.get(
        "/api/v1/ci-runs/42/diagnosis", headers=read_headers(test_settings)
    )

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "test-only database detail" not in response.text

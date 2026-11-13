"""验证历史HTTP读取权限、参数边界和共享资源传递。"""

from unittest.mock import AsyncMock

import pytest
from ci_failure_intelligence.api.routes import ci_failure_query as route
from ci_failure_intelligence.schemas.failure_history import CiFailureHistoryResponse
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
)


@pytest.mark.parametrize("state", ["available", "not_ready", "disabled", "degraded"])
def test_history_route_preserves_status_and_shared_resources(
    client, app, test_settings, monkeypatch, state
):
    result = CiFailureHistoryResponse(
        event_id=17,
        parse_status="parsed",
        retrieval_status=state,
        current_failures_truncated=False,
        history=[],
    )
    query = AsyncMock(return_value=result)
    monkeypatch.setattr(route, "get_ci_failure_history", query)
    response = client.get(
        "/api/v1/ci-runs/17/history",
        headers={
            "Authorization": "Bearer "
            + test_settings.failure_read_token.get_secret_value()
        },
    )
    assert response.status_code == 200 and response.json() == result.model_dump(
        mode="json"
    )
    context = app.state.ai_diagnosis_context
    query.assert_awaited_once_with(
        event_id=17,
        session_factory=context.session_factory,
        embedding_semaphore=context.embedding_semaphore,
        retrieval_settings=context.retrieval_settings,
        qdrant_client=context.qdrant_client,
        embedding_model=context.embedding_model,
    )


@pytest.mark.parametrize("credential", ["missing", "wrong", "trigger"])
def test_history_auth_rejects_before_query(
    client, test_settings, monkeypatch, credential
):
    query = AsyncMock()
    monkeypatch.setattr(route, "get_ci_failure_history", query)
    token = (
        test_settings.ai_diagnosis_trigger_token.get_secret_value()
        if credential == "trigger"
        else "wrong-token"
    )
    headers = {} if credential == "missing" else {"Authorization": "Bearer " + token}
    response = client.get("/api/v1/ci-runs/17/history", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    query.assert_not_awaited()


@pytest.mark.parametrize("event_id", ["0", "-1", "invalid"])
def test_history_invalid_id_does_not_query(
    client, test_settings, monkeypatch, event_id
):
    query = AsyncMock()
    monkeypatch.setattr(route, "get_ci_failure_history", query)
    response = client.get(
        f"/api/v1/ci-runs/{event_id}/history",
        headers={
            "Authorization": "Bearer "
            + test_settings.failure_read_token.get_secret_value()
        },
    )
    assert response.status_code == 422
    query.assert_not_awaited()


@pytest.mark.parametrize(
    "error,expected",
    [
        (CiFailureQueryNotFoundError(17), 404),
        (CiFailureQueryUnavailableError("sensitive-internal-error"), 503),
    ],
)
def test_history_source_errors_use_existing_handlers(
    client, test_settings, monkeypatch, error, expected
):
    monkeypatch.setattr(route, "get_ci_failure_history", AsyncMock(side_effect=error))
    response = client.get(
        "/api/v1/ci-runs/17/history",
        headers={
            "Authorization": "Bearer "
            + test_settings.failure_read_token.get_secret_value()
        },
    )
    assert response.status_code == expected
    assert "sensitive-internal-error" not in response.text

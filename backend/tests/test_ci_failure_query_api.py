"""验证 CI 失败详情只读接口的认证和 HTTP 响应契约。"""

from datetime import UTC, datetime

import ci_failure_intelligence.api.routes.ci_failure_query as query_route
import pytest
from ci_failure_intelligence.api.dependencies import (
    FAILURE_READ_UNAUTHORIZED_DETAIL,
)
from ci_failure_intelligence.api.exception_handlers import (
    CI_FAILURE_QUERY_NOT_FOUND_DETAIL,
    CI_FAILURE_QUERY_UNAVAILABLE_DETAIL,
)
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
    CiFailureQueryResponse,
)
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
)
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


def create_read_headers(settings: Settings) -> dict[str, str]:
    """使用测试专用读取令牌创建 Bearer 请求头。"""

    token = settings.failure_read_token.get_secret_value()
    return {"Authorization": f"Bearer {token}"}


def build_query_response(event_id: int) -> CiFailureQueryResponse:
    """构造正式查询接口可以返回的测试响应。"""

    return CiFailureQueryResponse(
        event_id=event_id,
        repository_full_name="owner/repository",
        run_id=1001,
        run_attempt=1,
        job_key="backend_tests",
        workflow_name="Backend CI",
        job_name="Backend tests (Python 3.12)",
        commit_sha="a" * 40,
        head_branch="main",
        parse_status="parsed",
        parse_error_code=None,
        parse_error_message=None,
        parse_finished_at=datetime(2026, 11, 2, 8, 30, tzinfo=UTC),
        failures=[
            CiFailureDetailResponse(
                id=7,
                testcase_index=3,
                issue_index=0,
                suite_name="backend",
                classname="tests.test_orders",
                test_name="test_create_order",
                issue_type="failure",
                message="expected 200 but received 500",
                details="test-only traceback",
            )
        ],
    )


def test_authorized_query_returns_event_and_failure_details(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有效令牌应允许路由返回查询服务提供的失败证据。"""

    observed_event_ids: list[int] = []

    async def fake_get_ci_failure_details(
        _: AsyncSession,
        event_id: int,
    ) -> CiFailureQueryResponse:
        observed_event_ids.append(event_id)
        return build_query_response(event_id)

    monkeypatch.setattr(
        query_route,
        "get_ci_failure_details",
        fake_get_ci_failure_details,
    )

    response = client.get(
        "/api/v1/ci-runs/42/failures",
        headers=create_read_headers(test_settings),
    )

    assert response.status_code == 200
    response_body = response.json()
    assert response_body["event_id"] == 42
    assert response_body["parse_status"] == "parsed"
    assert response_body["failures"][0]["test_name"] == "test_create_order"
    assert observed_event_ids == [42]


def test_missing_read_token_returns_401_without_querying(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有读取令牌时必须在查询服务执行前停止请求。"""

    query_calls: list[int] = []

    async def fake_get_ci_failure_details(
        _: AsyncSession,
        event_id: int,
    ) -> CiFailureQueryResponse:
        query_calls.append(event_id)
        return build_query_response(event_id)

    monkeypatch.setattr(
        query_route,
        "get_ci_failure_details",
        fake_get_ci_failure_details,
    )

    response = client.get("/api/v1/ci-runs/42/failures")

    assert response.status_code == 401
    assert response.json() == {"detail": FAILURE_READ_UNAUTHORIZED_DETAIL}
    assert query_calls == []


def test_unknown_event_returns_404(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询服务确认事件不存在时应返回固定 404。"""

    async def raise_not_found(
        _: AsyncSession,
        event_id: int,
    ) -> CiFailureQueryResponse:
        raise CiFailureQueryNotFoundError(event_id)

    monkeypatch.setattr(
        query_route,
        "get_ci_failure_details",
        raise_not_found,
    )

    response = client.get(
        "/api/v1/ci-runs/404/failures",
        headers=create_read_headers(test_settings),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": CI_FAILURE_QUERY_NOT_FOUND_DETAIL}


def test_query_unavailable_returns_503_without_internal_details(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库查询不可用时应隐藏内部信息并返回固定 503。"""

    async def raise_query_unavailable(
        _: AsyncSession,
        __: int,
    ) -> CiFailureQueryResponse:
        raise CiFailureQueryUnavailableError("test-only database detail")

    monkeypatch.setattr(
        query_route,
        "get_ci_failure_details",
        raise_query_unavailable,
    )

    response = client.get(
        "/api/v1/ci-runs/42/failures",
        headers=create_read_headers(test_settings),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": CI_FAILURE_QUERY_UNAVAILABLE_DETAIL}
    assert "test-only database detail" not in response.text


def test_non_positive_event_id_returns_422_before_querying(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非正数事件 ID 应由路径参数校验拒绝。"""

    query_calls: list[int] = []

    async def fake_get_ci_failure_details(
        _: AsyncSession,
        event_id: int,
    ) -> CiFailureQueryResponse:
        query_calls.append(event_id)
        return build_query_response(event_id)

    monkeypatch.setattr(
        query_route,
        "get_ci_failure_details",
        fake_get_ci_failure_details,
    )

    response = client.get(
        "/api/v1/ci-runs/0/failures",
        headers=create_read_headers(test_settings),
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "event_id"]
    assert query_calls == []

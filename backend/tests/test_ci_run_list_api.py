"""验证最近 CI 事件列表的认证、参数和 HTTP 响应契约。"""

from datetime import UTC, datetime

import ci_failure_intelligence.api.routes.ci_failure_query as query_route
import pytest
from ci_failure_intelligence.api.dependencies import (
    FAILURE_READ_UNAUTHORIZED_DETAIL,
)
from ci_failure_intelligence.api.exception_handlers import (
    CI_RUN_LIST_UNAVAILABLE_DETAIL,
)
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.schemas.ci_run_list import (
    CiRunListItem,
    CiRunListResponse,
)
from ci_failure_intelligence.service.ci_run_list import (
    CiRunListUnavailableError,
)
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


def create_read_headers(settings: Settings) -> dict[str, str]:
    """使用测试专用读取令牌创建 Bearer 请求头。"""

    token = settings.failure_read_token.get_secret_value()
    return {"Authorization": f"Bearer {token}"}


def build_list_response() -> CiRunListResponse:
    """构造包含一条真实形状摘要的列表响应。"""

    return CiRunListResponse(
        items=[
            CiRunListItem(
                event_id=13,
                repository_full_name="LCY-666lll/CI_Failure_Intelligence",
                run_id=1793602473,
                run_attempt=1,
                job_key="backend-tests-failure-detail-manual",
                workflow_name="manual-failure-detail-verification",
                job_name="backend-tests-python-3.12-failure-manual",
                commit_sha="a" * 40,
                head_branch="main",
                received_at=datetime(2026, 11, 2, 6, 54, 50, tzinfo=UTC),
                parse_status="parsed",
                failure_count=1,
            )
        ]
    )


@pytest.mark.parametrize(
    ("query_string", "expected_limit"),
    [
        ("", 20),
        ("?limit=5", 5),
        ("?limit=100", 100),
    ],
)
def test_authorized_list_passes_validated_limit_to_service(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    query_string: str,
    expected_limit: int,
) -> None:
    """有效令牌应使用默认值或显式合法 limit 调用列表服务。"""

    observed_limits: list[int] = []

    async def fake_list_recent_ci_runs(
        _: AsyncSession,
        limit: int,
    ) -> CiRunListResponse:
        observed_limits.append(limit)
        return build_list_response()

    monkeypatch.setattr(
        query_route,
        "list_recent_ci_runs",
        fake_list_recent_ci_runs,
    )

    response = client.get(
        f"/api/v1/ci-runs{query_string}",
        headers=create_read_headers(test_settings),
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["event_id"] == 13
    assert response.json()["items"][0]["failure_count"] == 1
    assert observed_limits == [expected_limit]


def test_list_returns_200_with_empty_items(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有事件时列表接口应返回200和显式空列表。"""

    async def return_empty_list(
        _: AsyncSession,
        limit: int,
    ) -> CiRunListResponse:
        return CiRunListResponse(items=[])

    monkeypatch.setattr(
        query_route,
        "list_recent_ci_runs",
        return_empty_list,
    )

    response = client.get(
        "/api/v1/ci-runs",
        headers=create_read_headers(test_settings),
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_missing_read_token_returns_401_without_listing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有读取令牌时必须在列表服务执行前停止请求。"""

    service_calls: list[int] = []

    async def fake_list_recent_ci_runs(
        _: AsyncSession,
        limit: int,
    ) -> CiRunListResponse:
        service_calls.append(limit)
        return CiRunListResponse(items=[])

    monkeypatch.setattr(
        query_route,
        "list_recent_ci_runs",
        fake_list_recent_ci_runs,
    )

    response = client.get("/api/v1/ci-runs")

    assert response.status_code == 401
    assert response.json() == {"detail": FAILURE_READ_UNAUTHORIZED_DETAIL}
    assert service_calls == []


@pytest.mark.parametrize("invalid_limit", [0, 101])
def test_invalid_limit_returns_422_without_listing(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    invalid_limit: int,
) -> None:
    """越界 limit 应在列表服务执行前由 FastAPI 拒绝。"""

    service_calls: list[int] = []

    async def fake_list_recent_ci_runs(
        _: AsyncSession,
        limit: int,
    ) -> CiRunListResponse:
        service_calls.append(limit)
        return CiRunListResponse(items=[])

    monkeypatch.setattr(
        query_route,
        "list_recent_ci_runs",
        fake_list_recent_ci_runs,
    )

    response = client.get(
        f"/api/v1/ci-runs?limit={invalid_limit}",
        headers=create_read_headers(test_settings),
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]
    assert service_calls == []


def test_list_unavailable_returns_503_without_internal_details(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列表查询不可用时应隐藏数据库信息并返回固定503。"""

    async def raise_list_unavailable(
        _: AsyncSession,
        limit: int,
    ) -> CiRunListResponse:
        raise CiRunListUnavailableError("test-only database detail")

    monkeypatch.setattr(
        query_route,
        "list_recent_ci_runs",
        raise_list_unavailable,
    )

    response = client.get(
        "/api/v1/ci-runs",
        headers=create_read_headers(test_settings),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": CI_RUN_LIST_UNAVAILABLE_DETAIL}
    assert "test-only database detail" not in response.text

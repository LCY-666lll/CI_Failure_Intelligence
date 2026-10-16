"""验证健康检查接口对外提供的稳定契约。"""

from fastapi.testclient import TestClient


def test_live_health_check_returns_ok(client: TestClient) -> None:
    """存活接口应返回 HTTP 200 和固定的健康状态。"""

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

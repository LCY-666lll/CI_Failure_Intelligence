"""验证 CI 失败接入接口的 HTTP 契约。"""

from typing import Any

from fastapi.testclient import TestClient

from ci_failure_intelligence.main import app


client = TestClient(app)


def build_valid_payload() -> dict[str, Any]:
    """构造一份可以通过 HTTP 接入校验的 CI 请求数据。"""

    return {
        "repository": {
            "github_repository_id": 123456,
            "full_name": "LCY-666lll/CI_Failure_Intelligence",
        },
        "run": {
            "run_id": 987654,
            "run_attempt": 1,
            "workflow_name": "CI",
            "job_key": "test:0",
            "job_name": "test-python-3.12",
            "commit_sha": "a" * 40,
            "head_branch": "main",
        },
        "junit_xml": "<testsuite></testsuite>",
    }


def test_valid_ci_failure_request_returns_validated_response() -> None:
    """合法请求应返回校验成功状态和对应的运行身份。"""

    response = client.post(
        "/api/v1/ci-runs/ingest",
        json=build_valid_payload(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "run_id": 987654,
        "run_attempt": 1,
        "job_key": "test:0",
    }


def test_string_run_id_returns_422_with_nested_field_location() -> None:
    """HTTP 接入应保留 strict 校验并准确指出嵌套字段位置。"""

    payload = build_valid_payload()
    payload["run"]["run_id"] = "987654"

    response = client.post(
        "/api/v1/ci-runs/ingest",
        json=payload,
    )

    assert response.status_code == 422
    first_error = response.json()["detail"][0]
    assert first_error["loc"] == ["body", "run", "run_id"]
    assert first_error["type"] == "int_type"


def test_invalid_json_returns_422() -> None:
    """无法解析的 JSON 应在进入业务函数前被拒绝。"""

    response = client.post(
        "/api/v1/ci-runs/ingest",
        content='{"repository":',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    first_error = response.json()["detail"][0]
    assert first_error["loc"][0] == "body"
    assert first_error["type"] == "json_invalid"

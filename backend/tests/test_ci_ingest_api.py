"""验证 CI 失败接入接口的 HTTP 契约。"""

import hashlib
import hmac
import json
from typing import Any

from fastapi.testclient import TestClient

from ci_failure_intelligence.middleware.ci_ingest_security import (
    CI_INGEST_PATH,
    CI_INGEST_SIGNATURE_HEADER,
    INVALID_SIGNATURE_DETAIL,
)


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


def serialize_json(payload: dict[str, Any]) -> bytes:
    """把测试数据转换为确定的 UTF-8 JSON 原始字节。"""

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def create_signed_headers(body: bytes, secret: bytes) -> dict[str, str]:
    """为实际发送的原始 Body 创建测试签名和内容类型。"""

    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        CI_INGEST_SIGNATURE_HEADER: f"sha256={digest}",
    }


def test_valid_ci_failure_request_returns_validated_response(
    client: TestClient,
    test_hmac_secret: bytes,
) -> None:
    """合法请求应返回校验成功状态和对应的运行身份。"""

    body = serialize_json(build_valid_payload())
    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, test_hmac_secret),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "run_id": 987654,
        "run_attempt": 1,
        "job_key": "test:0",
    }


def test_string_run_id_returns_422_with_nested_field_location(
    client: TestClient,
    test_hmac_secret: bytes,
) -> None:
    """HTTP 接入应保留 strict 校验并准确指出嵌套字段位置。"""

    payload = build_valid_payload()
    payload["run"]["run_id"] = "987654"

    body = serialize_json(payload)
    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, test_hmac_secret),
    )

    assert response.status_code == 422
    first_error = response.json()["detail"][0]
    assert first_error["loc"] == ["body", "run", "run_id"]
    assert first_error["type"] == "int_type"


def test_invalid_json_returns_422(
    client: TestClient,
    test_hmac_secret: bytes,
) -> None:
    """无法解析的 JSON 应在进入业务函数前被拒绝。"""

    body = b'{"repository":'
    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, test_hmac_secret),
    )

    assert response.status_code == 422
    first_error = response.json()["detail"][0]
    assert first_error["loc"][0] == "body"
    assert first_error["type"] == "json_invalid"


def test_missing_signature_returns_401_from_real_application(
    client: TestClient,
) -> None:
    """应用工厂必须为真实 CI 接入路由启用安全中间件。"""

    response = client.post(
        CI_INGEST_PATH,
        content=serialize_json(build_valid_payload()),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}

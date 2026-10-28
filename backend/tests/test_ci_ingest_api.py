"""验证 CI 失败接入接口的 HTTP 契约。"""

import hashlib
import hmac
import json
import time
from typing import Any

import ci_failure_intelligence.api.routes.ci_ingest as ci_ingest_route
import pytest
from ci_failure_intelligence.middleware.ci_ingest_security import (
    CI_INGEST_NONCE_HEADER,
    CI_INGEST_PATH,
    CI_INGEST_SIGNATURE_HEADER,
    CI_INGEST_TIMESTAMP_HEADER,
    INVALID_SIGNATURE_DETAIL,
)
from ci_failure_intelligence.schemas.ci_ingest import CiFailureIngestRequest
from ci_failure_intelligence.service.ci_failure_persistence import (
    CiFailurePayloadConflictError,
    CiFailurePersistenceResult,
    CiFailurePersistenceStatus,
    CiFailurePersistenceUnavailableError,
)
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

TEST_NONCE = "550e8400-e29b-41d4-a716-446655440000"

TIMESTAMP_HEADER = CI_INGEST_TIMESTAMP_HEADER.decode("ascii")
NONCE_HEADER = CI_INGEST_NONCE_HEADER.decode("ascii")
SIGNATURE_HEADER = CI_INGEST_SIGNATURE_HEADER.decode("ascii")


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
    """为实际发送的原始 Body 创建 v1 安全请求头和内容类型。"""

    # 应用级测试使用真实系统时钟，因此每次签名时生成当前 timestamp。
    timestamp = str(int(time.time()))
    signing_message = (
        b"v1."
        + timestamp.encode("ascii")
        + b"."
        + TEST_NONCE.encode("ascii")
        + b"."
        + body
    )
    digest = hmac.new(secret, signing_message, hashlib.sha256).hexdigest()

    return {
        "content-type": "application/json",
        TIMESTAMP_HEADER: timestamp,
        NONCE_HEADER: TEST_NONCE,
        SIGNATURE_HEADER: f"v1={digest}",
    }


def test_valid_ci_failure_request_returns_accepted_response_after_persistence(
    client: TestClient,
    test_hmac_secret: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事务持久化成功后应返回 accepted 和对应的运行身份。"""

    persisted_requests: list[CiFailureIngestRequest] = []

    async def fake_persist_ci_failure_event(
        _: AsyncSession,
        ingest_request: CiFailureIngestRequest,
    ) -> CiFailurePersistenceResult:
        persisted_requests.append(ingest_request)
        return CiFailurePersistenceResult(
            event_id=42,
            status=CiFailurePersistenceStatus.CREATED,
        )

    monkeypatch.setattr(
        ci_ingest_route,
        "persist_ci_failure_event",
        fake_persist_ci_failure_event,
        raising=False,
    )

    body = serialize_json(build_valid_payload())
    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, test_hmac_secret),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "run_id": 987654,
        "run_attempt": 1,
        "job_key": "test:0",
    }
    assert len(persisted_requests) == 1
    assert persisted_requests[0].run.run_id == 987654


def test_changed_payload_for_existing_business_identity_returns_409(
    client: TestClient,
    test_hmac_secret: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相同业务身份对应不同内容时应返回稳定的冲突响应。"""

    async def raise_payload_conflict(
        _: AsyncSession,
        __: CiFailureIngestRequest,
    ) -> CiFailurePersistenceResult:
        raise CiFailurePayloadConflictError(event_id=42)

    monkeypatch.setattr(
        ci_ingest_route,
        "persist_ci_failure_event",
        raise_payload_conflict,
    )

    body = serialize_json(build_valid_payload())
    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, test_hmac_secret),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "同一 CI 业务事件已经存在不同内容"}


def test_database_unavailable_returns_503_without_internal_details(
    client: TestClient,
    test_hmac_secret: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库无法确认事务结果时应返回可重试的统一响应。"""

    async def raise_persistence_unavailable(
        _: AsyncSession,
        __: CiFailureIngestRequest,
    ) -> CiFailurePersistenceResult:
        raise CiFailurePersistenceUnavailableError("test-only internal database detail")

    monkeypatch.setattr(
        ci_ingest_route,
        "persist_ci_failure_event",
        raise_persistence_unavailable,
    )

    body = serialize_json(build_valid_payload())
    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, test_hmac_secret),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "CI 失败事件存储暂时不可用"}


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

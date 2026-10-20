"""验证 CI 接入 v1 HMAC 安全中间件的关键行为。"""

import asyncio
import hashlib
import hmac
from collections.abc import Callable

from httpx2 import Client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from ci_failure_intelligence.middleware.ci_ingest_security import (
    BODY_TOO_LARGE_DETAIL,
    CI_INGEST_NONCE_HEADER,
    CI_INGEST_PATH,
    CI_INGEST_SIGNATURE_HEADER,
    CI_INGEST_TIMESTAMP_HEADER,
    INVALID_SIGNATURE_DETAIL,
    SECURITY_SERVICE_UNAVAILABLE_DETAIL,
    CiIngestSecurityMiddleware,
)
from ci_failure_intelligence.security.nonce_store import (
    NonceClaimResult,
    NonceStore,
    NonceStoreUnavailableError,
)


TEST_SECRET = b"test-only-secret-with-at-least-32-bytes"
FIXED_CURRENT_TIME_SECONDS = 1_792_100_000
TEST_TIMESTAMP = str(FIXED_CURRENT_TIME_SECONDS)
TEST_NONCE = "550e8400-e29b-41d4-a716-446655440000"

# TestClient 使用字符串请求头；ASGI 中间件使用 bytes 请求头。
TIMESTAMP_HEADER = CI_INGEST_TIMESTAMP_HEADER.decode("ascii")
NONCE_HEADER = CI_INGEST_NONCE_HEADER.decode("ascii")
SIGNATURE_HEADER = CI_INGEST_SIGNATURE_HEADER.decode("ascii")


class StubNonceStore:
    """按测试指定结果响应，并记录实际收到的 nonce。"""

    def __init__(
        self,
        *,
        result: NonceClaimResult = NonceClaimResult.CLAIMED,
        error: NonceStoreUnavailableError | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.claimed_nonces: list[str] = []

    async def claim(self, nonce: str) -> NonceClaimResult:
        self.claimed_nonces.append(nonce)

        if self.error is not None:
            raise self.error

        return self.result


def create_signature(
    body: bytes,
    *,
    timestamp: str = TEST_TIMESTAMP,
    nonce: str = TEST_NONCE,
) -> str:
    """按照项目 v1 协议为测试请求生成签名。"""

    signing_message = (
        b"v1."
        + timestamp.encode("ascii")
        + b"."
        + nonce.encode("ascii")
        + b"."
        + body
    )
    digest = hmac.new(TEST_SECRET, signing_message, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def create_signed_headers(
    body: bytes,
    *,
    timestamp: str = TEST_TIMESTAMP,
    nonce: str = TEST_NONCE,
) -> dict[str, str]:
    """创建 TestClient 可以直接使用的 v1 安全请求头。"""

    return {
        TIMESTAMP_HEADER: timestamp,
        NONCE_HEADER: nonce,
        SIGNATURE_HEADER: create_signature(
            body,
            timestamp=timestamp,
            nonce=nonce,
        ),
    }


async def echo_body(request: Request) -> JSONResponse:
    """返回下游读取到的 Body，验证中间件重放没有改变字节。"""

    body = await request.body()
    return JSONResponse({"body_hex": body.hex()})


async def health_check(_: Request) -> JSONResponse:
    """提供不应被 CI 接入安全中间件拦截的测试路径。"""

    return JSONResponse({"status": "ok"})


def create_test_client(
    *,
    max_body_bytes: int,
    timestamp_tolerance_seconds: int = 300,
    clock: Callable[[], float] = lambda: float(FIXED_CURRENT_TIME_SECONDS),
    nonce_store: NonceStore | None = None,
) -> Client:
    """创建只用于测试安全中间件的轻量 ASGI 应用。"""

    downstream_app = Starlette(
        routes=[
            Route(CI_INGEST_PATH, echo_body, methods=["POST"]),
            Route("/health/live", health_check, methods=["GET"]),
        ]
    )
    secured_app = CiIngestSecurityMiddleware(
        downstream_app,
        secret=TEST_SECRET,
        max_body_bytes=max_body_bytes,
        timestamp_tolerance_seconds=timestamp_tolerance_seconds,
        nonce_store=nonce_store or StubNonceStore(),
        clock=clock,
    )
    return TestClient(secured_app)


def test_non_ingest_request_bypasses_signature_check() -> None:
    client = create_test_client(max_body_bytes=32)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_security_headers_returns_401_before_body_size_check() -> None:
    client = create_test_client(max_body_bytes=1)

    response = client.post(CI_INGEST_PATH, content=b"too-large")

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_malformed_signature_returns_401() -> None:
    client = create_test_client(max_body_bytes=32)
    headers = create_signed_headers(b"body")
    headers[SIGNATURE_HEADER] = "v1=123"

    response = client.post(CI_INGEST_PATH, content=b"body", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_wrong_signature_returns_401() -> None:
    client = create_test_client(max_body_bytes=32)
    headers = create_signed_headers(b"body")
    headers[SIGNATURE_HEADER] = f"v1={'0' * 64}"

    response = client.post(CI_INGEST_PATH, content=b"body", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_modified_timestamp_cannot_reuse_old_signature() -> None:
    body = b"body"
    client = create_test_client(max_body_bytes=32)
    headers = create_signed_headers(body)
    headers[TIMESTAMP_HEADER] = "1792100001"

    response = client.post(CI_INGEST_PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_modified_nonce_cannot_reuse_old_signature() -> None:
    body = b"body"
    client = create_test_client(max_body_bytes=32)
    headers = create_signed_headers(body)
    headers[NONCE_HEADER] = "550e8400-e29b-41d4-a716-446655440001"

    response = client.post(CI_INGEST_PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_timestamp_at_past_boundary_is_accepted() -> None:
    body = b"body"
    timestamp = str(FIXED_CURRENT_TIME_SECONDS - 300)
    client = create_test_client(max_body_bytes=32)

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, timestamp=timestamp),
    )

    assert response.status_code == 200


def test_timestamp_at_future_boundary_is_accepted() -> None:
    body = b"body"
    timestamp = str(FIXED_CURRENT_TIME_SECONDS + 300)
    client = create_test_client(max_body_bytes=32)

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, timestamp=timestamp),
    )

    assert response.status_code == 200


def test_timestamp_older_than_tolerance_returns_401() -> None:
    body = b"body"
    timestamp = str(FIXED_CURRENT_TIME_SECONDS - 301)
    client = create_test_client(max_body_bytes=32)

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, timestamp=timestamp),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_timestamp_too_far_in_future_returns_401() -> None:
    body = b"body"
    timestamp = str(FIXED_CURRENT_TIME_SECONDS + 301)
    client = create_test_client(max_body_bytes=32)

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body, timestamp=timestamp),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_claimed_nonce_reaches_downstream_application() -> None:
    body = b"body"
    nonce_store = StubNonceStore(result=NonceClaimResult.CLAIMED)
    client = create_test_client(
        max_body_bytes=32,
        nonce_store=nonce_store,
    )

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body),
    )

    assert response.status_code == 200
    assert nonce_store.claimed_nonces == [TEST_NONCE]


def test_duplicate_nonce_returns_401() -> None:
    body = b"body"
    nonce_store = StubNonceStore(result=NonceClaimResult.DUPLICATE)
    client = create_test_client(
        max_body_bytes=32,
        nonce_store=nonce_store,
    )

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}
    assert nonce_store.claimed_nonces == [TEST_NONCE]


def test_unavailable_nonce_store_returns_503() -> None:
    body = b"body"
    nonce_store = StubNonceStore(
        error=NonceStoreUnavailableError("store unavailable"),
    )
    client = create_test_client(
        max_body_bytes=32,
        nonce_store=nonce_store,
    )

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body),
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": SECURITY_SERVICE_UNAVAILABLE_DETAIL,
    }
    assert nonce_store.claimed_nonces == [TEST_NONCE]


def test_invalid_hmac_does_not_read_server_clock() -> None:
    def fail_if_called() -> float:
        raise AssertionError("HMAC 失败时不应读取服务器时间")

    body = b"body"
    nonce_store = StubNonceStore()
    client = create_test_client(
        max_body_bytes=32,
        clock=fail_if_called,
        nonce_store=nonce_store,
    )
    headers = create_signed_headers(body)
    headers[SIGNATURE_HEADER] = f"v1={'0' * 64}"

    response = client.post(CI_INGEST_PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert nonce_store.claimed_nonces == []


def test_duplicate_security_header_returns_401() -> None:
    body = b"body"
    client = create_test_client(max_body_bytes=32)
    signed_headers = create_signed_headers(body)
    headers = list(signed_headers.items())
    headers.append((NONCE_HEADER, TEST_NONCE))

    response = client.post(CI_INGEST_PATH, content=body, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_body_at_exact_limit_is_verified_and_replayed() -> None:
    body = b"12345678"
    client = create_test_client(max_body_bytes=len(body))

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body),
    )

    assert response.status_code == 200
    assert response.json() == {"body_hex": body.hex()}


def test_chunked_body_is_hashed_and_replayed_in_original_order() -> None:
    body_chunks = [b'{"first":', b'"second"}']
    complete_body = b"".join(body_chunks)
    captured_body: dict[str, bytes] = {}

    async def run_scenario() -> None:
        messages: list[Message] = [
            {
                "type": "http.request",
                "body": body_chunks[0],
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": body_chunks[1],
                "more_body": False,
            },
        ]

        async def receive() -> Message:
            return messages.pop(0)

        async def send(_: Message) -> None:
            return None

        async def downstream(
            _: Scope,
            replay_receive: Receive,
            __: Send,
        ) -> None:
            replayed_message = await replay_receive()
            captured_body["value"] = replayed_message["body"]

        signature = create_signature(complete_body).encode("ascii")
        scope: Scope = {
            "type": "http",
            "method": "POST",
            "path": CI_INGEST_PATH,
            "headers": [
                (CI_INGEST_TIMESTAMP_HEADER, TEST_TIMESTAMP.encode("ascii")),
                (CI_INGEST_NONCE_HEADER, TEST_NONCE.encode("ascii")),
                (CI_INGEST_SIGNATURE_HEADER, signature),
            ],
        }
        middleware = CiIngestSecurityMiddleware(
            downstream,
            secret=TEST_SECRET,
            max_body_bytes=len(complete_body),
            timestamp_tolerance_seconds=300,
            nonce_store=StubNonceStore(),
            clock=lambda: float(FIXED_CURRENT_TIME_SECONDS),
        )

        await middleware(scope, receive, send)

    asyncio.run(run_scenario())

    assert captured_body["value"] == complete_body


def test_body_over_limit_by_one_byte_returns_413() -> None:
    body = b"123456789"
    client = create_test_client(max_body_bytes=len(body) - 1)

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers=create_signed_headers(body),
    )

    assert response.status_code == 413
    assert response.json() == {"detail": BODY_TOO_LARGE_DETAIL}

"""验证 CI 接入 HMAC 安全中间件的关键行为。"""

import asyncio
import hashlib
import hmac

from httpx2 import Client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from ci_failure_intelligence.middleware.ci_ingest_security import (
    BODY_TOO_LARGE_DETAIL,
    CI_INGEST_PATH,
    CI_INGEST_SIGNATURE_HEADER,
    INVALID_SIGNATURE_DETAIL,
    CiIngestSecurityMiddleware,
)


TEST_SECRET = b"test-only-secret-with-at-least-32-bytes"


def create_signature(body: bytes) -> str:
    """为测试请求生成与服务端协议一致的签名头。"""

    digest = hmac.new(TEST_SECRET, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def echo_body(request: Request) -> JSONResponse:
    """返回下游实际读取到的 Body，验证中间件重放没有改变字节。"""

    body = await request.body()
    return JSONResponse({"body_hex": body.hex()})


async def health_check(_: Request) -> JSONResponse:
    """提供不应被 CI 接入安全中间件拦截的测试路径。"""

    return JSONResponse({"status": "ok"})


def create_test_client(*, max_body_bytes: int) -> Client:
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
    )
    return TestClient(secured_app)


def test_non_ingest_request_bypasses_signature_check() -> None:
    client = create_test_client(max_body_bytes=32)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_signature_returns_401_before_body_size_check() -> None:
    client = create_test_client(max_body_bytes=1)

    response = client.post(CI_INGEST_PATH, content=b"too-large")

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_malformed_signature_returns_401() -> None:
    client = create_test_client(max_body_bytes=32)

    response = client.post(
        CI_INGEST_PATH,
        content=b"body",
        headers={CI_INGEST_SIGNATURE_HEADER: "sha256=123"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_wrong_signature_returns_401() -> None:
    client = create_test_client(max_body_bytes=32)

    response = client.post(
        CI_INGEST_PATH,
        content=b"body",
        headers={CI_INGEST_SIGNATURE_HEADER: f"sha256={'0' * 64}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": INVALID_SIGNATURE_DETAIL}


def test_body_at_exact_limit_is_verified_and_replayed() -> None:
    body = b"12345678"
    client = create_test_client(max_body_bytes=len(body))

    response = client.post(
        CI_INGEST_PATH,
        content=body,
        headers={CI_INGEST_SIGNATURE_HEADER: create_signature(body)},
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
                (CI_INGEST_SIGNATURE_HEADER.lower().encode("ascii"), signature)
            ],
        }
        middleware = CiIngestSecurityMiddleware(
            downstream,
            secret=TEST_SECRET,
            max_body_bytes=len(complete_body),
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
        headers={CI_INGEST_SIGNATURE_HEADER: create_signature(body)},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": BODY_TOO_LARGE_DETAIL}

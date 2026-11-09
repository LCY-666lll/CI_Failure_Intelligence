"""验证异步Qdrant访问的认证、REST协议及失败传播边界。"""

import asyncio

import httpx
import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.infrastructure.qdrant_client import create_qdrant_client
from pydantic import SecretStr
from qdrant_client.http.exceptions import UnexpectedResponse


@pytest.mark.parametrize("http_status", [200, 503])
def test_qdrant_request_authentication_and_failure_boundary(
    monkeypatch: pytest.MonkeyPatch, http_status: int
) -> None:
    """通过真实客户端与模拟HTTP响应验证认证传递和故障不被吞掉。"""
    requests: list[httpx.Request] = []
    test_key = "test-only-qdrant-key-with-at-least-32-characters"
    settings = RetrievalSettings(
        qdrant_url="http://qdrant.test:6333",
        qdrant_api_key=SecretStr(test_key),
        qdrant_timeout_seconds=7,
    )

    async def fake_send(
        self: httpx.AsyncClient, request: httpx.Request, **kwargs: object
    ) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            http_status,
            json={"result": {"collections": []}, "status": "ok", "time": 0},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

    async def scenario() -> None:
        with pytest.warns(
            UserWarning, match="Api key is used with an insecure connection"
        ):
            client = create_qdrant_client(settings)
        assert not requests
        try:
            if http_status == 200:
                result = await client.get_collections()
                assert result.collections == []
            else:
                with pytest.raises(UnexpectedResponse) as error:
                    await client.get_collections()
                assert error.value.status_code == 503
        finally:
            await client.close()

    asyncio.run(scenario())
    assert len(requests) == 1
    assert str(requests[0].url) == "http://qdrant.test:6333/collections"
    assert requests[0].headers["api-key"] == test_key
    assert requests[0].extensions["timeout"]["read"] == 7

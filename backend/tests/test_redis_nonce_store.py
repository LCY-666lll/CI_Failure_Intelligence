"""验证 Redis nonce 存储的安全顺序和失败关闭规则。"""

import asyncio
from collections.abc import Iterable
from typing import Any, cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from ci_failure_intelligence.infrastructure.redis_nonce_store import RedisNonceStore
from ci_failure_intelligence.security.nonce_store import (
    NonceClaimResult,
    NonceStoreUnavailableError,
)


TEST_NONCE = "123e4567-e89b-42d3-a456-426614174000"
TEST_TTL_SECONDS = 660


class FakeRedisClient:
    """记录命令顺序，并返回测试指定的 Redis 结果。"""

    def __init__(
        self,
        *,
        info_results: Iterable[dict[str, Any] | BaseException],
        set_result: object | BaseException = True,
    ) -> None:
        self.info_results = iter(info_results)
        self.set_result = set_result
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def info(self, *, section: str) -> dict[str, Any]:
        self.calls.append(("info", {"section": section}))
        result = next(self.info_results)

        if isinstance(result, BaseException):
            raise result

        return result

    async def set(
        self,
        *,
        name: str,
        value: str,
        nx: bool,
        ex: int,
    ) -> object:
        self.calls.append(
            (
                "set",
                {
                    "name": name,
                    "value": value,
                    "nx": nx,
                    "ex": ex,
                },
            )
        )

        if isinstance(self.set_result, BaseException):
            raise self.set_result

        return self.set_result


def create_store(fake_client: FakeRedisClient) -> RedisNonceStore:
    """把记录命令的假客户端注入 Redis nonce 存储。"""

    return RedisNonceStore(
        redis_client=cast(Redis, fake_client),
        nonce_ttl_seconds=TEST_TTL_SECONDS,
    )


def server_state(
    *,
    run_id: object = "redis-process-a",
    uptime_seconds: object = TEST_TTL_SECONDS,
) -> dict[str, object]:
    """创建一份可按测试需要覆盖字段的 INFO server 结果。"""

    return {
        "run_id": run_id,
        "uptime_in_seconds": uptime_seconds,
    }


@pytest.mark.parametrize(
    ("set_result", "expected_result"),
    [
        (True, NonceClaimResult.CLAIMED),
        (None, NonceClaimResult.DUPLICATE),
    ],
)
def test_claim_interprets_result_only_after_two_info_checks(
    set_result: object,
    expected_result: NonceClaimResult,
) -> None:
    fake_client = FakeRedisClient(
        info_results=[
            server_state(uptime_seconds=700),
            server_state(uptime_seconds=701),
        ],
        set_result=set_result,
    )
    store = create_store(fake_client)

    result = asyncio.run(store.claim(TEST_NONCE))

    assert result is expected_result
    assert fake_client.calls == [
        ("info", {"section": "server"}),
        (
            "set",
            {
                "name": f"cfi:nonce:{TEST_NONCE}",
                "value": "1",
                "nx": True,
                "ex": TEST_TTL_SECONDS,
            },
        ),
        ("info", {"section": "server"}),
    ]


def test_claim_rejects_redis_process_still_in_warmup() -> None:
    fake_client = FakeRedisClient(
        info_results=[server_state(uptime_seconds=659)],
    )
    store = create_store(fake_client)

    with pytest.raises(NonceStoreUnavailableError):
        asyncio.run(store.claim(TEST_NONCE))

    assert fake_client.calls == [("info", {"section": "server"})]


def test_claim_converts_redis_error_to_store_unavailable() -> None:
    redis_error = RedisConnectionError("connection failed")
    fake_client = FakeRedisClient(info_results=[redis_error])
    store = create_store(fake_client)

    with pytest.raises(NonceStoreUnavailableError) as captured_error:
        asyncio.run(store.claim(TEST_NONCE))

    assert captured_error.value.__cause__ is redis_error


@pytest.mark.parametrize(
    "invalid_state",
    [
        server_state(run_id=""),
        server_state(run_id=None),
        server_state(uptime_seconds=None),
        server_state(uptime_seconds=True),
        server_state(uptime_seconds=-1),
    ],
)
def test_claim_rejects_invalid_server_state(
    invalid_state: dict[str, object],
) -> None:
    fake_client = FakeRedisClient(info_results=[invalid_state])
    store = create_store(fake_client)

    with pytest.raises(NonceStoreUnavailableError):
        asyncio.run(store.claim(TEST_NONCE))


def test_claim_rejects_redis_process_change() -> None:
    fake_client = FakeRedisClient(
        info_results=[
            server_state(run_id="redis-process-a", uptime_seconds=700),
            server_state(run_id="redis-process-b", uptime_seconds=1),
        ],
    )
    store = create_store(fake_client)

    with pytest.raises(NonceStoreUnavailableError):
        asyncio.run(store.claim(TEST_NONCE))


def test_claim_rejects_uptime_going_backwards() -> None:
    fake_client = FakeRedisClient(
        info_results=[
            server_state(uptime_seconds=700),
            server_state(uptime_seconds=699),
        ],
    )
    store = create_store(fake_client)

    with pytest.raises(NonceStoreUnavailableError):
        asyncio.run(store.claim(TEST_NONCE))


def test_claim_rejects_unknown_set_result() -> None:
    fake_client = FakeRedisClient(
        info_results=[
            server_state(uptime_seconds=700),
            server_state(uptime_seconds=701),
        ],
        set_result="OK",
    )
    store = create_store(fake_client)

    with pytest.raises(NonceStoreUnavailableError):
        asyncio.run(store.claim(TEST_NONCE))


def test_store_rejects_non_positive_safety_period() -> None:
    fake_client = FakeRedisClient(info_results=[])

    with pytest.raises(ValueError):
        RedisNonceStore(
            redis_client=cast(Redis, fake_client),
            nonce_ttl_seconds=0,
        )

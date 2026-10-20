"""验证后端运行配置的安全边界。"""

import pytest
from pydantic import SecretStr, ValidationError

from ci_failure_intelligence.core.config import Settings


TEST_SECRET = SecretStr("test-only-secret-with-at-least-32-bytes")
TEST_REDIS_PASSWORD = SecretStr("test-only-redis-password-with-32-bytes")


def create_test_settings(**overrides: object) -> Settings:
    """使用完全独立于本地 `.env` 的数据创建测试配置。"""

    settings_data: dict[str, object] = {
        "ingest_hmac_secret": TEST_SECRET,
        "redis_password": TEST_REDIS_PASSWORD,
    }
    settings_data.update(overrides)

    return Settings.model_validate(settings_data)


def test_timestamp_tolerance_defaults_to_300_seconds() -> None:
    settings = create_test_settings()

    assert settings.ingest_timestamp_tolerance_seconds == 300


def test_timestamp_tolerance_accepts_stricter_value() -> None:
    settings = create_test_settings(
        ingest_timestamp_tolerance_seconds=120,
    )

    assert settings.ingest_timestamp_tolerance_seconds == 120


@pytest.mark.parametrize("invalid_tolerance", [0, 301])
def test_timestamp_tolerance_rejects_unsafe_value(
    invalid_tolerance: int,
) -> None:
    with pytest.raises(ValidationError):
        create_test_settings(
            ingest_timestamp_tolerance_seconds=invalid_tolerance,
        )


def test_nonce_ttl_defaults_to_660_seconds() -> None:
    settings = create_test_settings()

    assert settings.ingest_nonce_ttl_seconds == 660


@pytest.mark.parametrize("valid_ttl", [660, 900])
def test_nonce_ttl_accepts_safe_value(valid_ttl: int) -> None:
    settings = create_test_settings(
        ingest_nonce_ttl_seconds=valid_ttl,
    )

    assert settings.ingest_nonce_ttl_seconds == valid_ttl


def test_nonce_ttl_rejects_value_below_safety_minimum() -> None:
    with pytest.raises(ValidationError):
        create_test_settings(
            ingest_nonce_ttl_seconds=659,
        )


def test_redis_connection_uses_safe_defaults() -> None:
    settings = create_test_settings()

    assert settings.redis_host == "127.0.0.1"
    assert settings.redis_port == 6379
    assert settings.redis_username == "cfi_ingest"
    assert settings.redis_database == 0
    assert settings.redis_connect_timeout_seconds == 1.0
    assert settings.redis_socket_timeout_seconds == 1.0
    assert settings.redis_max_connections == 10
    assert str(settings.redis_password) == "**********"


def test_redis_password_rejects_short_value() -> None:
    with pytest.raises(ValidationError):
        create_test_settings(redis_password=SecretStr("too-short"))


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("redis_port", 0),
        ("redis_port", 65536),
        ("redis_connect_timeout_seconds", 0),
        ("redis_socket_timeout_seconds", 11),
        ("redis_max_connections", 0),
        ("redis_max_connections", 101),
    ],
)
def test_redis_connection_rejects_invalid_boundaries(
    field_name: str,
    invalid_value: int,
) -> None:
    with pytest.raises(ValidationError):
        create_test_settings(**{field_name: invalid_value})

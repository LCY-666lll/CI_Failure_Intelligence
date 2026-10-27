"""验证后端运行配置的安全边界。"""

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsConfigDict

from ci_failure_intelligence.core.config import PostgresSettings, Settings


TEST_SECRET = SecretStr("test-only-secret-with-at-least-32-bytes")
TEST_REDIS_PASSWORD = SecretStr("test-only-redis-password-with-32-bytes")
TEST_POSTGRES_PASSWORD = SecretStr("test-only-postgres-password-with-32-bytes")


class IsolatedPostgresSettings(PostgresSettings):
    """关闭 `.env`，只验证测试显式提供的 PostgreSQL 配置。"""

    model_config = SettingsConfigDict(env_file=None)


class IsolatedSettings(Settings):
    """关闭 `.env`，只验证测试显式提供的完整后端配置。"""

    model_config = SettingsConfigDict(env_file=None)


def create_test_settings(**overrides: object) -> Settings:
    """使用完全独立于本地 `.env` 的数据创建测试配置。"""

    settings_data: dict[str, object] = {
        "ingest_hmac_secret": TEST_SECRET,
        "redis_password": TEST_REDIS_PASSWORD,
        "postgres_password": TEST_POSTGRES_PASSWORD,
    }
    settings_data.update(overrides)

    return Settings.model_validate(settings_data)


def test_postgres_settings_does_not_require_unrelated_security_secrets() -> None:
    """数据库专用配置不应依赖 HMAC 或 Redis 密钥。"""

    settings = IsolatedPostgresSettings.model_validate(
        {"postgres_password": TEST_POSTGRES_PASSWORD}
    )

    assert settings.postgres_database == "ci_failure_intelligence"
    assert settings.postgres_username == "cfi_app"


def test_full_settings_still_requires_ingest_and_redis_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FastAPI 完整配置不能因拆分数据库配置而放松安全要求。"""

    # 禁用进程环境变量和 .env，确保测试只观察类本身的必填字段。
    monkeypatch.delenv("CFI_INGEST_HMAC_SECRET", raising=False)
    monkeypatch.delenv("CFI_REDIS_PASSWORD", raising=False)

    with pytest.raises(ValidationError) as captured_error:
        IsolatedSettings.model_validate({"postgres_password": TEST_POSTGRES_PASSWORD})

    missing_fields = {error["loc"] for error in captured_error.value.errors()}
    assert ("ingest_hmac_secret",) in missing_fields
    assert ("redis_password",) in missing_fields


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


def test_postgres_connection_uses_safe_defaults() -> None:
    settings = create_test_settings()

    assert settings.postgres_host == "127.0.0.1"
    assert settings.postgres_port == 5432
    assert settings.postgres_database == "ci_failure_intelligence"
    assert settings.postgres_username == "cfi_app"
    assert settings.postgres_pool_size == 5
    assert settings.postgres_max_overflow == 5
    assert settings.postgres_connect_timeout_seconds == 3
    assert str(settings.postgres_password) == "**********"


def test_postgres_password_rejects_short_value() -> None:
    with pytest.raises(ValidationError):
        create_test_settings(
            postgres_password=SecretStr("too-short"),
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("postgres_port", 0),
        ("postgres_port", 65536),
        ("postgres_pool_size", 0),
        ("postgres_pool_size", 21),
        ("postgres_max_overflow", -1),
        ("postgres_max_overflow", 21),
        ("postgres_connect_timeout_seconds", 0),
        ("postgres_connect_timeout_seconds", 31),
    ],
)
def test_postgres_connection_rejects_invalid_boundaries(
    field_name: str,
    invalid_value: int,
) -> None:
    with pytest.raises(ValidationError):
        create_test_settings(**{field_name: invalid_value})


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

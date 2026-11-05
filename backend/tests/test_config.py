"""验证后端运行配置的安全边界。"""

import pytest
from ci_failure_intelligence.core.config import (
    DeepSeekSettings,
    PostgresSettings,
    Settings,
)
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsConfigDict

TEST_SECRET = SecretStr("test-only-secret-with-at-least-32-bytes")
TEST_FAILURE_READ_TOKEN = SecretStr("test-only-failure-read-token-with-32-bytes")
TEST_AI_DIAGNOSIS_TRIGGER_TOKEN = SecretStr(
    "test-only-ai-trigger-token-with-at-least-32-bytes"
)
TEST_REDIS_PASSWORD = SecretStr("test-only-redis-password-with-32-bytes")
TEST_POSTGRES_PASSWORD = SecretStr("test-only-postgres-password-with-32-bytes")
TEST_DEEPSEEK_API_KEY = SecretStr("test-only-deepseek-api-key")


class IsolatedDeepSeekSettings(DeepSeekSettings):
    """关闭 `.env`，只验证测试显式提供的 DeepSeek 配置。"""

    model_config = SettingsConfigDict(env_file=None)


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
        "failure_read_token": TEST_FAILURE_READ_TOKEN,
        "ai_diagnosis_trigger_token": TEST_AI_DIAGNOSIS_TRIGGER_TOKEN,
        "redis_password": TEST_REDIS_PASSWORD,
        "postgres_password": TEST_POSTGRES_PASSWORD,
        "deepseek_api_key": TEST_DEEPSEEK_API_KEY,
    }
    settings_data.update(overrides)

    return Settings.model_validate(settings_data)


def create_deepseek_settings(**overrides: object) -> DeepSeekSettings:
    """使用完全独立于本地 `.env` 的数据创建 DeepSeek 测试配置。"""

    settings_data: dict[str, object] = {
        "deepseek_api_key": TEST_DEEPSEEK_API_KEY,
    }
    settings_data.update(overrides)

    return IsolatedDeepSeekSettings.model_validate(settings_data)


def test_deepseek_settings_does_not_require_unrelated_backend_secrets() -> None:
    """AI 专用配置不应依赖 HMAC、Redis 或 PostgreSQL 密钥。"""

    settings = create_deepseek_settings()

    assert settings.deepseek_model == "deepseek-flash"


def test_full_settings_includes_deepseek_and_ai_trigger_configuration() -> None:
    """完整 FastAPI 配置应同时具备模型调用和付费触发配置。"""

    settings = create_test_settings()

    assert settings.redis_username == "cfi_ingest"
    assert settings.deepseek_model == "deepseek-flash"
    assert str(settings.deepseek_api_key) == "**********"
    assert str(settings.ai_diagnosis_trigger_token) == "**********"


def test_deepseek_connection_uses_bounded_defaults() -> None:
    """DeepSeek 默认配置应固定官方端点、模型和资源边界。"""

    settings = create_deepseek_settings()

    assert settings.deepseek_api_base == "https://api.deepseek.com"
    assert settings.deepseek_model == "deepseek-flash"
    assert settings.deepseek_timeout_seconds == 30.0
    assert settings.deepseek_max_retries == 1
    assert settings.deepseek_max_output_tokens == 4096
    assert str(settings.deepseek_api_key) == "**********"


@pytest.mark.parametrize(
    "invalid_api_key",
    [
        SecretStr(""),
        SecretStr(" leading-space"),
        SecretStr("trailing-space "),
        SecretStr("surrounded-by-newline\n"),
    ],
)
def test_deepseek_api_key_rejects_empty_or_surrounding_whitespace(
    invalid_api_key: SecretStr,
) -> None:
    """拒绝无法认证或容易因复制产生歧义的 API Key。"""

    with pytest.raises(ValidationError):
        create_deepseek_settings(deepseek_api_key=invalid_api_key)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("deepseek_api_base", "https://example.com"),
        ("deepseek_model", "deepseek-chat"),
        ("deepseek_timeout_seconds", 0),
        ("deepseek_timeout_seconds", 121),
        ("deepseek_max_retries", -1),
        ("deepseek_max_retries", 3),
        ("deepseek_max_output_tokens", 255),
        ("deepseek_max_output_tokens", 4097),
    ],
)
def test_deepseek_settings_rejects_unsupported_or_unbounded_values(
    field_name: str,
    invalid_value: object,
) -> None:
    """拒绝未知端点、退役模型和越过项目资源边界的数值。"""

    with pytest.raises(ValidationError):
        create_deepseek_settings(**{field_name: invalid_value})


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
    monkeypatch.delenv("CFI_FAILURE_READ_TOKEN", raising=False)
    monkeypatch.delenv("CFI_AI_DIAGNOSIS_TRIGGER_TOKEN", raising=False)
    monkeypatch.delenv("CFI_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("CFI_REDIS_PASSWORD", raising=False)

    with pytest.raises(ValidationError) as captured_error:
        IsolatedSettings.model_validate({"postgres_password": TEST_POSTGRES_PASSWORD})

    missing_fields = {error["loc"] for error in captured_error.value.errors()}
    assert ("ingest_hmac_secret",) in missing_fields
    assert ("failure_read_token",) in missing_fields
    assert ("ai_diagnosis_trigger_token",) in missing_fields
    assert ("deepseek_api_key",) in missing_fields
    assert ("redis_password",) in missing_fields


def test_failure_read_token_is_masked() -> None:
    """读取令牌不能通过配置对象的普通字符串表示泄露。"""

    settings = create_test_settings()

    assert str(settings.failure_read_token) == "**********"


def test_failure_read_token_rejects_short_value() -> None:
    """拒绝不足32个UTF-8字节的失败详情读取令牌。"""

    with pytest.raises(ValidationError):
        create_test_settings(
            failure_read_token=SecretStr("too-short"),
        )


def test_ai_diagnosis_trigger_token_is_masked() -> None:
    """付费触发令牌不能通过配置对象的普通字符串表示泄露。"""

    settings = create_test_settings()

    assert str(settings.ai_diagnosis_trigger_token) == "**********"


def test_ai_diagnosis_trigger_token_rejects_short_value() -> None:
    """拒绝不足 32 个 UTF-8 字节的 AI 诊断触发令牌。"""

    with pytest.raises(ValidationError):
        create_test_settings(
            ai_diagnosis_trigger_token=SecretStr("too-short"),
        )


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

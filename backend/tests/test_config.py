"""验证后端运行配置的安全边界。"""

import pytest
from pydantic import SecretStr, ValidationError

from ci_failure_intelligence.core.config import Settings


TEST_SECRET = SecretStr("test-only-secret-with-at-least-32-bytes")


def test_timestamp_tolerance_defaults_to_300_seconds() -> None:
    settings = Settings(ingest_hmac_secret=TEST_SECRET)

    assert settings.ingest_timestamp_tolerance_seconds == 300


def test_timestamp_tolerance_accepts_stricter_value() -> None:
    settings = Settings(
        ingest_hmac_secret=TEST_SECRET,
        ingest_timestamp_tolerance_seconds=120,
    )

    assert settings.ingest_timestamp_tolerance_seconds == 120


@pytest.mark.parametrize("invalid_tolerance", [0, 301])
def test_timestamp_tolerance_rejects_unsafe_value(
    invalid_tolerance: int,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            ingest_hmac_secret=TEST_SECRET,
            ingest_timestamp_tolerance_seconds=invalid_tolerance,
        )

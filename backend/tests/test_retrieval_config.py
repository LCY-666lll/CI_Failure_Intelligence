"""验证检索专用配置的独立性、密钥遮蔽和资源边界。"""

import os
from pathlib import Path

import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from pydantic import SecretStr, ValidationError

TEST_KEY = "test-only-qdrant-key-with-at-least-32-characters"


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离开发机环境和私有.env，避免假阳性或意外读取密钥。"""
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith("CFI_"):
            monkeypatch.delenv(name)


def test_retrieval_configuration_does_not_require_other_service_secrets() -> None:
    settings = RetrievalSettings(qdrant_api_key=SecretStr(TEST_KEY))

    assert settings.embedding_cache_dir == Path("data/local/embedding-models")
    assert not settings.embedding_cache_dir.exists()
    assert TEST_KEY not in repr(settings)
    assert str(settings.qdrant_api_key) == "**********"


def test_retrieval_configuration_reads_environment_and_overrides_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CFI_QDRANT_API_KEY", TEST_KEY)
    monkeypatch.setenv("CFI_QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("CFI_EMBEDDING_CACHE_DIR", "/models")
    monkeypatch.setenv("CFI_EMBEDDING_THREADS", "1")

    settings = RetrievalSettings.model_validate({})

    assert settings.qdrant_api_key.get_secret_value() == TEST_KEY
    assert settings.qdrant_url == "http://qdrant:6333"
    assert settings.embedding_cache_dir == Path("/models")
    assert settings.embedding_threads == 1


def test_retrieval_configuration_requires_its_own_key() -> None:
    with pytest.raises(ValidationError) as error:
        RetrievalSettings.model_validate({})

    assert [entry["loc"] for entry in error.value.errors()] == [("qdrant_api_key",)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qdrant_api_key", "short"),
        ("qdrant_api_key", ""),
        ("qdrant_timeout_seconds", 0),
        ("qdrant_timeout_seconds", 31),
        ("qdrant_timeout_seconds", 1.5),
        ("embedding_threads", 0),
        ("embedding_threads", 3),
        ("embedding_model", "other-model"),
    ],
)
def test_retrieval_configuration_rejects_unsafe_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError) as error:
        RetrievalSettings.model_validate({"qdrant_api_key": TEST_KEY, field: value})

    assert error.value.errors()[0]["loc"] == (field,)

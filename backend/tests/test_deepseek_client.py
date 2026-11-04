"""验证 DeepSeek 客户端工厂的配置映射和安全边界。"""

from typing import Any

import pytest
from ci_failure_intelligence.core.config import DeepSeekSettings
from ci_failure_intelligence.infrastructure.ai import deepseek_client
from langchain_deepseek import ChatDeepSeek
from pydantic import SecretStr


def create_test_deepseek_settings() -> DeepSeekSettings:
    """创建不依赖本机真实 API Key 的固定测试配置。"""

    return DeepSeekSettings.model_validate(
        {
            "deepseek_api_key": SecretStr("test-only-deepseek-api-key"),
        }
    )


def test_client_factory_maps_all_bounded_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工厂应把已校验配置映射为唯一且有界的客户端参数。"""

    captured_arguments: dict[str, object] = {}
    expected_client = object()

    def fake_chat_deepseek(**keyword_arguments: object) -> Any:
        captured_arguments.update(keyword_arguments)
        return expected_client

    monkeypatch.setattr(deepseek_client, "ChatDeepSeek", fake_chat_deepseek)
    settings = create_test_deepseek_settings()

    actual_client = deepseek_client.create_deepseek_client(settings)

    assert actual_client is expected_client
    assert captured_arguments == {
        "model": "deepseek-flash",
        "api_key": settings.deepseek_api_key,
        "base_url": "https://api.deepseek.com",
        "timeout": 30.0,
        "max_retries": 1,
        "max_tokens": 4096,
        "use_responses_api": False,
        "extra_body": {
            "thinking": {
                "type": "disabled",
            }
        },
    }
    assert "temperature" not in captured_arguments


def test_client_factory_builds_locked_provider_without_network_call() -> None:
    """锁定版本应能离线构造客户端，并继续遮蔽测试 API Key。"""

    client = deepseek_client.create_deepseek_client(create_test_deepseek_settings())

    assert isinstance(client, ChatDeepSeek)
    assert client.model_name == "deepseek-flash"
    assert client.openai_api_base == "https://api.deepseek.com"
    assert client.request_timeout == 30.0
    assert client.max_retries == 1
    assert client.max_tokens == 4096
    assert client.use_responses_api is False
    assert client.extra_body == {"thinking": {"type": "disabled"}}
    assert str(client.openai_api_key) == "**********"

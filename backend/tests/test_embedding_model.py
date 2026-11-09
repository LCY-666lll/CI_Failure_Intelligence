"""验证模型缓存缺失时不会偷偷联网下载。"""

import socket
from pathlib import Path

import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.infrastructure.ai.embedding_model import (
    create_embedding_model,
)
from pydantic import SecretStr


def test_missing_model_cache_fails_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """部署缓存遗漏应明确失败，不能在业务请求内退回在线下载。"""
    connections: list[object] = []

    def reject_connection(self: socket.socket, address: object) -> None:
        connections.append(address)
        raise AssertionError("模型加载不得发起网络连接")

    monkeypatch.setattr(socket.socket, "connect", reject_connection)
    settings = RetrievalSettings(
        qdrant_api_key=SecretStr("test-only-qdrant-key-with-at-least-32-characters"),
        embedding_cache_dir=tmp_path / "missing-model-cache",
    )

    with pytest.raises(ValueError):
        create_embedding_model(settings)

    assert connections == []

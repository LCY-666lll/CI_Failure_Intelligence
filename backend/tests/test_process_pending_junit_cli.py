"""验证 pending JUnit CLI 的参数、输出和 Engine 生命周期。"""

import argparse
import asyncio
from typing import cast

import pytest
from ci_failure_intelligence.cli import process_pending_junit as cli
from ci_failure_intelligence.core.config import PostgresSettings
from ci_failure_intelligence.service.junit_pending import JunitPendingBatchResult
from ci_failure_intelligence.service.junit_persistence import (
    JunitPersistenceUnavailableError,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class FakeEngine:
    """记录 CLI 是否在成功或失败后释放 Engine。"""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [("1", 1), ("100", 100)],
)
def test_parse_limit_accepts_supported_boundaries(
    raw_value: str,
    expected: int,
) -> None:
    """CLI 应接受 1 和 100 两个批次边界。"""
    assert cli._parse_limit(raw_value) == expected


@pytest.mark.parametrize("raw_value", ["0", "101", "not-a-number"])
def test_parse_limit_rejects_invalid_values(raw_value: str) -> None:
    """非法批次数量应由 argparse 在连接数据库前拒绝。"""
    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_limit(raw_value)


def test_async_runner_disposes_engine_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批处理成功后也必须释放进程级 Engine。"""
    engine = FakeEngine()
    session_factory = cast(async_sessionmaker[AsyncSession], object())
    expected = JunitPendingBatchResult(
        selected_count=3,
        parsed_count=2,
        failed_count=1,
    )

    monkeypatch.setattr(
        cli,
        "create_database_engine",
        lambda settings: cast(AsyncEngine, engine),
    )
    monkeypatch.setattr(
        cli,
        "create_database_session_factory",
        lambda actual_engine: session_factory,
    )

    async def fake_process_pending_batch(
        actual_factory: async_sessionmaker[AsyncSession],
        limit: int,
    ) -> JunitPendingBatchResult:
        assert actual_factory is session_factory
        assert limit == 7
        return expected

    monkeypatch.setattr(
        cli,
        "process_pending_junit_batch",
        fake_process_pending_batch,
    )

    result = asyncio.run(
        cli._run_pending_batch(
            cast(PostgresSettings, object()),
            limit=7,
        )
    )

    assert result == expected
    assert engine.disposed is True


def test_async_runner_disposes_engine_after_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批处理异常向上传播时，finally 仍必须释放 Engine。"""
    engine = FakeEngine()

    monkeypatch.setattr(
        cli,
        "create_database_engine",
        lambda settings: cast(AsyncEngine, engine),
    )
    monkeypatch.setattr(
        cli,
        "create_database_session_factory",
        lambda actual_engine: cast(async_sessionmaker[AsyncSession], object()),
    )

    async def unavailable_batch(
        session_factory: async_sessionmaker[AsyncSession],
        limit: int,
    ) -> JunitPendingBatchResult:
        raise JunitPersistenceUnavailableError("test-only database failure")

    monkeypatch.setattr(
        cli,
        "process_pending_junit_batch",
        unavailable_batch,
    )

    with pytest.raises(JunitPersistenceUnavailableError):
        asyncio.run(
            cli._run_pending_batch(
                cast(PostgresSettings, object()),
                limit=10,
            )
        )

    assert engine.disposed is True


def test_main_prints_committed_batch_counts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """成功命令只输出已经提交的批次统计并返回 0。"""
    expected = JunitPendingBatchResult(
        selected_count=4,
        parsed_count=3,
        failed_count=1,
    )
    fake_settings = cast(PostgresSettings, object())

    monkeypatch.setattr(cli, "PostgresSettings", lambda: fake_settings)
    monkeypatch.setattr(
        cli,
        "_run_pending_batch_with_selector_loop",
        lambda settings, limit: expected,
    )

    exit_code = cli.main(["--limit", "20"])
    output = capsys.readouterr()

    assert exit_code == 0
    assert output.err == ""
    assert "selected=4" in output.out
    assert "parsed=3" in output.out
    assert "failed=1" in output.out


def test_main_returns_one_for_database_failure_without_secret_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """数据库故障应返回 1，且错误输出不得包含数据库密码。"""
    fake_settings = cast(PostgresSettings, object())
    secret_marker = "test-only-password-must-not-appear"

    monkeypatch.setattr(cli, "PostgresSettings", lambda: fake_settings)

    def unavailable_runner(
        settings: PostgresSettings,
        limit: int,
    ) -> JunitPendingBatchResult:
        raise JunitPersistenceUnavailableError(
            f"数据库不可用；敏感底层信息={secret_marker}"
        )

    monkeypatch.setattr(
        cli,
        "_run_pending_batch_with_selector_loop",
        unavailable_runner,
    )

    exit_code = cli.main([])
    output = capsys.readouterr()

    assert exit_code == 1
    assert output.out == ""
    assert "JUnit pending 处理失败" in output.err
    assert secret_marker not in output.err


def test_invalid_limit_stops_before_loading_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """argparse 拒绝非法参数时不得读取密码或创建数据库资源。"""
    settings_loaded = False

    def unexpected_settings() -> PostgresSettings:
        nonlocal settings_loaded
        settings_loaded = True
        return cast(PostgresSettings, object())

    monkeypatch.setattr(cli, "PostgresSettings", unexpected_settings)

    with pytest.raises(SystemExit) as captured:
        cli.main(["--limit", "0"])

    assert captured.value.code == 2
    assert settings_loaded is False

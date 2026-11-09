"""验证单事件索引CLI的参数、安全输出和资源生命周期。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from ci_failure_intelligence.cli import sync_failure_vectors as cli
from ci_failure_intelligence.core.config import PostgresSettings, RetrievalSettings
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
)
from qdrant_client.http.exceptions import ApiException


@pytest.mark.parametrize("arguments", [[], ["0"], ["-1"], ["abc"]])
def test_invalid_arguments_stop_before_configuration(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    def forbidden_configuration():
        raise AssertionError("非法参数不应读取配置或创建资源")

    monkeypatch.setattr(cli, "PostgresSettings", forbidden_configuration)
    monkeypatch.setattr(cli, "RetrievalSettings", forbidden_configuration)
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize(
    "failure",
    [
        CiFailureQueryNotFoundError(13),
        CiFailureQueryUnavailableError("secret-must-not-appear"),
        ApiException("secret-must-not-appear"),
        ValueError("secret-must-not-appear"),
    ],
)
def test_runtime_failure_returns_one_without_printing_sensitive_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    monkeypatch.setattr(cli, "PostgresSettings", lambda: object())
    monkeypatch.setattr(cli, "RetrievalSettings", lambda: object())

    async def unavailable(*args):
        raise failure

    monkeypatch.setattr(cli, "_run_event_index", unavailable)
    assert cli.main(["13"]) == 1
    output = capsys.readouterr()
    assert "失败" in output.err
    assert "secret-must-not-appear" not in output.err
    assert not output.out


def install_fake_runtime(monkeypatch, *, status="parsed", failures=1, failure=None):
    events = []
    session_state = {"open": False}
    event = SimpleNamespace(parse_status=status, failures=[object()] * failures)

    async def dispose():
        events.append("engine_disposed")

    class Session:
        async def __aenter__(self):
            session_state["open"] = True
            return self

        async def __aexit__(self, *args):
            session_state["open"] = False
            events.append("session_closed")

        async def execute(self, statement):
            return SimpleNamespace(one=lambda: (123, datetime(2026, 11, 9, tzinfo=UTC)))

    async def read_details(session, event_id):
        assert event_id == 13
        return event

    def load_model(settings):
        assert not session_state["open"]
        events.append("model_loaded")
        if failure == "model":
            raise RuntimeError("test model failure")
        return object()

    async def close():
        events.append("client_closed")
        if failure == "close":
            raise RuntimeError("test close failure")

    async def write(**kwargs):
        assert not session_state["open"]
        assert kwargs["event"] is event
        assert kwargs["github_repository_id"] == 123
        events.append("write")
        if failure == "write":
            raise RuntimeError("test write failure")
        return failures

    monkeypatch.setattr(
        cli, "create_database_engine", lambda settings: SimpleNamespace(dispose=dispose)
    )
    monkeypatch.setattr(cli, "create_database_session_factory", lambda engine: Session)
    monkeypatch.setattr(cli, "get_ci_failure_details", read_details)
    monkeypatch.setattr(cli, "create_embedding_model", load_model)
    monkeypatch.setattr(
        cli, "create_qdrant_client", lambda settings: SimpleNamespace(close=close)
    )
    monkeypatch.setattr(cli, "index_ci_failure_event", write)
    return events


@pytest.mark.parametrize("failure", [None, "model", "write", "close"])
def test_runner_closes_session_before_embedding_and_disposes_resources(
    monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    events = install_fake_runtime(monkeypatch, failure=failure)
    postgres_settings = cast(PostgresSettings, object())
    retrieval_settings = cast(RetrievalSettings, object())
    if failure is None:
        assert (
            asyncio.run(cli._run_event_index(13, postgres_settings, retrieval_settings))
            == 1
        )
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(cli._run_event_index(13, postgres_settings, retrieval_settings))
    assert events[0] == "session_closed"
    assert events[-1] == "engine_disposed"
    assert ("client_closed" in events) == (failure != "model")


@pytest.mark.parametrize("status", ["parsed", "pending", "failed"])
def test_zero_failures_or_unparsed_state_never_loads_model(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    events = install_fake_runtime(monkeypatch, status=status, failures=0)
    postgres_settings = cast(PostgresSettings, object())
    retrieval_settings = cast(RetrievalSettings, object())
    if status == "parsed":
        assert (
            asyncio.run(cli._run_event_index(13, postgres_settings, retrieval_settings))
            == 0
        )
    else:
        with pytest.raises(ValueError):
            asyncio.run(cli._run_event_index(13, postgres_settings, retrieval_settings))
    assert events == ["session_closed", "engine_disposed"]


def test_main_reports_written_count_and_success_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "PostgresSettings", lambda: object())
    monkeypatch.setattr(cli, "RetrievalSettings", lambda: object())

    async def successful(event_id, *args):
        assert event_id == 13
        return 1

    monkeypatch.setattr(cli, "_run_event_index", successful)
    assert cli.main(["13"]) == 0
    output = capsys.readouterr()
    assert "event_id=13 written=1" in output.out
    assert not output.err

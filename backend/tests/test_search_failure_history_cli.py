"""验证只读历史CLI的JSON输出、故障边界和资源释放。"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from ci_failure_intelligence.cli import search_failure_history as cli
from ci_failure_intelligence.core.config import PostgresSettings, RetrievalSettings
from ci_failure_intelligence.schemas.ci_failure_query import CiFailureDetailResponse
from ci_failure_intelligence.schemas.failure_history import HistoricalFailureResponse
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
    CiFailureQueryUnavailableError,
)
from qdrant_client.http.exceptions import ApiException


@pytest.mark.parametrize("arguments", [[], ["0"], ["-1"], ["abc"]])
def test_invalid_arguments_stop_before_loading_configuration(monkeypatch, arguments):
    def forbidden():
        raise AssertionError("参数非法时不能读取配置")

    monkeypatch.setattr(cli, "PostgresSettings", forbidden)
    monkeypatch.setattr(cli, "RetrievalSettings", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


def mock_configuration(monkeypatch):
    monkeypatch.setattr(cli, "PostgresSettings", lambda: object())
    monkeypatch.setattr(cli, "RetrievalSettings", lambda: object())


def test_main_outputs_json_with_real_schema_time_and_chinese(monkeypatch, capsys):
    mock_configuration(monkeypatch)
    response = HistoricalFailureResponse(
        event_id=13,
        repository_full_name="team/backend",
        run_id=1001,
        run_attempt=1,
        job_key="backend-tests",
        received_at=datetime(2026, 11, 9, tzinfo=UTC),
        similarity_score=0.9,
        failure=CiFailureDetailResponse(
            id=1,
            testcase_index=0,
            issue_index=0,
            suite_name=None,
            classname=None,
            test_name="test_order",
            issue_type="failure",
            message="预期200实际500",
            details="AssertionError",
        ),
    )

    async def history(event_id, *args):
        assert event_id == 14
        return [response]

    monkeypatch.setattr(cli, "_run_event_history", history)
    assert cli.main(["14"]) == 0
    output = capsys.readouterr()
    data = json.loads(output.out)
    assert data["event_id"] == 14 and data["history"][0]["event_id"] == 13
    assert isinstance(data["history"][0]["received_at"], str)
    assert "预期200实际500" in output.out and not output.err


def test_normal_empty_history_is_success(monkeypatch, capsys):
    mock_configuration(monkeypatch)

    async def empty(*args):
        return []

    monkeypatch.setattr(cli, "_run_event_history", empty)
    assert cli.main(["13"]) == 0
    assert json.loads(capsys.readouterr().out) == {"event_id": 13, "history": []}


@pytest.mark.parametrize(
    "error",
    [
        CiFailureQueryNotFoundError(13),
        CiFailureQueryUnavailableError("secret-must-not-appear"),
        ApiException("secret-must-not-appear"),
        ValueError("secret-must-not-appear"),
    ],
)
def test_failed_query_returns_one_without_printing_private_exception(
    monkeypatch, capsys, error
):
    mock_configuration(monkeypatch)

    async def fail(*args):
        raise error

    monkeypatch.setattr(cli, "_run_event_history", fail)
    assert cli.main(["13"]) == 1
    output = capsys.readouterr()
    assert not output.out and "失败" in output.err
    assert "secret-must-not-appear" not in output.err


def install_runtime(monkeypatch, *, status="parsed", count=1, failure=None):
    events = []
    state = {"open": False}
    event = SimpleNamespace(parse_status=status, failures=[object()] * count)

    class Session:
        async def __aenter__(self):
            state["open"] = True
            return self

        async def __aexit__(self, *args):
            state["open"] = False
            events.append("session_closed")

    async def context(session, event_id):
        assert state["open"] and event_id == 13
        return event, 123, datetime(2026, 11, 10, tzinfo=UTC)

    async def dispose():
        events.append("engine_disposed")

    def model(settings):
        assert not state["open"]
        events.append("model_loaded")
        if failure == "model":
            raise RuntimeError("test model failure")
        return object()

    async def close():
        events.append("client_closed")
        if failure == "close":
            raise RuntimeError("test close failure")

    async def history(**kwargs):
        assert not state["open"] and kwargs["event"] is event
        assert kwargs["github_repository_id"] == 123
        events.append("retrieve")
        if failure == "retrieve":
            raise RuntimeError("test retrieve failure")
        return []

    monkeypatch.setattr(
        cli, "create_database_engine", lambda settings: SimpleNamespace(dispose=dispose)
    )
    monkeypatch.setattr(cli, "create_database_session_factory", lambda engine: Session)
    monkeypatch.setattr(cli, "get_ci_failure_context", context)
    monkeypatch.setattr(cli, "create_embedding_model", model)
    monkeypatch.setattr(
        cli, "create_qdrant_client", lambda settings: SimpleNamespace(close=close)
    )
    monkeypatch.setattr(cli, "retrieve_event_history", history)
    return events


def run_driver():
    return asyncio.run(
        cli._run_event_history(
            13, cast(PostgresSettings, object()), cast(RetrievalSettings, object())
        )
    )


@pytest.mark.parametrize("failure", [None, "model", "retrieve", "close"])
def test_resources_are_released_and_read_session_closes_before_model(
    monkeypatch, failure
):
    events = install_runtime(monkeypatch, failure=failure)
    if failure is None:
        assert run_driver() == []
    else:
        with pytest.raises(RuntimeError):
            run_driver()
    assert events[0] == "session_closed" and events[-1] == "engine_disposed"
    assert ("client_closed" in events) == (failure != "model")


@pytest.mark.parametrize("status", ["parsed", "pending", "failed"])
def test_no_model_or_vector_connection_for_zero_or_unparsed_event(monkeypatch, status):
    events = install_runtime(monkeypatch, status=status, count=0)
    if status == "parsed":
        assert run_driver() == []
    else:
        with pytest.raises(ValueError):
            run_driver()
    assert events == ["session_closed", "engine_disposed"]

"""验证五分钟后台循环的轮转、故障边界和取消行为。"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from ci_failure_intelligence.service import ci_processing as service
from ci_failure_intelligence.service.junit_pending import JunitPendingBatchResult


def make_args() -> dict[str, Any]:
    return {
        "session_factory": object(),
        "embedding_semaphore": asyncio.Semaphore(1),
        "retrieval_settings": object(),
        "qdrant_client": object(),
        "embedding_model": object(),
    }


def make_result():
    return service.CiProcessingBatchResult(JunitPendingBatchResult(0, 0, 0), (), (), 0)


def test_default_five_minute_wait_cursor_retry_and_reset(monkeypatch, caplog):
    values = make_args()
    candidates = AsyncMock(
        side_effect=[
            ([6, 13], 17),
            ([6, 13], 17),
            ([17], 17),
            ([], 17),
            ([6], 20),
        ]
    )
    batch = AsyncMock(
        side_effect=[
            service.JunitPersistenceUnavailableError("test-only-secret"),
            make_result(),
            make_result(),
            make_result(),
            make_result(),
        ]
    )
    sleeps = []

    async def sleep(seconds):
        # 每轮处理先于等待，启动不必先等五分钟。
        assert batch.await_count == len(sleeps) + 1
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise asyncio.CancelledError()

    monkeypatch.setattr(service, "list_index_repair_candidates", candidates)
    monkeypatch.setattr(service, "process_pending_ci_batch", batch)
    # 只替换当前模块的等待入口，不影响测试运行器使用的 asyncio。
    monkeypatch.setattr(service, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_ci_processing(**values))

    assert [
        (call.kwargs["after_event_id"], call.kwargs["through_event_id"])
        for call in candidates.await_args_list
    ] == [(0, None), (0, None), (13, 17), (17, 17), (0, None)]
    assert sleeps == [300.0] * 5
    assert "test-only-secret" not in caplog.text
    for call in batch.await_args_list:
        for key, value in values.items():
            assert call.kwargs[key] is value
        assert call.kwargs["limit"] == 10


@pytest.mark.parametrize(
    "options", [{"batch_size": 0}, {"batch_size": 101}, {"interval_seconds": 0}]
)
def test_bad_configuration_rejected_before_work(monkeypatch, options):
    query = AsyncMock()
    monkeypatch.setattr(service, "list_index_repair_candidates", query)
    with pytest.raises(ValueError):
        asyncio.run(service.run_ci_processing(**make_args(), **options))
    query.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        service.CiFailureQueryUnavailableError("test-only-db-failure"),
        service.CiFailureEventNotFoundError(6),
    ],
)
def test_expected_query_or_deleted_event_failure_waits_before_retry(monkeypatch, error):
    query = AsyncMock(side_effect=error)
    batch = AsyncMock()
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(service, "list_index_repair_candidates", query)
    monkeypatch.setattr(service, "process_pending_ci_batch", batch)
    monkeypatch.setattr(service, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_ci_processing(**make_args()))
    batch.assert_not_awaited()
    sleep.assert_awaited_once_with(300.0)


def test_program_error_propagates_instead_of_retrying_forever(monkeypatch):
    monkeypatch.setattr(
        service,
        "list_index_repair_candidates",
        AsyncMock(side_effect=RuntimeError("test-only-program-error")),
    )
    with pytest.raises(RuntimeError):
        asyncio.run(service.run_ci_processing(**make_args()))


def test_single_event_index_failure_advances_page_for_later_retry(monkeypatch):
    query = AsyncMock(side_effect=[([6], 17), ([], 17)])
    batch = AsyncMock(
        return_value=service.CiProcessingBatchResult(
            JunitPendingBatchResult(0, 0, 0), (), (6,), 0
        )
    )
    waits = 0

    async def sleep(seconds):
        nonlocal waits
        waits += 1
        if waits == 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(service, "list_index_repair_candidates", query)
    monkeypatch.setattr(service, "process_pending_ci_batch", batch)
    monkeypatch.setattr(service, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_ci_processing(**make_args()))
    assert query.await_args_list[1].kwargs["after_event_id"] == 6
    assert query.await_args_list[1].kwargs["through_event_id"] == 17


def test_cancel_during_batch_propagates_and_runs_batch_cleanup(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        exited = asyncio.Event()

        async def work(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        monkeypatch.setattr(
            service, "list_index_repair_candidates", AsyncMock(return_value=([], 0))
        )
        monkeypatch.setattr(service, "process_pending_ci_batch", work)
        task = asyncio.create_task(service.run_ci_processing(**make_args()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exited.is_set()

    asyncio.run(scenario())

"""验证线程计算结束前不会因请求取消而释放推理许可。"""

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import numpy as np
import pytest
from ci_failure_intelligence.core.config import RetrievalSettings
from ci_failure_intelligence.infrastructure.ai.embedding_model import (
    run_embedding_in_thread,
)
from ci_failure_intelligence.service.ai_diagnosis_orchestration import (
    AiDiagnosisGraphContext,
    AiDiagnosisGraphState,
    retrieve_failure_history_node,
)
from fastembed import TextEmbedding
from langchain_deepseek import ChatDeepSeek
from langgraph.runtime import Runtime
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.tests.test_ai_diagnosis_orchestration import (
    build_query_response,
    build_settings,
)


async def wait_for_thread_event(event: threading.Event) -> None:
    """让事件循环保持响应，并为线程启动设置测试超时。"""
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


def test_thread_work_preserves_result_and_runs_outside_event_loop() -> None:
    """普通成功调用返回原对象，工作不在事件循环线程执行。"""

    async def scenario():
        loop_thread = threading.get_ident()
        expected = object()

        def work():
            assert threading.get_ident() != loop_thread
            return expected

        assert await run_embedding_in_thread(work) is expected

    asyncio.run(scenario())


def test_thread_work_propagates_original_error() -> None:
    """没有取消时，模型错误不能被吞掉或变成空历史。"""
    failure = ValueError("test-only-invalid-model-output")

    def work():
        raise failure

    async def scenario():
        with pytest.raises(ValueError) as caught:
            await run_embedding_in_thread(work)
        assert caught.value is failure

    asyncio.run(scenario())


def test_cancelled_work_error_is_retrieved_and_cancellation_preserved() -> None:
    """取消后线程再报错，也要等待结束、读取异常并保持取消语义。"""

    async def scenario():
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        loop_errors: list[dict] = []
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        def work():
            started.set()
            try:
                if not release.wait(3):
                    raise AssertionError("测试未释放线程")
                raise RuntimeError("test-only-error-after-cancellation")
            finally:
                finished.set()

        task = asyncio.create_task(run_embedding_in_thread(work))
        try:
            await wait_for_thread_event(started)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            assert finished.is_set()
            await asyncio.sleep(0)
            assert loop_errors == []
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            loop.set_exception_handler(original_handler)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_count", [1, 2])
def test_real_retrieval_holds_permit_until_cancelled_embedding_finishes(cancel_count):
    """正式节点和检索服务串联：取消不允许下一请求并行启动原生计算。"""

    async def scenario():
        started = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        loop_thread = threading.get_ident()
        active = 0
        peak = 0
        calls = 0

        class BlockingEmbedding:
            def embed(self, documents, batch_size):
                nonlocal active, peak, calls
                # 这里是生成器实际迭代的位置，不能停留在事件循环线程。
                assert threading.get_ident() != loop_thread
                assert len(documents) == 1 and batch_size == 1
                with guard:
                    calls += 1
                    active += 1
                    peak = max(peak, active)
                started.set()
                try:
                    if not release.wait(3):
                        raise AssertionError("测试未释放线程")
                    yield np.zeros(384, dtype=np.float32)
                finally:
                    with guard:
                        active -= 1

        query_points = AsyncMock(return_value=SimpleNamespace(points=[]))
        context = AiDiagnosisGraphContext(
            session_factory=cast(async_sessionmaker[AsyncSession], object()),
            client=cast(ChatDeepSeek, object()),
            settings=build_settings(),
            embedding_semaphore=asyncio.Semaphore(1),
            retrieval_settings=RetrievalSettings.model_validate(
                {"qdrant_api_key": "test-only-" + "x" * 32}
            ),
            qdrant_client=cast(
                AsyncQdrantClient, SimpleNamespace(query_points=query_points)
            ),
            embedding_model=cast(TextEmbedding, BlockingEmbedding()),
        )
        state: AiDiagnosisGraphState = {
            "event_id": 42,
            "event": build_query_response(),
            "github_repository_id": 123456,
            "received_at": datetime(2026, 11, 12, tzinfo=UTC),
        }
        runtime = Runtime(context=context)
        first = asyncio.create_task(retrieve_failure_history_node(state, runtime))
        second = None
        try:
            await wait_for_thread_event(started)
            for _ in range(cancel_count):
                first.cancel()
                await asyncio.sleep(0)
                assert not first.done()
                assert context.embedding_semaphore.locked()

            second = asyncio.create_task(retrieve_failure_history_node(state, runtime))
            await asyncio.sleep(0)
            assert not second.done()
            assert calls == 1 and active == 1
            query_points.assert_not_awaited()

            release.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), timeout=3
            )
            assert isinstance(results[0], asyncio.CancelledError)
            assert results[1] == {"history": [], "retrieval_status": "available"}
            assert calls == 2 and peak == 1 and active == 0
            assert not context.embedding_semaphore.locked()
            # 已取消请求不再查询向量库，只有第二个请求继续检索。
            query_points.assert_awaited_once()
        finally:
            release.set()
            tasks = [first] if second is None else [first, second]
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())

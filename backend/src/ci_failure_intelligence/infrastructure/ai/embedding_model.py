"""加载本地 Embedding 模型，并管理同步模型工作的异步等待。"""

import asyncio
from collections.abc import Callable
from typing import TypeVar

# 第三方库：提供文本转向量的能力
from fastembed import TextEmbedding

from ci_failure_intelligence.core.config import RetrievalSettings

# 保留同步工作的实际返回类型，不把向量或模型对象都写成 object。
_EmbeddingResult = TypeVar("_EmbeddingResult")


def create_embedding_model(
    settings: RetrievalSettings,
) -> TextEmbedding:
    """从本地缓存加载用于失败索引和检索的模型。

    Args:
        settings: 已完成字段校验的检索配置，包含固定模型名、
            模型缓存目录和推理线程数。

    Returns:
        TextEmbedding: 已加载完成、可供调用方复用的 CPU 模型对象。
    """

    # 创建并返回模型对象
    return TextEmbedding(
        # 模型叫什么名字
        model_name=settings.embedding_model,
        # 模型文件保存在哪个目录
        cache_dir=str(settings.embedding_cache_dir),
        # 模型计算时使用多少个 CPU 线程资源
        threads=settings.embedding_threads,
        # 指定使用 CPU 进行模型推理
        providers=["CPUExecutionProvider"],
        # 创建对象时立即加载模型
        lazy_load=False,
        # 只从本地缓存找模型，不下载
        local_files_only=True,
    )


async def run_embedding_in_thread(
    work: Callable[[], _EmbeddingResult],
) -> _EmbeddingResult:
    """在线程执行模型工作，取消时等后台结束后再传播取消。

    正常执行时，同步工作的异常原样向上传播。

    Args:
        work: 无参数的同步调用；需要在线程完成的计算和生成器迭代
            都应包含在这个调用内部。

    Returns:
        _EmbeddingResult: 同步工作实际返回的结果。

    Raises:
        asyncio.CancelledError: 调用方取消执行；后台工作结束后继续传播。
    """
    # 持有后台任务引用，既能取得结果，也能在取消时等待其结束。
    # 创建一个 Task，让这个后台执行任务独立运行，并且得到一个可以跟踪的任务对象。
    worker = asyncio.create_task(asyncio.to_thread(work))
    try:
        # 保护后台任务，避免外层请求取消时直接取消 worker。
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # 同步线程无法被异步取消强制停止，退出前等待它实际完成。
        # 清理只收集工作结果或异常，不用后台异常替换本请求的取消。
        completion = asyncio.gather(worker, return_exceptions=True)
        while not completion.done():
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                # 清理期间再次收到取消，也不能提前释放推理许可。
                continue
        # 保持本请求已取消的语义，不伪装成成功或正常降级。
        raise

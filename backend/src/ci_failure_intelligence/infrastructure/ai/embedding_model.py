"""根据配置加载本地 Embedding 模型。"""

# 第三方库：提供文本转向量的能力
from fastembed import TextEmbedding

from ci_failure_intelligence.core.config import RetrievalSettings


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

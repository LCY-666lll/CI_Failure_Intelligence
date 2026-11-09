"""根据运行配置创建异步 Qdrant 客户端。"""

from qdrant_client import AsyncQdrantClient

from ci_failure_intelligence.core.config import RetrievalSettings


def create_qdrant_client(settings: RetrievalSettings) -> AsyncQdrantClient:
    """创建供索引或检索流程复用的异步 Qdrant 客户端。

    Args:
        settings: 已完成环境读取和字段校验的检索配置，
            包含服务地址、私有 Key 和请求超时。

    Returns:
        AsyncQdrantClient: 配置完成的异步客户端。
            调用方负责复用实例，并在结束时等待 close()。
    """

    return AsyncQdrantClient(
        url=settings.qdrant_url,
        # 仅在第三方客户端认证边界取出真实 Key，不打印它。
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout=settings.qdrant_timeout_seconds,
        # 当前通过 6333 端口使用 REST，不优先使用 gRPC。
        prefer_grpc=False,
        # 服务端与客户端版本已固定，避免构造时发起后台版本探测。
        check_compatibility=False,
    )

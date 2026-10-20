"""根据运行配置创建异步 Redis 客户端。"""

from redis.asyncio import Redis

from ci_failure_intelligence.core.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    """创建供整个 FastAPI 应用共享的异步 Redis 客户端。

    创建客户端对象时不会主动执行 Redis 命令。客户端内部持有连接池，
    第一次等待 Redis 命令时才会按需建立网络连接。

    Args:
        settings: 已完成环境读取和字段校验的后端运行配置，
            包含 Redis 地址、认证信息、超时和连接池上限。

    Returns:
        Redis: 配置完成但尚未执行网络命令的异步 Redis 客户端。
            调用方负责在应用关闭时等待 `aclose()`。
    """

    # 只有第三方客户端认证边界需要真实密码，
    redis_password = settings.redis_password.get_secret_value()

    # Redis客户端内部管理连接池，后续由整个FastAPI应用共享这一实例。
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_username,
        password=redis_password,
        db=settings.redis_database,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
    )

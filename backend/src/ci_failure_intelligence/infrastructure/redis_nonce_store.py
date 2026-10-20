"""使用 Redis 实现 CI 接入请求的 nonce 原子占用。"""

# 用于保存两次 INFO 读到的 Redis 进程状态。
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from ci_failure_intelligence.security.nonce_store import (
    NonceClaimResult,
    NonceStoreUnavailableError,
)


# 与 Compose 中 cfi_ingest 用户被授权的 ACL 键前缀保持一致。
NONCE_KEY_PREFIX = "cfi:nonce:"

# @dataclas:自动生成构造方法、比较方法等样板代码
# frozen=True:创建后不允许修改
@dataclass(frozen=True)
class _RedisServerState:
    """保存一次 Redis INFO server 返回的进程连续性信息。"""
    
    # Redis 当前进程的唯一身份:能判断 Redis 中间是不是重启过
    run_id: str
    # Redis 当前进程运行了多久。
    uptime_seconds: int


class RedisNonceStore:
    """使用单个直连 Redis 实例登记已经使用过的 nonce。"""

    def __init__(
        self,
        *,
        redis_client: Redis,
        nonce_ttl_seconds: int,
    ) -> None:
        """保存共享 Redis 客户端和 nonce 安全周期。

        Args:
            redis_client: 已配置认证、超时和连接池的异步 Redis 客户端。
                客户端生命周期由 FastAPI 应用工厂管理，本对象不负责关闭。
            nonce_ttl_seconds: nonce 在 Redis 中的保存秒数，同时也是
                Redis 新进程允许接收 CI 请求前必须连续运行的秒数。

        Raises:
            ValueError: nonce 安全周期不是正整数。
        """

        # Settings 已保证生产配置不低于660秒。
        # 这里仍拒绝非正数，防止测试或其他调用者绕过 Settings 后构造无效对象。
        if nonce_ttl_seconds <= 0:
            raise ValueError("nonce 安全周期必须大于 0 秒")

        self.redis_client = redis_client
        self.nonce_ttl_seconds = nonce_ttl_seconds

    async def claim(self, nonce: str) -> NonceClaimResult:
        """尝试在 Redis 中首次占用一个已经通过安全校验的 nonce。

        Args:
            nonce: 已通过小写 UUID v4 格式、HMAC 和时间窗口校验的
                CI 请求发送号码。

        Returns:
            NonceClaimResult: Redis 可靠确认首次写入时返回 `CLAIMED`；
                Redis 可靠确认 nonce 已存在时返回 `DUPLICATE`。

        Raises:
            NonceStoreUnavailableError: Redis 命令失败、服务器信息无效、
                新进程尚未完成预热、前后进程不连续，或者 SET 返回
                无法识别的结果。
        """

        nonce_key = f"{NONCE_KEY_PREFIX}{nonce}"

        try:
            # 写入 nonce 前记录当前 Redis 进程身份和运行时间。
            state_before = await self._read_server_state()

            # 新 Redis 进程的历史 nonce 可能不完整，
            # 连续运行达到安全周期以前必须失败关闭。
            if state_before.uptime_seconds < self.nonce_ttl_seconds:
                raise NonceStoreUnavailableError("Redis 新进程尚未完成 nonce 安全预热")

            # 真正执行 nonce 抢占 (SET cfi:nonce:abc 1 NX EX 660)
            # NX 保证只有不存在的 nonce 才能写入；
            # EX 在同一条命令中设置秒级过期时间。
            set_result = await self.redis_client.set(
                name=nonce_key,
                value="1",
                nx=True,
                ex=self.nonce_ttl_seconds,
            )

            # Redis 在这里重启会更换进程，无论 SET 看起来成功还是重复，都必须再次检查 Redis 进程。
            state_after = await self._read_server_state()

        except RedisError as exc:
            # 中间件不应依赖 redis-py 的具体异常类型，
            # 因此在基础设施边界转换成统一的存储不可用异常。
            raise NonceStoreUnavailableError("Redis 无法可靠完成 nonce 占用") from exc

        # run_id 变化说明两次 INFO 之间 Redis 已经切换为另一个进程。
        if state_before.run_id != state_after.run_id:
            raise NonceStoreUnavailableError("nonce 占用期间 Redis 进程身份发生变化")

        # 同一 Redis 进程的 uptime 不应倒退；
        # 倒退表示当前状态不能被可靠解释。
        if state_after.uptime_seconds < state_before.uptime_seconds:
            raise NonceStoreUnavailableError("nonce 占用期间 Redis 运行时间发生倒退")

        # redis-py 将 SET 成功响应转换为 True。
        if set_result is True:
            return NonceClaimResult.CLAIMED

        # 使用 NX 且 key 已存在时，Redis 不执行写入并返回空响应；
        # redis-py 将该响应转换为 None。
        if set_result is None:
            return NonceClaimResult.DUPLICATE

        # 只接受当前协议明确认识的 True 和 None。
        # 未知结果不能被猜测为首次或重复，必须失败关闭。
        raise NonceStoreUnavailableError("Redis 返回了无法识别的 nonce 占用结果")

    async def _read_server_state(self) -> _RedisServerState:
        """读取并校验当前 Redis 服务进程的身份和运行时间。

        Returns:
            _RedisServerState: 字段完整且类型正确的 Redis 进程状态。

        Raises:
            RedisError: Redis 连接、认证、超时或 INFO 命令执行失败。
            NonceStoreUnavailableError: INFO 返回结构缺少必要字段，
                或字段类型和值无法被可靠解释。
        """

        # redis.asyncio 的 INFO 返回可等待对象；
        # section="server" 只请求当前流程需要的服务进程信息。
        server_info = await self.redis_client.info(section="server")

        # redis-py 正常情况下返回字典；保留运行时检查，
        # 避免异常响应被继续当作可信数据使用。
        if not isinstance(server_info, dict):
            raise NonceStoreUnavailableError("Redis INFO server 未返回字典")

        run_id = server_info.get("run_id")
        uptime_seconds = server_info.get("uptime_in_seconds")

        # run_id 必须是非空字符串，才能用于比较前后进程身份。
        if not isinstance(run_id, str) or not run_id:
            raise NonceStoreUnavailableError("Redis INFO server 缺少有效的 run_id")

        # 使用 type(...) is int 排除布尔值；
        # Python 中 bool 是 int 的子类，但它不是合法 uptime。
        if type(uptime_seconds) is not int or uptime_seconds < 0:
            raise NonceStoreUnavailableError(
                "Redis INFO server 缺少有效的 uptime_in_seconds"
            )

        return _RedisServerState(
            run_id=run_id,
            uptime_seconds=uptime_seconds,
        )

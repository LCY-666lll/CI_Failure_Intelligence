"""定义 CI 接入防重放流程依赖的 nonce 存储契约。"""

from enum import Enum

# 用来定义:一个对象要有哪些方法，才算符合这个接口
from typing import Protocol


class NonceClaimResult(Enum):
    """表示 nonce 存储已经可靠确认的占用结果。"""

    # 当前 nonce 此前不存在，本次请求已经成功完成首次占用。
    CLAIMED = "claimed"

    # 当前 nonce 此前已经被占用，本次请求属于原始安全信封的重复提交。
    DUPLICATE = "duplicate"


class NonceStoreUnavailableError(RuntimeError):
    """继承 RuntimeError,表示 nonce 存储当前无法可靠判断或登记请求号码。"""


class NonceStore(Protocol):
    """声明 CI 接入安全中间件需要的 nonce 存储能力。"""

    async def claim(self, nonce: str) -> NonceClaimResult:
        """尝试首次占用一个已经通过安全校验的 nonce。

        Args:
            nonce: 已通过小写 UUID v4 格式、HMAC 和时间窗口校验的
                CI 请求发送号码。调用方不得传入未经验证的请求头文本。

        Returns:
            NonceClaimResult: 首次占用成功时返回 `CLAIMED`；
                已经存在相同 nonce 时返回 `DUPLICATE`。

        Raises:
            NonceStoreUnavailableError: 存储无法连接、无法确认 Redis
                进程连续性，或者无法可靠解释命令结果。
        """

        # Protocol 只声明方法约定，真正行为由 Redis 实现或测试假实现提供。
        ...

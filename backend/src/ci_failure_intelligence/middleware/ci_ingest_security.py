"""提供 CI 失败接入接口使用的 HMAC 安全中间件。"""

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from collections.abc import Callable

from starlette import status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


# 只有这个方法和路径组成的请求需要执行 CI 接入安全检查。
CI_INGEST_METHOD = "POST"
CI_INGEST_PATH = "/api/v1/ci-runs/ingest"

# ASGI scope 使用 bytes 保存原始请求头名称和值。
# 请求头名称统一使用小写 bytes，便于直接和 scope["headers"] 比较。
CI_INGEST_TIMESTAMP_HEADER = b"x-cfi-timestamp"
CI_INGEST_NONCE_HEADER = b"x-cfi-nonce"
CI_INGEST_SIGNATURE_HEADER = b"x-cfi-signature-256"

# 所有签名认证失败都返回相同提示，避免暴露具体失败环节。
INVALID_SIGNATURE_DETAIL = "请求签名无效"

# Body 超过传输层限制时返回的提示。
BODY_TOO_LARGE_DETAIL = "请求体超过允许的大小"

# timestamp 只能是 0，或者不以 0 开头的 1 至 10 位十进制数字。
TIMESTAMP_PATTERN = re.compile(rb"(?:0|[1-9][0-9]{0,9})")

# UUID v4 使用固定的 8-4-4-4-12 格式，并且只接受小写十六进制字符。
NONCE_PATTERN = re.compile(
    rb"[0-9a-f]{8}-"
    rb"[0-9a-f]{4}-"
    rb"4[0-9a-f]{3}-"
    rb"[89ab][0-9a-f]{3}-"
    rb"[0-9a-f]{12}"
)

# v1 签名由固定版本前缀和 64 位小写 SHA-256 十六进制摘要组成。
SIGNATURE_PATTERN = re.compile(rb"v1=([0-9a-f]{64})")


class _RequestBodyTooLargeError(Exception):
    """表示实际读取到的请求 Body 已经超过配置上限。"""


@dataclass(frozen=True)
class ParsedSecurityHeaders:
    """保存已经通过文本格式校验的 v1 安全请求头。"""

    # 保留 timestamp 的原始 ASCII 字节，验签前不能重新格式化。
    timestamp: bytes

    # 保留 nonce 的原始 ASCII 字节，确保服务端签名输入与客户端完全一致。
    nonce: bytes

    # 摘要正则只允许 ASCII 十六进制字符，因此这里可以安全转换为 str。
    provided_digest: str


def is_ci_ingest_request(scope: Scope) -> bool:
    """判断当前 ASGI 请求是否为需要保护的 CI 接入请求。

    Args:
        scope: ASGI 提供的连接和请求元数据。

    Returns:
        bool: 方法和路径都匹配 CI 接入接口时返回 True，否则返回 False。
    """

    if scope["type"] != "http":
        return False

    return (
        scope.get("method") == CI_INGEST_METHOD and scope.get("path") == CI_INGEST_PATH
    )


def get_single_header_value(
    scope: Scope,
    header_name: bytes,
) -> bytes | None:
    """读取一个必须恰好出现一次的 ASGI 原始请求头。

    Args:
        scope: ASGI 提供的连接和请求元数据，其中包含原始请求头列表。
        header_name: 已转换为小写 bytes 的目标请求头名称。

    Returns:
        bytes | None: 请求头恰好出现一次时返回未经修改的原始值；
            请求头缺失或重复时返回 None。
    """

    # ASGI 把请求头保存为 (名称 bytes, 值 bytes) 组成的可迭代对象。
    raw_headers = scope.get("headers", [])

    # 逐项查找目标请求头，不能使用 dict，因为 dict 会丢失重复项。
    matched_values = []
    for name, value in raw_headers:
        if name.lower() == header_name:
            matched_values.append(value)

    # 缺失和重复都违反“恰好出现一次”的安全协议。
    if len(matched_values) != 1:
        return None

    # 不执行 strip、decode 或重新编码，保留客户端实际发送的原始字节。
    return matched_values[0]


def parse_security_headers(
    scope: Scope,
) -> ParsedSecurityHeaders | None:
    """读取并校验 CI 接入请求的三个 v1 安全请求头。

    Args:
        scope: 已确认属于目标 CI 接入接口的 ASGI HTTP 请求元数据。

    Returns:
        ParsedSecurityHeaders | None: 三个请求头均唯一且格式正确时，
            返回保留原始 timestamp、nonce 和摘要的对象；
            任一请求头缺失、重复或格式错误时返回 None。
    """

    timestamp = get_single_header_value(
        scope,
        CI_INGEST_TIMESTAMP_HEADER,
    )
    nonce = get_single_header_value(
        scope,
        CI_INGEST_NONCE_HEADER,
    )
    signature = get_single_header_value(
        scope,
        CI_INGEST_SIGNATURE_HEADER,
    )

    # 三个安全请求头缺少任何一个，都不能组成完整的 v1 安全信封。
    if timestamp is None or nonce is None or signature is None:
        return None

    # 检查格式合法：
    # fullmatch 要求整个 timestamp 都符合协议，不能只匹配其中一部分。
    if TIMESTAMP_PATTERN.fullmatch(timestamp) is None:
        return None

    # nonce 必须完整符合小写 UUID v4 文本格式。
    if NONCE_PATTERN.fullmatch(nonce) is None:
        return None

    # 签名必须完整符合 v1=<64位小写十六进制摘要>。
    signature_match = SIGNATURE_PATTERN.fullmatch(signature)

    if signature_match is None:
        return None

    # 正则捕获组只可能包含 ASCII 十六进制字符，可以安全解码。
    provided_digest = signature_match.group(1).decode("ascii")

    return ParsedSecurityHeaders(
        timestamp=timestamp,
        nonce=nonce,
        provided_digest=provided_digest,
    )


def build_signing_prefix(
    timestamp: bytes,
    nonce: bytes,
) -> bytes:
    """构造 v1 HMAC 在原始 Body 之前使用的固定签名前缀。

    Args:
        timestamp: 已通过格式校验的 timestamp 原始 ASCII 字节。
        nonce: 已通过格式校验的 nonce 原始 ASCII 字节。

    Returns:
        bytes: 按照 `v1.<timestamp>.<nonce>.` 组成的签名前缀。
    """

    # Body 会在后续分块追加，因此这里的前缀必须以最后一个点号结束。
    return b"v1." + timestamp + b"." + nonce + b"."

def is_timestamp_within_tolerance(
    timestamp: bytes,
    *,
    current_time_seconds: int,
    tolerance_seconds: int,
) -> bool:
    """判断请求时间是否位于服务器允许的时间窗口内。

    Args:
        timestamp: 已通过格式校验和 HMAC 验证的 Unix 秒级时间戳原始字节。
        current_time_seconds: 当前服务器的 Unix 秒级时间。
        tolerance_seconds: 请求时间允许早于或晚于服务器时间的最大秒数。

    Returns:
        bool: 时间差小于或等于容差时返回 True，否则返回 False

    """

    # timestamp 已经过十进制格式校验和 HMAC 验证，可以安全地转换为整数参与时间计算。
    request_time_seconds = int(timestamp)
    # abs 同时覆盖过去和未来两个方向的时钟偏差。
    time_difference_seconds = abs(current_time_seconds - request_time_seconds)

    return time_difference_seconds <= tolerance_seconds

class CiIngestSecurityMiddleware:
    """保护 CI 接入接口的纯 ASGI 安全中间件。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: bytes,
        max_body_bytes: int,
        timestamp_tolerance_seconds:int,
        clock:Callable[[],float] = time.time
    ) -> None:
        """保存下游应用和已经校验的安全配置。

        Args:
            app: 验签成功后继续处理请求的下游 ASGI 应用。
            secret: 用于计算 HMAC-SHA256 的密钥字节。
            max_body_bytes: 允许读取和暂存的最大 Body 字节数。
            timestamp_tolerance_seconds: 请求时间与服务器时间允许相差的最大秒数。
            clock: 返回当前 Unix 秒级时间的函数。生产环境默认使用
                time.time 自动化测试可以注入固定时钟。
        """

        # 中间件完成检查后，通过这个对象把请求交给 FastAPI。
        self.app = app

        # 密钥在应用工厂中从 SecretStr 取出并编码为 UTF-8 字节。
        self.secret = secret

        # 这个限制针对实际收到的原始字节，不是字符串字符数量。
        self.max_body_bytes = max_body_bytes

        # 配置层已经保证该值大于 0 且不超过 v1 协议的 300 秒。
        self.timestamp_tolerance_seconds = timestamp_tolerance_seconds

        # 保存函数对象，请求通过 HMAC 后才调用它取得当时的服务器时间。
        self.clock = clock

    async def __call__(
        self,
        # 请求类型、POST、路径、请求头等信息。
        scope: Scope,
        # 调用这个函数可以取得请求 Body 的消息。
        receive: Receive,
        # 调用这个函数可以把响应消息交回服务器。
        # Uvicorn 最终负责把 send 收到的响应通过网络发回客户端。
        send: Send,
    ) -> None:
        """检查目标 CI 接入请求，并将合法请求交给下游应用。

        Args:
            scope: 当前连接和 HTTP 请求的元数据。
            receive: 从 ASGI 服务器异步读取请求消息的函数。
            send: 向 ASGI 服务器异步发送响应消息的函数。
        """

        # 非目标请求不读取 Body，也不要求携带 CI 安全请求头。
        if not is_ci_ingest_request(scope):
            # 原封不动交给后面的 FastAPI，没有提前读取 Body。
            await self.app(scope, receive, send)
            return

        # 直接读取 ASGI 原始请求头，确保缺失和重复请求头都能被拒绝。
        security_headers = parse_security_headers(scope)

        # 任一安全头缺失、重复或格式错误时，都返回统一的 401。
        if security_headers is None:
            await self._send_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=INVALID_SIGNATURE_DETAIL,
            )
            return

        # timestamp 和 nonce 已通过格式检查，可以构造固定的 v1 签名前缀。
        signing_prefix = build_signing_prefix(
            timestamp=security_headers.timestamp,
            nonce=security_headers.nonce,
        )

        try:
            body_result = await self._read_and_hash_body(
                receive,
                signing_prefix=signing_prefix,
            )
        except _RequestBodyTooLargeError:
            await self._send_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=BODY_TOO_LARGE_DETAIL,
            )
            return

        # 客户端在 Body 读取完成前断开连接时，不再调用 FastAPI。
        if body_result is None:
            return

        body, computed_digest = body_result

        # 使用安全比较函数核对客户端摘要和服务端实际计算的摘要。
        if not hmac.compare_digest(
            security_headers.provided_digest,
            computed_digest,
        ):
            await self._send_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=INVALID_SIGNATURE_DETAIL,
            )
            return

        # 只有 HMAC 通过后，timestamp 才能被视为可信的客户端输入。
        current_time_seconds = int(self.clock())

        # 过旧或过于超前的请求使用统一的签名无效响应。
        if not is_timestamp_within_tolerance(
            security_headers.timestamp,
            current_time_seconds=current_time_seconds,
            tolerance_seconds=self.timestamp_tolerance_seconds,
        ):
            await self._send_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=INVALID_SIGNATURE_DETAIL,
            )
            return

        # 记录已经验证的 Body 是否已经向 FastAPI 重放。
        body_has_been_replayed = False

        # FastAPI 也需要读取传入的 Body，才能把它解析成 JSON。
        async def replay_receive() -> Message:
            """把已经验签的原始 Body 作为一条 ASGI 消息交给下游应用。"""

            nonlocal body_has_been_replayed

            if not body_has_been_replayed:
                body_has_been_replayed = True

                # 第一次被调用时，返回刚才验证过的客户端原始 Body。
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }

            # 如果下游继续监听连接状态，转回原始 receive 获取后续消息。
            return await receive()

        # FastAPI 此时读取到的仍是客户端实际发送并且已经验签的原始字节。
        # 原始 receive：中间件从 Uvicorn 取得客户端发来的 Body。
        # replay_receive：FastAPI 从中间件取得已经验证并保存的 Body。
        await self.app(scope, replay_receive, send)

    async def _read_and_hash_body(
        self,
        receive: Receive,
        *,
        signing_prefix: bytes,
    ) -> tuple[bytes, str] | None:
        """分块读取 Body，同时检查大小并计算 v1 HMAC-SHA256。

        Args:
            receive: 从 ASGI 服务器读取请求消息的异步函数。
            signing_prefix: 已由 timestamp 和 nonce 构造的 v1 签名前缀。

        Returns:
            tuple[bytes, str] | None: 正常读完时返回完整 Body 和摘要；
                客户端提前断开连接时返回 None。

        Raises:
            _RequestBodyTooLargeError: 实际 Body 超过配置的字节上限。
        """

        # 创建 HMAC 计算器时先写入 v1 安全前缀。
        # 后续收到的每个 Body 分块会继续追加到同一个计算过程。
        digest_calculator = hmac.new(
            self.secret,
            msg=signing_prefix,
            digestmod=hashlib.sha256,
        )

        body_chunks: list[bytes] = []
        received_body_bytes = 0

        # Uvicorn 可能把这份 Body 的字节分成多条消息交给应用。
        # 可能一次就给全，也可能先给一部分、再给剩余部分。
        # 代码必须同时支持这两种情况。
        while True:
            # 每次调用 receive 获取一个 ASGI 请求消息。
            message = await receive()

            if message["type"] == "http.disconnect":
                return None

            # HTTP 请求 Body 使用 http.request 消息传输。
            if message["type"] != "http.request":
                continue

            # 有 "body" 键时取得当前分块，例如 b'{"run":'。
            # 没有 "body" 键时使用默认值 b""，表示零个字节。
            body_chunk = message.get("body", b"")
            received_body_bytes += len(body_chunk)

            # 先判断大小，超限分块不会加入 Body 缓冲列表。
            if received_body_bytes > self.max_body_bytes:
                raise _RequestBodyTooLargeError

            # 把当前客户端 Body 分块继续加入 v1 HMAC 计算。
            digest_calculator.update(body_chunk)

            if body_chunk:
                # 保存当前原始字节，稍后原样交给 FastAPI。
                body_chunks.append(body_chunk)

            # more_body=False 表示已经收到最后一个 Body 分块。
            if not message.get("more_body", False):
                break

        # join 会生成交给 FastAPI 的完整原始 Body。
        body = b"".join(body_chunks)

        return body, digest_calculator.hexdigest()

    async def _send_error_response(
        self,
        *,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        """从 ASGI 中间件边界发送统一的 JSON 错误响应。

        Args:
            scope: 当前请求的 ASGI 元数据。
            receive: 当前请求的 ASGI 接收函数。
            send: 当前请求的 ASGI 发送函数。
            status_code: 需要返回的 HTTP 状态码。
            detail: 返回给请求方的错误说明。
        """

        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail},
        )

        # Response 对象本身也是一个 ASGI 应用，可以直接发送响应。
        await response(scope, receive, send)

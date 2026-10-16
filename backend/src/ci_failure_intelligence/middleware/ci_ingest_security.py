"""提供 CI 失败接入接口使用的 HMAC 安全中间件。"""

import hashlib
import hmac
import re

from starlette import status
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


# 只有这个方法和路径组成的请求需要执行 CI 接入安全检查。
CI_INGEST_METHOD = "POST"
CI_INGEST_PATH = "/api/v1/ci-runs/ingest"

# GitHub Actions 客户端和服务端共同约定的签名请求头名称。
CI_INGEST_SIGNATURE_HEADER = "X-CFI-Signature-256"

# 所有签名认证失败都返回相同提示，避免暴露具体失败环节。
INVALID_SIGNATURE_DETAIL = "请求签名无效"

# Body 超过传输层限制时返回的提示。
BODY_TOO_LARGE_DETAIL = "请求体超过允许的大小"

# SHA-256 摘要是 32 字节，转换为十六进制文本后正好是 64 个字符。
SIGNATURE_PATTERN = re.compile(r"sha256=([0-9a-fA-F]{64})")


class _RequestBodyTooLargeError(Exception):
    """表示实际读取到的请求 Body 已经超过配置上限。"""


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


def parse_signature_header(header_value: str | None) -> str | None:
    """解析 CI 接入请求头中的 SHA-256 签名摘要。

    Args:
        header_value: 从签名请求头读取的完整文本；
            请求头不存在时传入 None。

    Returns:
        str | None: 格式正确时返回统一转换为小写的摘要；
            请求头缺失或格式错误时返回 None。
    """

    if header_value is None:
        return None

    # fullmatch 要求整个请求头都符合协议，不能只匹配其中一部分。
    match = SIGNATURE_PATTERN.fullmatch(header_value)

    if match is None:
        return None

    # group(1) 取得正则表达式小括号捕获的 64 位摘要。
    return match.group(1).lower()


class CiIngestSecurityMiddleware:
    """保护 CI 接入接口的纯 ASGI 安全中间件。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: bytes,
        max_body_bytes: int,
    ) -> None:
        """保存下游应用和已经校验的安全配置。

        Args:
            app: 验签成功后继续处理请求的下游 ASGI 应用。
            secret: 用于计算 HMAC-SHA256 的密钥字节。
            max_body_bytes: 允许读取和暂存的最大 Body 字节数。
        """

        # 中间件完成检查后，通过这个对象把请求交给 FastAPI。
        self.app = app

        # 密钥在应用工厂中从 SecretStr 取出并编码为 UTF-8 字节。
        self.secret = secret

        # 这个限制针对实际收到的原始字节，不是字符串字符数量。
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        # 请求类型、POST、路径、请求头等信息
        scope: Scope,
        # 调用这个函数可以取得请求 Body 的消息
        receive: Receive,
        # 调用这个函数可以把响应消息交回服务器
        # Uvicorn最终负责把 send 收到的响应通过网络发回客户端
        send: Send,
    ) -> None:
        """检查目标 CI 接入请求，并将合法请求交给下游应用。

        Args:
            scope: 当前连接和 HTTP 请求的元数据。
            receive: 从 ASGI 服务器异步读取请求消息的函数。
            send: 向 ASGI 服务器异步发送响应消息的函数。
        """

        # 非目标请求不读取 Body，也不要求携带 CI 签名。
        if not is_ci_ingest_request(scope):
            # 原封不动交给后面的 FastAPI,没有读取 Body
            await self.app(scope, receive, send)
            return

        # Headers 会以不区分大小写的方式读取 scope 中的原始请求头。
        headers = Headers(scope=scope)

        # 先检查签名格式，再决定是否读取可能很大的请求 Body。
        provided_digest = parse_signature_header(
            headers.get(CI_INGEST_SIGNATURE_HEADER)
        )

        # 缺失签名和格式错误都立即返回相同的 401。
        if provided_digest is None:
            await self._send_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=INVALID_SIGNATURE_DETAIL,
            )
            return

        try:
            body_result = await self._read_and_hash_body(receive)
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
        if not hmac.compare_digest(provided_digest, computed_digest):
            await self._send_error_response(
                scope=scope,
                receive=receive,
                send=send,
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=INVALID_SIGNATURE_DETAIL,
            )
            return

        # 记录已验证的 Body 是否已经向 FastAPI 重放。
        body_has_been_replayed = False

        # FastAPI也需要读取传入的body，才能把它解析成 JSON
        async def replay_receive() -> Message:
            """把已经验签的原始 Body 作为一条 ASGI 消息交给下游应用。"""

            nonlocal body_has_been_replayed

            if not body_has_been_replayed:
                body_has_been_replayed = True

                # 第一次被调用时，返回刚才验证过的客户端原始 Body
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }

            # 如果下游继续监听连接状态，转回原始 receive 获取后续消息。
            return await receive()

        # FastAPI 此时读取到的仍是客户端实际发送并且已经验签的原始字节。
        # 原始 receive：中间件从 Uvicorn 取客户端发来的body;
        # replay_receive：FastAPI从中间件取已经验证并保存的body
        await self.app(scope, replay_receive, send)

    async def _read_and_hash_body(
        self,
        receive: Receive,
    ) -> tuple[bytes, str] | None:
        """分块读取 Body，同时检查大小并计算 HMAC-SHA256。

        Args:
            receive: 从 ASGI 服务器读取请求消息的异步函数。

        Returns:
            tuple[bytes, str] | None: 正常读完时返回完整 Body 和摘要；
                客户端提前断开连接时返回 None。

        Raises:
            _RequestBodyTooLargeError: 实际 Body 超过配置的字节上限。
        """

        # 创建增量 HMAC 计算器，不需要先把完整 Body 拼接出来。
        digest_calculator = hmac.new(
            # __init__()保存的服务器密钥
            self.secret,
            digestmod=hashlib.sha256,
        )

        body_chunks: list[bytes] = []
        received_body_bytes = 0

        # Uvicorn可能把这份 Body 的字节分成多条消息交给应用。
        # 可能一次就给全，也可能先给一部分、再给剩余部分。代码必须都能处理
        while True:
            # 每次调用 receive 获取一个 ASGI 请求消息。
            message = await receive()

            if message["type"] == "http.disconnect":
                return None

            # HTTP 请求 Body 使用 http.request 消息传输。
            if message["type"] != "http.request":
                continue

            # 有 "body" 键 → 取它的值，比如 b'{"run":'；
            # 没有 "body" 键 → 使用默认值 b""，也就是零个字节
            body_chunk = message.get("body", b"")
            received_body_bytes += len(body_chunk)

            # 先判断大小，超限分块不会加入我们的 Body 缓冲列表。
            if received_body_bytes > self.max_body_bytes:
                raise _RequestBodyTooLargeError

            # 把这一部分客户端 Body 加入 HMAC 计算
            digest_calculator.update(body_chunk)

            if body_chunk:
                # 把这一部分原始字节保存起来，稍后交给 FastAPI
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

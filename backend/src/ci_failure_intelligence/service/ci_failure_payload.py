"""计算 CI 失败事件稳定业务内容的摘要。"""

import hashlib
import json

from ci_failure_intelligence.schemas.ci_ingest import CiFailureIngestRequest


def calculate_ci_failure_payload_sha256(ingest_request: CiFailureIngestRequest) -> str:
    """计算已经通过校验的 CI 失败业务内容摘要。

    Args:
        ingest_request: 已通过 Pydantic 严格校验的 CI 失败接入请求。

    Returns:
        str: 业务内容对应的 64 位小写 SHA-256 十六进制摘要。
    """

    # 只提取稳定业务内容，不包含 timestamp、nonce 和 HMAC 请求头。
    # mode="json" ：“生成 Python 字典时，把里面的值处理成将来能够转成 JSON 的样子。
    payload_data = ingest_request.model_dump(mode="json")

    canonical_json = json.dumps(
        payload_data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # SHA-256 处理字节，因此双方固定使用 UTF-8 编码
    payload_bytes = canonical_json.encode("utf-8")

    return hashlib.sha256(payload_bytes).hexdigest()

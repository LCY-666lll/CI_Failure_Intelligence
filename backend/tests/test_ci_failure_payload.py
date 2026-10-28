"""验证 CI 失败事件业务内容摘要协议。"""

from typing import Any

from ci_failure_intelligence.schemas.ci_ingest import CiFailureIngestRequest
from ci_failure_intelligence.service.ci_failure_payload import (
    calculate_ci_failure_payload_sha256,
)


def build_ingest_request() -> CiFailureIngestRequest:
    """构造一份固定的合法 CI 失败接入请求。"""

    payload: dict[str, Any] = {
        "repository": {
            "github_repository_id": 123456,
            "full_name": "LCY-666lll/CI_Failure_Intelligence",
        },
        "run": {
            "run_id": 987654,
            "run_attempt": 1,
            "workflow_name": "CI",
            "job_key": "test:0",
            "job_name": "test-python-3.12",
            "commit_sha": "a" * 40,
            "head_branch": "main",
        },
        "junit_xml": "<testsuite></testsuite>",
    }
    return CiFailureIngestRequest.model_validate(payload)


def test_payload_digest_matches_stable_protocol_value() -> None:
    """固定业务内容应得到协议约定的 64 位小写摘要。"""

    digest = calculate_ci_failure_payload_sha256(build_ingest_request())

    assert digest == (
        "c454bfbd0178438af58ff060dbc6f89121b9a17a0d55a38b275c155389e4a8f8"
    )


def test_equal_business_content_produces_equal_digest() -> None:
    """两个内容相同的模型实例应得到同一个业务摘要。"""

    first_request = build_ingest_request()
    second_request = CiFailureIngestRequest.model_validate(
        first_request.model_dump(mode="json")
    )

    assert calculate_ci_failure_payload_sha256(
        first_request
    ) == calculate_ci_failure_payload_sha256(second_request)


def test_changed_business_content_produces_different_digest() -> None:
    """JUnit 内容变化应产生不同摘要，以便识别同身份内容冲突。"""

    original_request = build_ingest_request()
    changed_payload = original_request.model_dump(mode="json")
    changed_payload["junit_xml"] = '<testsuite failures="1"></testsuite>'
    changed_request = CiFailureIngestRequest.model_validate(changed_payload)

    assert calculate_ci_failure_payload_sha256(
        original_request
    ) != calculate_ci_failure_payload_sha256(changed_request)

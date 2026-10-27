"""验证 CI 失败接入请求的数据契约。"""

from typing import Any

import pytest
from pydantic import ValidationError

from ci_failure_intelligence.schemas.ci_ingest import CiFailureIngestRequest


def build_valid_payload() -> dict[str, Any]:
    """构造一份满足当前接入契约的最小请求数据。"""

    return {
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


def test_valid_payload_builds_nested_models() -> None:
    """合法请求应生成结构明确的仓库和运行上下文模型。"""

    request = CiFailureIngestRequest.model_validate(build_valid_payload())

    assert request.repository.github_repository_id == 123456
    assert request.run.run_id == 987654
    assert request.junit_xml == "<testsuite></testsuite>"


def test_string_run_id_is_rejected() -> None:
    """字符串形式的运行编号不应被自动转换成整数。"""

    payload = build_valid_payload()
    payload["run"]["run_id"] = "987654"

    with pytest.raises(ValidationError) as captured_error:
        CiFailureIngestRequest.model_validate(payload)

    first_error = captured_error.value.errors()[0]
    assert first_error["loc"] == ("run", "run_id")
    assert first_error["type"] == "int_type"


def test_unknown_run_field_is_rejected() -> None:
    """拼错或未声明的运行字段不应被静默忽略。"""

    payload = build_valid_payload()
    payload["run"]["run_attemp"] = 1

    with pytest.raises(ValidationError) as captured_error:
        CiFailureIngestRequest.model_validate(payload)

    first_error = captured_error.value.errors()[0]
    assert first_error["loc"] == ("run", "run_attemp")
    assert first_error["type"] == "extra_forbidden"


def test_repository_full_name_requires_owner_and_repository() -> None:
    """仓库全名必须同时包含所有者和仓库名称。"""

    payload = build_valid_payload()
    payload["repository"]["full_name"] = "repository-only"

    with pytest.raises(ValidationError):
        CiFailureIngestRequest.model_validate(payload)


def test_commit_sha_requires_forty_hexadecimal_characters() -> None:
    """提交标识必须是当前 GitHub 使用的完整 SHA 格式。"""

    payload = build_valid_payload()
    payload["run"]["commit_sha"] = "not-a-valid-sha"

    with pytest.raises(ValidationError):
        CiFailureIngestRequest.model_validate(payload)


def test_empty_junit_xml_is_rejected() -> None:
    """空字符串不能作为可处理的 JUnit XML 报告。"""

    payload = build_valid_payload()
    payload["junit_xml"] = ""

    with pytest.raises(ValidationError):
        CiFailureIngestRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "field_name", "invalid_value"),
    [
        (
            "repository",
            "github_repository_id",
            9_223_372_036_854_775_808,
        ),
        ("run", "run_id", 9_223_372_036_854_775_808),
        ("run", "run_attempt", 2_147_483_648),
    ],
)
def test_database_integer_overflow_is_rejected(
    section: str,
    field_name: str,
    invalid_value: int,
) -> None:
    """请求整数不得超过对应 PostgreSQL 字段可以存储的范围。"""

    payload = build_valid_payload()
    payload[section][field_name] = invalid_value

    with pytest.raises(ValidationError):
        CiFailureIngestRequest.model_validate(payload)

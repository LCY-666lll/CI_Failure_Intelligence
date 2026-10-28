"""验证CI失败发送脚本的配置、协议、重试和命令行行为。"""

import hashlib
import hmac
import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Self
from urllib.error import HTTPError, URLError
from urllib.request import Request
from uuid import UUID

import pytest
from ci_failure_intelligence.schemas.ci_ingest import CiFailureIngestRequest

import scripts.ci.send_ci_failure as sender

TEST_SECRET_TEXT = "test-only-secret-with-at-least-32-bytes"
TEST_SECRET = TEST_SECRET_TEXT.encode("utf-8")
TEST_REPORT = '<testsuite name="backend"></testsuite>'
FIXED_TIMESTAMP = 1_792_627_200
FIXED_NONCE = UUID("550e8400-e29b-41d4-a716-446655440000")


class FakeResponse:
    """模拟urllib成功响应，并保留真实的受限读取行为。"""

    def __init__(self, *, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            return self.body

        return self.body[:amount]


class FakeOpener:
    """记录发送端创建的请求，并返回测试指定的响应或异常。"""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def open(self, request: Request, timeout: float) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)

        if self.error is not None:
            raise self.error

        if self.response is None:
            raise AssertionError("FakeOpener缺少测试响应")

        return self.response


@pytest.fixture
def sender_config(tmp_path: Path) -> sender.SenderConfig:
    """创建包含测试报告路径的固定发送端配置。"""

    report_path = tmp_path / "backend-junit.xml"
    report_path.write_text(TEST_REPORT, encoding="utf-8")

    return sender.SenderConfig(
        ingest_url="http://127.0.0.1:8001/api/v1/ci-runs/ingest",
        hmac_secret=TEST_SECRET,
        junit_xml_path=report_path,
        github_repository_id=123456,
        github_repository="LCY-666lll/CI_Failure_Intelligence",
        github_run_id=987654,
        github_run_attempt=1,
        github_workflow_name="Backend CI",
        github_job_key="backend-tests",
        github_job_name="backend-tests-python-3.12",
        github_commit_sha="a" * 40,
        github_head_branch="main",
    )


@pytest.fixture
def sender_environment(sender_config: sender.SenderConfig) -> dict[str, str]:
    """把固定配置转换成GitHub Actions会提供的字符串环境变量。"""

    return {
        "CFI_INGEST_URL": sender_config.ingest_url,
        "CFI_INGEST_HMAC_SECRET": TEST_SECRET_TEXT,
        "CFI_JUNIT_XML_PATH": str(sender_config.junit_xml_path),
        "CFI_GITHUB_REPOSITORY_ID": str(sender_config.github_repository_id),
        "CFI_GITHUB_REPOSITORY": sender_config.github_repository,
        "CFI_GITHUB_RUN_ID": str(sender_config.github_run_id),
        "CFI_GITHUB_RUN_ATTEMPT": str(sender_config.github_run_attempt),
        "CFI_GITHUB_WORKFLOW_NAME": sender_config.github_workflow_name,
        "CFI_GITHUB_JOB_KEY": sender_config.github_job_key,
        "CFI_GITHUB_JOB_NAME": sender_config.github_job_name,
        "CFI_GITHUB_COMMIT_SHA": sender_config.github_commit_sha,
        "CFI_GITHUB_HEAD_BRANCH": sender_config.github_head_branch,
    }


def create_success_body(config: sender.SenderConfig) -> bytes:
    """创建与当前发送配置身份一致的 accepted 响应。"""

    return json.dumps(
        {
            "status": "accepted",
            "run_id": config.github_run_id,
            "run_attempt": config.github_run_attempt,
            "job_key": config.github_job_key,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_load_sender_config_converts_environment_types(
    sender_environment: dict[str, str],
) -> None:
    config = sender.load_sender_config(sender_environment)

    assert config.github_repository_id == 123456
    assert config.github_run_id == 987654
    assert config.github_run_attempt == 1
    assert config.hmac_secret == TEST_SECRET
    assert TEST_SECRET_TEXT not in repr(config)


@pytest.mark.parametrize(
    ("environment_name", "invalid_value"),
    [
        ("CFI_GITHUB_RUN_ID", "0"),
        ("CFI_GITHUB_RUN_ID", "0987654"),
        ("CFI_GITHUB_RUN_ATTEMPT", "one"),
    ],
)
def test_load_sender_config_rejects_invalid_integer_inputs(
    sender_environment: dict[str, str],
    environment_name: str,
    invalid_value: str,
) -> None:
    sender_environment[environment_name] = invalid_value

    with pytest.raises(sender.SenderConfigurationError):
        sender.load_sender_config(sender_environment)


def test_read_junit_xml_accepts_utf8_and_rejects_invalid_inputs(
    tmp_path: Path,
) -> None:
    valid_report = tmp_path / "valid.xml"
    valid_report.write_text('<testsuite name="中文"></testsuite>', encoding="utf-8")
    assert "中文" in sender.read_junit_xml(valid_report)

    empty_report = tmp_path / "empty.xml"
    empty_report.write_bytes(b"")
    with pytest.raises(sender.SenderReportError):
        sender.read_junit_xml(empty_report)

    invalid_utf8_report = tmp_path / "invalid.xml"
    invalid_utf8_report.write_bytes(b"\xff")
    with pytest.raises(sender.SenderReportError):
        sender.read_junit_xml(invalid_utf8_report)


def test_build_request_body_is_stable_and_matches_server_contract(
    sender_config: sender.SenderConfig,
) -> None:
    first_body = sender.build_request_body(sender_config, TEST_REPORT)
    second_body = sender.build_request_body(sender_config, TEST_REPORT)

    assert first_body == second_body

    request = CiFailureIngestRequest.model_validate_json(first_body)
    assert request.repository.github_repository_id == 123456
    assert request.run.run_id == 987654
    assert request.junit_xml == TEST_REPORT


def test_create_security_headers_matches_v1_protocol_and_refreshes_nonce() -> None:
    raw_body = b'{"message":"failed"}'
    fixed_headers = sender.create_security_headers(
        TEST_SECRET,
        raw_body,
        timestamp_seconds=FIXED_TIMESTAMP,
        nonce=FIXED_NONCE,
    )
    signing_message = (
        b"v1."
        + str(FIXED_TIMESTAMP).encode("ascii")
        + b"."
        + str(FIXED_NONCE).encode("ascii")
        + b"."
        + raw_body
    )
    expected_digest = hmac.new(
        TEST_SECRET,
        signing_message,
        hashlib.sha256,
    ).hexdigest()

    assert fixed_headers["X-CFI-Signature-256"] == f"v1={expected_digest}"

    first_headers = sender.create_security_headers(TEST_SECRET, raw_body)
    second_headers = sender.create_security_headers(TEST_SECRET, raw_body)
    assert first_headers["X-CFI-Nonce"] != second_headers["X-CFI-Nonce"]
    assert first_headers["X-CFI-Signature-256"] != second_headers["X-CFI-Signature-256"]


@pytest.mark.parametrize(
    "response_body",
    [
        b"not-json",
        b"[]",
        b'{"status":"accepted"}',
        b'{"status":"validated","run_id":987654,"run_attempt":1,"job_key":"backend-tests"}',
        b'{"status":"accepted","run_id":true,"run_attempt":1,"job_key":"backend-tests"}',
        b'{"status":"accepted","run_id":111,"run_attempt":1,"job_key":"backend-tests"}',
    ],
)
def test_validate_success_response_rejects_untrusted_responses(
    sender_config: sender.SenderConfig,
    response_body: bytes,
) -> None:
    with pytest.raises(sender.SenderResponseError):
        sender.validate_success_response(response_body, sender_config)


def test_send_single_attempt_sends_signed_original_body(
    sender_config: sender.SenderConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(
        status=202,
        body=create_success_body(sender_config),
    )
    opener = FakeOpener(response=response)
    monkeypatch.setattr(sender, "HTTP_OPENER", opener)
    raw_body = sender.build_request_body(sender_config, TEST_REPORT)

    sender.send_single_attempt(sender_config, raw_body)

    assert opener.timeouts == [sender.HTTP_REQUEST_TIMEOUT_SECONDS]
    assert len(opener.requests) == 1
    request = opener.requests[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert request.get_method() == "POST"
    assert request.data == raw_body
    assert headers["content-type"] == "application/json; charset=utf-8"

    signing_message = (
        b"v1."
        + headers["x-cfi-timestamp"].encode("ascii")
        + b"."
        + headers["x-cfi-nonce"].encode("ascii")
        + b"."
        + raw_body
    )
    expected_digest = hmac.new(
        TEST_SECRET,
        signing_message,
        hashlib.sha256,
    ).hexdigest()
    assert headers["x-cfi-signature-256"] == f"v1={expected_digest}"


def test_send_single_attempt_classifies_http_and_network_errors(
    sender_config: sender.SenderConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_error = HTTPError(
        sender_config.ingest_url,
        503,
        "Service Unavailable",
        Message(),
        BytesIO(b"{}"),
    )
    monkeypatch.setattr(
        sender,
        "HTTP_OPENER",
        FakeOpener(error=http_error),
    )
    with pytest.raises(sender.SenderHttpStatusError) as http_error_info:
        sender.send_single_attempt(sender_config, b"{}")
    assert http_error_info.value.status_code == 503

    monkeypatch.setattr(
        sender,
        "HTTP_OPENER",
        FakeOpener(error=URLError("connection refused")),
    )
    with pytest.raises(sender.SenderNetworkError):
        sender.send_single_attempt(sender_config, b"{}")


def test_send_single_attempt_rejects_oversized_success_response(
    sender_config: sender.SenderConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sender,
        "HTTP_OPENER",
        FakeOpener(
            response=FakeResponse(
                status=202,
                body=b"x" * (sender.MAX_RESPONSE_BODY_BYTES + 1),
            )
        ),
    )

    with pytest.raises(sender.SenderResponseError, match="超过允许的大小"):
        sender.send_single_attempt(sender_config, b"{}")


def test_send_with_retries_waits_two_then_five_seconds(
    sender_config: sender.SenderConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes: list[BaseException | None] = [
        sender.SenderHttpStatusError(503),
        sender.SenderHttpStatusError(503),
        None,
    ]
    attempts = 0
    waits: list[float] = []

    def fake_send_single_attempt(
        config: sender.SenderConfig,
        raw_body: bytes,
    ) -> None:
        nonlocal attempts
        assert config is sender_config
        assert raw_body == b"fixed-body"
        outcome = outcomes[attempts]
        attempts += 1

        if outcome is not None:
            raise outcome

    monkeypatch.setattr(sender, "send_single_attempt", fake_send_single_attempt)

    sender.send_with_retries(
        sender_config,
        b"fixed-body",
        sleep=waits.append,
    )

    assert attempts == 3
    assert waits == [2.0, 5.0]


@pytest.mark.parametrize(
    "error",
    [
        sender.SenderHttpStatusError(401),
        sender.SenderResponseError("invalid response"),
    ],
)
def test_send_with_retries_stops_immediately_for_permanent_errors(
    sender_config: sender.SenderConfig,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    attempts = 0
    waits: list[float] = []

    def fail_once(
        config: sender.SenderConfig,
        raw_body: bytes,
    ) -> None:
        nonlocal attempts
        attempts += 1
        raise error

    monkeypatch.setattr(sender, "send_single_attempt", fail_once)

    with pytest.raises(type(error)):
        sender.send_with_retries(
            sender_config,
            b"fixed-body",
            sleep=waits.append,
        )

    assert attempts == 1
    assert waits == []


def test_send_with_retries_stops_after_three_network_failures(
    sender_config: sender.SenderConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    waits: list[float] = []

    def always_fail(
        config: sender.SenderConfig,
        raw_body: bytes,
    ) -> None:
        nonlocal attempts
        attempts += 1
        raise sender.SenderNetworkError("network unavailable")

    monkeypatch.setattr(sender, "send_single_attempt", always_fail)

    with pytest.raises(sender.SenderNetworkError):
        sender.send_with_retries(
            sender_config,
            b"fixed-body",
            sleep=waits.append,
        )

    assert attempts == 3
    assert waits == [2.0, 5.0]


def test_main_returns_zero_and_does_not_log_sensitive_content(
    sender_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sender,
        "send_with_retries",
        lambda config, raw_body: None,
    )

    exit_code = sender.main(sender_environment)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "原始 CI 失败事件已保存" in captured.out
    assert captured.err == ""
    assert TEST_SECRET_TEXT not in captured.out
    assert TEST_REPORT not in captured.out


def test_main_returns_one_for_configuration_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = sender.main({})
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "配置错误" in captured.err

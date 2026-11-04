"""构造发送给外部模型的有限、脱敏 CI 失败证据。"""

import re

from ci_failure_intelligence.schemas.ai_evidence import (
    AiEvidencePack,
    AiFailureEvidence,
)
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
    CiFailureQueryResponse,
)

# 单次模型诊断最多选择前 20 个 failure/error 节点。
MAX_FAILURES_PER_EVIDENCE_PACK = 20
# 按最终 JSON 的 UTF-8 字节数限制整个模型证据包。
MAX_EVIDENCE_PACK_BYTES = 32 * 1024

MAX_SUITE_NAME_CHARACTERS = 500
MAX_CLASSNAME_CHARACTERS = 500
MAX_TEST_NAME_CHARACTERS = 1000
MAX_MESSAGE_CHARACTERS = 2000
MAX_DETAILS_CHARACTERS = 4000

REDACTED_VALUE = "[REDACTED]"
TRUNCATION_MARKER = "\n...[TRUNCATED]"

# 保留 Authorization 的字段名称和认证类型，只替换真实凭据。
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+"
)

# 处理日志中常见的 key=value 和 key: value 密钥形式。
_NAMED_SECRET_PATTERN = re.compile(
    r"(?i)\b("
    r"api[_-]?key|"
    r"access[_-]?token|"
    r"token|"
    r"password|"
    r"secret"
    r")\s*[:=]\s*[\"']?[^\s,\"';]+[\"']?"
)

# 处理没有字段名、直接出现在 traceback 或日志中的常见令牌。
_TOKEN_PREFIX_PATTERN = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"github_pat_[A-Za-z0-9_]+|"
    r"gh[pousr]_[A-Za-z0-9]+"
    r")\b"
)


class AiEvidenceBuildError(RuntimeError):
    """表示当前 CI 事件无法构造可供模型使用的证据包。"""


class AiEvidenceNotReadyError(AiEvidenceBuildError):
    """表示 CI 事件尚未成功完成 JUnit 解析。"""

    def __init__(self, event_id: int, parse_status: str) -> None:
        """记录无法诊断的事件及其当前解析状态。

        Args:
            event_id: 当前 CI 失败事件的数据库主键。
            parse_status: 事件当前的 pending、parsed 或 failed 状态。
        """
        self.event_id = event_id
        self.parse_status = parse_status
        super().__init__(
            "CI 失败事件尚未形成可诊断证据："
            f"event_id={event_id}, parse_status={parse_status}"
        )


class AiEvidenceEmptyError(AiEvidenceBuildError):
    """表示事件解析成功，但没有 failure/error 可供诊断。"""

    def __init__(self, event_id: int) -> None:
        """记录没有失败证据的事件。

        Args:
            event_id: 当前 CI 失败事件的数据库主键。
        """
        self.event_id = event_id
        super().__init__(f"CI 失败事件没有可诊断证据：event_id={event_id}")


class AiEvidencePackTooLargeError(AiEvidenceBuildError):
    """表示第一条有限证据仍无法放入 32 KiB 证据包。"""

    def __init__(self, event_id: int, actual_bytes: int) -> None:
        """记录无法满足总字节限制的事件。

        Args:
            event_id: 当前 CI 失败事件的数据库主键。
            actual_bytes: 只放入第一条有限证据时的最终 JSON UTF-8 字节数。
        """
        self.event_id = event_id
        self.actual_bytes = actual_bytes
        super().__init__(
            "单条 CI 失败证据超过模型输入包上限："
            f"event_id={event_id}, actual_bytes={actual_bytes}, "
            f"max_bytes={MAX_EVIDENCE_PACK_BYTES}"
        )


def redact_sensitive_text(value: str) -> str:
    """替换失败证据中第一版可识别的常见凭据。

    Args:
        value: 从数据库失败明细读取的原始文本。该文本来自不可信的
            JUnit failure/error 内容，尚未经过模型输入脱敏。

    Returns:
        str: 保留排查上下文、但将已识别凭据替换为固定占位符的文本。
            原始数据库内容不会被修改。
    """

    redacted_value = _AUTHORIZATION_PATTERN.sub(
        rf"\1{REDACTED_VALUE}",
        value,
    )
    redacted_value = _NAMED_SECRET_PATTERN.sub(
        rf"\1={REDACTED_VALUE}",
        redacted_value,
    )
    return _TOKEN_PREFIX_PATTERN.sub(
        REDACTED_VALUE,
        redacted_value,
    )


def sanitize_evidence_text(
    value: str | None,
    *,
    max_characters: int,
) -> tuple[str | None, bool]:
    """脱敏并按字符上限截断一段模型证据文本。

    Args:
        value: 原始证据文本；可选 XML 属性缺失时为 None。
        max_characters: 当前字段允许进入 evidence pack 的最大字符数。
            调用方必须提供大于截断标记长度的正整数。

    Returns:
        tuple[str | None, bool]: 第一个元素是脱敏、必要时截断后的文本；
            第二个元素表示是否因字符上限发生过截断。脱敏本身不算截断。

    Raises:
        ValueError: max_characters 不足以容纳截断标记。
    """

    if value is None:
        return None, False

    if max_characters <= len(TRUNCATION_MARKER):
        raise ValueError("max_characters 必须大于截断标记长度")

    redacted_value = redact_sensitive_text(value)

    if len(redacted_value) <= max_characters:
        return redacted_value, False

    preserved_characters = max_characters - len(TRUNCATION_MARKER)
    truncated_value = redacted_value[:preserved_characters] + TRUNCATION_MARKER

    return truncated_value, True


def build_ai_failure_evidence(detail: CiFailureDetailResponse) -> AiFailureEvidence:
    """将一条数据库失败明细转换为有限、脱敏的模型证据。

    Args:
        detail: 已由失败查询响应模型校验的一条 failure/error 明细，
            包含原 XML 位置、测试身份和原始错误文本。

    Returns:
        AiFailureEvidence: 使用 XML 位置生成稳定证据编号，并完成所有
            文本字段脱敏和字符截断后的模型证据。
    """

    suite_name, suite_name_truncated = sanitize_evidence_text(
        detail.suite_name, max_characters=MAX_SUITE_NAME_CHARACTERS
    )
    classname, classname_truncated = sanitize_evidence_text(
        detail.classname,
        max_characters=MAX_CLASSNAME_CHARACTERS,
    )
    test_name, test_name_truncated = sanitize_evidence_text(
        detail.test_name,
        max_characters=MAX_TEST_NAME_CHARACTERS,
    )
    message, message_truncated = sanitize_evidence_text(
        detail.message,
        max_characters=MAX_MESSAGE_CHARACTERS,
    )
    details, details_truncated = sanitize_evidence_text(
        detail.details,
        max_characters=MAX_DETAILS_CHARACTERS,
    )
    # 输入模型已经保证 test_name 和 details 不是 None。
    if test_name is None or details is None:
        raise ValueError("必填失败证据文本不能为 None")
    # 一组条件中有一个是 True，结果就是 True；全部是 False，结果才是 False
    content_truncated = any(
        (
            suite_name_truncated,
            classname_truncated,
            test_name_truncated,
            message_truncated,
            details_truncated,
        )
    )
    return AiFailureEvidence(
        evidence_id=(f"failure:{detail.testcase_index}:{detail.issue_index}"),
        testcase_index=detail.testcase_index,
        issue_index=detail.issue_index,
        suite_name=suite_name,
        classname=classname,
        test_name=test_name,
        issue_type=detail.issue_type,
        message=message,
        details=details,
        content_truncated=content_truncated,
    )


def _create_ai_evidence_pack(
    event: CiFailureQueryResponse,
    failures: list[AiFailureEvidence],
) -> AiEvidencePack:
    """使用已经通过单条处理的失败证据创建候选证据包。

    Args:
        event: 已由查询响应模型校验的 CI 事件和全部结构化失败明细。
        failures: 已完成脱敏和字段限长、准备接受总字节检查的证据。

    Returns:
        AiEvidencePack: 计数和截断语义与当前候选证据一致的证据包。
    """

    total_failure_count = len(event.failures)

    # 少选了失败节点，或者任一文本字段被截断，都表示模型没有看到完整证据。
    truncated = len(failures) < total_failure_count or any(
        failure.content_truncated for failure in failures
    )

    return AiEvidencePack(
        event_id=event.event_id,
        repository_full_name=event.repository_full_name,
        run_id=event.run_id,
        run_attempt=event.run_attempt,
        job_key=event.job_key,
        workflow_name=event.workflow_name,
        job_name=event.job_name,
        commit_sha=event.commit_sha,
        head_branch=event.head_branch,
        total_failure_count=total_failure_count,
        included_failure_count=len(failures),
        truncated=truncated,
        failures=failures,
    )


def build_ai_evidence_pack(
    event: CiFailureQueryResponse,
) -> AiEvidencePack:
    """为一次已解析 CI 事件构造有限、脱敏的完整模型证据包。

    Args:
        event: 查询服务返回的 CI 事件。调用前已通过响应模型校验，
            但可能仍处于 pending、failed 或合法零失败状态。

    Returns:
        AiEvidencePack: 按 XML 位置稳定排序，最多包含 20 条失败，
            且最终 JSON UTF-8 大小不超过 32 KiB 的模型证据包。

    Raises:
        AiEvidenceNotReadyError: 事件尚未成功解析。
        AiEvidenceEmptyError: 事件解析成功但没有 failure/error。
        AiEvidencePackTooLargeError: 第一条有限证据仍超过整包字节上限。
    """

    if event.parse_status != "parsed":
        raise AiEvidenceNotReadyError(
            event_id=event.event_id,
            parse_status=event.parse_status,
        )

    if not event.failures:
        raise AiEvidenceEmptyError(event.event_id)

    # 即使调用方未来改变数据库查询，也始终按原 XML 位置稳定选择前 20 条。
    ordered_details = sorted(
        event.failures,
        key=lambda detail: (
            # 拿每条 detail 的 testcase_index 作为排序依据
            detail.testcase_index,
            # 如果 testcase_index 一样，再按照 issue_index 排序
            detail.issue_index,
        ),
    )

    # 保存已经通过检查的失败证据
    included_failures: list[AiFailureEvidence] = []
    accepted_pack: AiEvidencePack | None = None

    for detail in ordered_details[:MAX_FAILURES_PER_EVIDENCE_PACK]:
        new_evidence = build_ai_failure_evidence(detail)

        # 先创建候选列表，不在字节检查通过前修改已接纳的证据。
        candidate_failures = [
            # 可迭代对象解包
            *included_failures,
            new_evidence,
        ]
        candidate_pack = _create_ai_evidence_pack(
            event,
            candidate_failures,
        )
        candidate_bytes = len(candidate_pack.model_dump_json().encode("utf-8"))

        if candidate_bytes > MAX_EVIDENCE_PACK_BYTES:
            if accepted_pack is None:
                raise AiEvidencePackTooLargeError(
                    event_id=event.event_id,
                    actual_bytes=candidate_bytes,
                )

            # 保留之前已经通过字节检查的证据，不加入当前超限节点。
            break

        included_failures = candidate_failures
        accepted_pack = candidate_pack

    # 非空事件至少会尝试第一条；该分支只防止未来修改破坏这个不变量。
    if accepted_pack is None:
        raise AiEvidencePackTooLargeError(
            event_id=event.event_id,
            actual_bytes=0,
        )

    return accepted_pack

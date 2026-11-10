"""构造发送给外部模型的有限、脱敏 CI 失败证据。"""

import json
import re
from typing import Literal

from ci_failure_intelligence.schemas.ai_evidence import (
    AiEvidencePack,
    AiFailureEvidence,
    AiHistoricalFailureEvidence,
)
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
    CiFailureQueryResponse,
)
from ci_failure_intelligence.schemas.failure_history import (
    HistoricalFailureResponse,
)

# 单次模型诊断最多选择前 20 个 failure/error 节点。
MAX_FAILURES_PER_EVIDENCE_PACK = 20
# 历史只使用检索服务给出的有序前缀，不挤占当前失败的位置。
MAX_HISTORY_PER_EVIDENCE_PACK = 5
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


def serialize_ai_evidence_pack(evidence_pack: AiEvidencePack) -> str:
    """按同一规则序列化预算检查、Prompt和缓存使用的证据包。

    Args:
        evidence_pack: 已通过字段和内部一致性校验的候选或最终证据包。

    Returns:
        str: 键排序、紧凑且保留Unicode的JSON文本。
            候选包仍须由构造函数检查UTF-8字节大小。
    """
    return json.dumps(
        evidence_pack.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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


def build_ai_historical_failure_evidence(
    history: HistoricalFailureResponse,
) -> AiHistoricalFailureEvidence:
    """把已核对的历史失败转换成有限、脱敏的AI证据。

    Args:
        history: 检索服务已回查PostgreSQL并核对来源的历史失败。

    Returns:
        AiHistoricalFailureEvidence: 经过现有脱敏和单条截断处理，
            并携带历史编号、运行来源和相似分数的证据。

    Raises:
        ValidationError: 转换后的字段不符合AI证据契约。
    """

    # 复用现有错误文本处理，不另写一套脱敏和截断规则。
    failure = build_ai_failure_evidence(history.failure)

    return AiHistoricalFailureEvidence.model_validate(
        {
            # **:就是把字典里的键值对全部展开，放进新字典
            **failure.model_dump(),
            # 覆盖原来的failure编号，明确指向历史事件及错误位置。
            "evidence_id": (
                f"history:{history.event_id}:"
                f"{failure.testcase_index}:{failure.issue_index}"
            ),
            "event_id": history.event_id,
            "repository_full_name": history.repository_full_name,
            "run_id": history.run_id,
            "run_attempt": history.run_attempt,
            "job_key": history.job_key,
            "similarity_score": history.similarity_score,
        }
    )


def _create_ai_evidence_pack(
    event: CiFailureQueryResponse,
    failures: list[AiFailureEvidence],
    *,
    history: list[AiHistoricalFailureEvidence] | None = None,
    total_history_count: int = 0,
    retrieval_status: Literal["available", "degraded", "disabled"] = "disabled",
    embedding_model: str | None = None,
) -> AiEvidencePack:
    """组装等待接受总字节检查的候选证据包。

    Args:
        event: 已由查询响应模型校验的 CI 事件和全部结构化失败明细。
        failures: 已完成脱敏和字段限长、准备接受总字节检查的证据。
        history: 已完成单条转换、暂时选入的历史证据。
        total_history_count: 本次检索提供的历史数量，不是数据库历史总量。
        retrieval_status: 本次检索正常、降级或明确关闭的状态。
        embedding_model: 启用检索时配置的向量模型名称。

    Returns:
        AiEvidencePack: 计数、截断标记和检索上下文一致的候选包。

    Raises:
        ValidationError: 证据或检索上下文不符合整包契约。
    """

    selected_history = history if history is not None else []
    total_failure_count = len(event.failures)

    # 少选了失败节点，或者任一文本字段被截断，都表示模型没有看到完整证据。
    truncated = len(failures) < total_failure_count or any(
        failure.content_truncated for failure in failures
    )
    # 即使历史一条都没放下，也要声明本次检索提供的内容被省略了。
    history_truncated = len(selected_history) < total_history_count or any(
        item.content_truncated for item in selected_history
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
        history=selected_history,
        history_truncated=history_truncated,
        retrieval_status=retrieval_status,
        embedding_model=embedding_model,
    )


def build_ai_evidence_pack(
    event: CiFailureQueryResponse,
    *,
    history: list[HistoricalFailureResponse] | None = None,
    retrieval_status: Literal["available", "degraded", "disabled"] = "disabled",
    embedding_model: str | None = None,
) -> AiEvidencePack:
    """为一次已解析 CI 事件构造有限、脱敏的完整模型证据包。

    Args:
        event: 查询服务返回的 CI 事件。调用前已通过响应模型校验，
            但可能仍处于 pending、failed 或合法零失败状态。
        history: 检索服务已核对、按相似分数排序的历史；省略时为空。
        retrieval_status: 检索正常、降级或关闭；旧调用默认关闭。
        embedding_model: 启用或降级时记录配置中的向量模型名称。

    Returns:
        AiEvidencePack: 当前最多20条、历史最多5条；当前优先，
            两者及检索上下文合计的最终JSON UTF-8不超过32KiB。

    Raises:
        AiEvidenceNotReadyError: 事件尚未成功解析。
        AiEvidenceEmptyError: 事件解析成功但没有 failure/error。
        AiEvidencePackTooLargeError: 第一条有限证据仍超过整包字节上限。
        ValueError: 未正常完成检索却提供了历史内容。
        ValidationError: 检索上下文或历史来源不符合证据契约。
    """

    if event.parse_status != "parsed":
        raise AiEvidenceNotReadyError(
            event_id=event.event_id,
            parse_status=event.parse_status,
        )

    if not event.failures:
        raise AiEvidenceEmptyError(event.event_id)

    history_items = history if history is not None else []
    if retrieval_status != "available" and history_items:
        raise ValueError("检索未正常完成时不能提供历史证据")

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
            total_history_count=len(history_items),
            retrieval_status=retrieval_status,
            embedding_model=embedding_model,
        )
        # 元数据从第一条当前证据起就计入；与Prompt和缓存使用同一JSON。
        candidate_bytes = len(
            serialize_ai_evidence_pack(candidate_pack).encode("utf-8")
        )

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

    # 当前证据选择结束后，再使用剩余预算接纳有序历史前缀。
    included_history: list[AiHistoricalFailureEvidence] = []
    for item in history_items[:MAX_HISTORY_PER_EVIDENCE_PACK]:
        new_history = build_ai_historical_failure_evidence(item)
        candidate_history = [*included_history, new_history]
        candidate_pack = _create_ai_evidence_pack(
            event,
            included_failures,
            history=candidate_history,
            total_history_count=len(history_items),
            retrieval_status=retrieval_status,
            embedding_model=embedding_model,
        )
        candidate_bytes = len(
            serialize_ai_evidence_pack(candidate_pack).encode("utf-8")
        )
        if candidate_bytes > MAX_EVIDENCE_PACK_BYTES:
            # 保留此前通过检查的包，不删除当前失败来腾出历史空间。
            break
        included_history = candidate_history
        accepted_pack = candidate_pack

    return accepted_pack

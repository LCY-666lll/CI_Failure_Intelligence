"""JUnit XML 失败证据解析器:安全解析 pytest JUnit XML，并输出确定性的失败证据。"""

from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from typing import Literal
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException

# defusedxml 保留 ElementTree 的常用接口，
# 同时可以禁止 DTD、实体声明和外部资源引用。
from defusedxml.ElementTree import iterparse


class JunitParseErrorCode(StrEnum):
    """描述 JUnit XML 无法形成完整解析结果的稳定原因。"""

    INVALID_XML = "invalid_xml"
    UNSAFE_XML = "unsafe_xml"
    UNSUPPORTED_JUNIT = "unsupported_junit"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"


@dataclass(frozen=True, slots=True)
class JunitParserLimits:
    """限制单份 JUnit 报告能够产生的解析工作量。"""

    max_testcases: int
    max_issues: int
    max_detail_text_bytes: int


# 这三个值是 MVP 初始安全上限，不是性能测试结论。
DEFAULT_JUNIT_PARSER_LIMITS = JunitParserLimits(
    max_testcases=20_000,
    max_issues=5_000,
    max_detail_text_bytes=8 * 1024 * 1024,
)


@dataclass(frozen=True, slots=True)
class ParsedJunitIssue:
    """保存一个 failure 或 error 节点携带的确定性证据。"""

    testcase_index: int
    issue_index: int
    suite_name: str | None
    classname: str | None
    test_name: str
    issue_type: Literal["failure", "error"]
    message: str | None
    details: str


@dataclass(frozen=True, slots=True)
class JunitParseResult:
    """保存一次完整且未超限的 JUnit 解析结果。"""

    details: tuple[ParsedJunitIssue, ...]
    testcase_count: int
    issue_count: int
    detail_text_bytes: int


@dataclass(frozen=True, slots=True)
class _TestcaseContext:
    """暂存一个尚未完整结束的 testcase 所属位置。"""

    testcase_index: int
    suite_name: str | None


class JunitParsingError(ValueError):
    """表示报告本身无法形成可信、完整的解析结果。"""

    def __init__(
        self,
        code: JunitParseErrorCode,
        message: str,
    ) -> None:
        """记录稳定错误代码和供排查使用的安全摘要。

        Args:
            code: 调用层能够持久化和判断的稳定机器代码。
            message: 不包含完整 XML 的中文排查摘要。
        """
        self.code = code
        super().__init__(message)


def _local_name(tag: str) -> str:
    """移除可选 XML namespace，只保留节点的本地名称。

    Args:
        tag: ElementTree 返回的完整 XML 标签。

    Returns:
        str: 不包含 namespace 前缀的标签名称。
    """
    return tag.rsplit("}", maxsplit=1)[-1]


def _utf8_size(*values: str | None) -> int:
    """计算一条明细最终复制到数据库的 UTF-8 字节量，防止错误堆栈等文本太大。
    *values : 可以接受多个参数

    Args:
        values: 明细中的 suite、测试身份、消息和错误文本。

    Returns:
        int: 所有非空字符串编码后的字节数总和。
    """
    return sum(len(value.encode("utf-8")) for value in values if value is not None)


def _raise_resource_limit(resource_name: str) -> None:
    """产生统一的资源超限错误。

    Args:
        resource_name: 已经超过约定上限的资源名称。

    Raises:
        JunitParsingError: 始终抛出资源超限错误。
    """
    raise JunitParsingError(
        JunitParseErrorCode.RESOURCE_LIMIT_EXCEEDED,
        f"JUnit XML 超过允许的{resource_name}上限",
    )


def _extract_testcase_issues(
    element: Element,
    context: _TestcaseContext,
    current_issue_count: int,
    current_text_bytes: int,
    limits: JunitParserLimits,
) -> tuple[list[ParsedJunitIssue], int, int]:
    """提取一个完整 testcase 的直接 failure/error 子节点。

    Args:
        element: 已经接收到 end 事件的完整 testcase 元素。
        context: testcase 在原始 XML 中的位置和所属 suite。
        current_issue_count: 处理当前 testcase 前的全局错误节点数。
        current_text_bytes: 处理当前 testcase 前的明细文本字节数。
        limits: 本次解析使用的三个资源上限。

    Returns:
        tuple: 当前 testcase 的全部失败明细、更新后的全局错误节点数、
        以及更新后的全局明细文本字节数。

    Raises:
        JunitParsingError: testcase 缺少名称或解析结果超过资源上限。
    """
    test_name = element.get("name")

    if not test_name:
        raise JunitParsingError(
            JunitParseErrorCode.UNSUPPORTED_JUNIT,
            "JUnit testcase 缺少非空 name 属性",
        )

    classname = element.get("classname")
    testcase_details: list[ParsedJunitIssue] = []
    issue_index = 0

    # 这里只遍历 testcase 的直接子节点。
    # failure/error 内部即使还有其他 XML 标签，也只作为 details 文本处理。
    for child in element:
        child_name = _local_name(child.tag)

        if child_name == "failure":
            issue_type: Literal["failure", "error"] = "failure"
        elif child_name == "error":
            issue_type = "error"
        else:
            # passed 没有子节点；skipped 等其他节点不生成失败明细。
            continue

        current_issue_count += 1

        if current_issue_count > limits.max_issues:
            _raise_resource_limit("failure/error 节点数量")

        message = child.get("message")

        # itertext() 会按 XML 内部顺序取得节点自身及其后代的全部文本，
        # 因此嵌套在 failure 中的堆栈文本不会被漏掉。
        details = "".join(child.itertext())

        detail_text_bytes = _utf8_size(
            context.suite_name,
            classname,
            test_name,
            message,
            details,
        )
        current_text_bytes += detail_text_bytes

        if current_text_bytes > limits.max_detail_text_bytes:
            _raise_resource_limit("明细文本 UTF-8 字节量")

        # 创建失败记录
        testcase_details.append(
            ParsedJunitIssue(
                testcase_index=context.testcase_index,
                issue_index=issue_index,
                suite_name=context.suite_name,
                classname=classname,
                test_name=test_name,
                issue_type=issue_type,
                message=message,
                details=details,
            )
        )
        issue_index += 1

    return (
        testcase_details,
        current_issue_count,
        current_text_bytes,
    )


def parse_junit_xml(
    junit_xml: str,
    limits: JunitParserLimits = DEFAULT_JUNIT_PARSER_LIMITS,
) -> JunitParseResult:
    """把一份受支持的 JUnit XML 完整转换成失败证据。

    Args:
        junit_xml: 已经通过请求字符数限制并保存的 JUnit XML 原文。
        limits: 本次解析允许的 testcase、错误节点和明细文本上限。

    Returns:
        JunitParseResult: 完整解析后的失败明细和资源统计。

    Raises:
        JunitParsingError: XML 损坏、危险、不受支持或超过资源上限。
    """
    suite_names: list[str | None] = []

    # Element 对象不可直接作为稳定业务键，
    # 这里只在本次解析生命周期内用对象 id 找回 testcase 上下文。
    testcase_contexts: dict[int, _TestcaseContext] = {}

    parsed_details: list[ParsedJunitIssue] = []
    testcase_count = 0
    issue_count = 0
    detail_text_bytes = 0
    root_seen = False

    try:
        # 输入已经是 Python str。重新编码为 UTF-8 字节流后，
        # iterparse 可以像读取文件一样逐步产生 start/end 事件。
        xml_source = BytesIO(junit_xml.encode("utf-8"))

        events = iterparse(
            xml_source,
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )

        for event, element in events:
            tag_name = _local_name(element.tag)

            # 第一条 start 事件一定对应 XML 根节点。
            if not root_seen and event == "start":
                root_seen = True

                if tag_name not in {"testsuites", "testsuite"}:
                    raise JunitParsingError(
                        JunitParseErrorCode.UNSUPPORTED_JUNIT,
                        "XML 根节点不是受支持的 testsuites 或 testsuite",
                    )

            if event == "start":
                if tag_name == "testsuite":
                    # 栈最后一项永远表示当前最近一层 suite。
                    suite_names.append(element.get("name"))

                elif tag_name == "testcase":
                    testcase_index = testcase_count
                    testcase_count += 1

                    if testcase_count > limits.max_testcases:
                        _raise_resource_limit("testcase 数量")

                    testcase_contexts[id(element)] = _TestcaseContext(
                        testcase_index=testcase_index,
                        suite_name=(suite_names[-1] if suite_names else None),
                    )

                # start 事件只能确定标签和属性；
                # 节点文本尚未保证完整，因此不能在这里提取错误内容。
                continue

            if tag_name == "testcase":
                context = testcase_contexts.pop(id(element), None)

                if context is None:
                    raise JunitParsingError(
                        JunitParseErrorCode.UNSUPPORTED_JUNIT,
                        "JUnit testcase 结构无法确定",
                    )

                (
                    testcase_details,
                    issue_count,
                    detail_text_bytes,
                ) = _extract_testcase_issues(
                    element,
                    context,
                    issue_count,
                    detail_text_bytes,
                    limits,
                )
                parsed_details.extend(testcase_details)

                # 当前 testcase 已经转换成 Python 明细，
                # 清理 ElementTree 内容，避免所有 testcase 长期留在内存。
                element.clear()

            elif tag_name == "testsuite":
                if suite_names:
                    suite_names.pop()

                element.clear()

            elif not testcase_contexts:
                # testcase 外部的其他元数据节点不需要继续保留。
                element.clear()

        if not root_seen:
            raise JunitParsingError(
                JunitParseErrorCode.INVALID_XML,
                "JUnit XML 不包含根节点",
            )

    except JunitParsingError:
        # 这是本模块主动产生的稳定业务错误，不应被重新包装。
        raise
    except DefusedXmlException as error:
        raise JunitParsingError(
            JunitParseErrorCode.UNSAFE_XML,
            "JUnit XML 包含禁止的 DTD、实体或外部引用",
        ) from error
    except (ParseError, UnicodeEncodeError) as error:
        raise JunitParsingError(
            JunitParseErrorCode.INVALID_XML,
            "JUnit XML 语法或字符编码无效",
        ) from error

    return JunitParseResult(
        details=tuple(parsed_details),
        testcase_count=testcase_count,
        issue_count=issue_count,
        detail_text_bytes=detail_text_bytes,
    )

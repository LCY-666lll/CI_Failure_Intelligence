"""验证 JUnit XML 解析器只返回完整、可信且有限量的失败证据。"""

from pathlib import Path

import pytest
from ci_failure_intelligence.service.junit_parser import (
    DEFAULT_JUNIT_PARSER_LIMITS,
    JunitParseErrorCode,
    JunitParserLimits,
    JunitParsingError,
    ParsedJunitIssue,
    parse_junit_xml,
)

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "junit"


def read_junit_fixture(filename: str) -> str:
    """读取保持真实 pytest xunit2 结构的固定测试样本。"""
    return (FIXTURE_DIRECTORY / filename).read_text(encoding="utf-8")


def detail_text_bytes(*rows: tuple[str | None, ...]) -> int:
    """按生产契约累计每条明细实际复制的 UTF-8 字节数。"""
    return sum(
        len(value.encode("utf-8")) for row in rows for value in row if value is not None
    )


def test_default_junit_parser_limits_match_v1_contract() -> None:
    assert DEFAULT_JUNIT_PARSER_LIMITS == JunitParserLimits(
        max_testcases=20_000,
        max_issues=5_000,
        max_detail_text_bytes=8 * 1024 * 1024,
    )


def test_parsing_error_exposes_stable_code() -> None:
    error = JunitParsingError(
        JunitParseErrorCode.INVALID_XML,
        "JUnit XML 无效",
    )

    assert error.code is JunitParseErrorCode.INVALID_XML
    assert str(error) == "JUnit XML 无效"


def test_valid_report_without_failure_returns_empty_details() -> None:
    result = parse_junit_xml(read_junit_fixture("pytest_zero_failures.xml"))

    assert result.details == ()
    assert result.testcase_count == 2
    assert result.issue_count == 0
    assert result.detail_text_bytes == 0


def test_failure_node_is_extracted_with_testcase_identity() -> None:
    result = parse_junit_xml(read_junit_fixture("pytest_one_failure.xml"))

    assert result.testcase_count == 1
    assert result.issue_count == 1
    assert result.details[0] == ParsedJunitIssue(
        testcase_index=0,
        issue_index=0,
        suite_name="pytest",
        classname="tests.test_ci_ingest_api",
        test_name="test_create_event",
        issue_type="failure",
        message="assert 500 == 202",
        details="Traceback...\nassert 500 == 202",
    )


def test_all_testcases_and_each_direct_issue_receive_stable_indexes() -> None:
    xml = """<testsuites><testsuite name="backend">
    <testcase name="passed" />
    <testcase classname="tests.test_api" name="failed">
      <failure message="assertion">first</failure>
      <error message="teardown">second</error>
    </testcase>
    </testsuite></testsuites>"""

    result = parse_junit_xml(xml)

    assert [
        (item.testcase_index, item.issue_index, item.issue_type)
        for item in result.details
    ] == [
        (1, 0, "failure"),
        (1, 1, "error"),
    ]


def test_nearest_nested_suite_name_is_used() -> None:
    xml = """<testsuites><testsuite name="outer"><testsuite name="inner">
    <testcase name="failed"><failure>boom</failure></testcase>
    </testsuite></testsuite></testsuites>"""

    result = parse_junit_xml(xml)

    assert result.details[0].suite_name == "inner"


@pytest.mark.parametrize(
    ("xml", "expected_code"),
    [
        ("<testsuites><testsuite>", JunitParseErrorCode.INVALID_XML),
        ("<report />", JunitParseErrorCode.UNSUPPORTED_JUNIT),
        (
            "<!DOCTYPE testsuites [<!ENTITY x 'boom'>]><testsuites>&x;</testsuites>",
            JunitParseErrorCode.UNSAFE_XML,
        ),
    ],
)
def test_invalid_unsafe_and_unsupported_xml_are_classified(
    xml: str,
    expected_code: JunitParseErrorCode,
) -> None:
    with pytest.raises(JunitParsingError) as captured:
        parse_junit_xml(xml)

    assert captured.value.code is expected_code


def test_testcase_without_name_is_unsupported() -> None:
    with pytest.raises(JunitParsingError) as captured:
        parse_junit_xml(
            "<testsuite><testcase><failure>boom</failure></testcase></testsuite>"
        )

    assert captured.value.code is JunitParseErrorCode.UNSUPPORTED_JUNIT


def test_exact_resource_limits_are_allowed() -> None:
    xml = """<testsuite name="s">
    <testcase name="a"><failure message="m">x</failure></testcase>
    <testcase name="b"><error message="n">y</error></testcase>
    </testsuite>"""
    expected_bytes = detail_text_bytes(
        ("s", None, "a", "m", "x"),
        ("s", None, "b", "n", "y"),
    )

    result = parse_junit_xml(
        xml,
        JunitParserLimits(
            max_testcases=2,
            max_issues=2,
            max_detail_text_bytes=expected_bytes,
        ),
    )

    assert result.testcase_count == 2
    assert result.issue_count == 2
    assert result.detail_text_bytes == expected_bytes


@pytest.mark.parametrize(
    ("limits", "xml"),
    [
        (
            JunitParserLimits(1, 2, 100),
            "<testsuite><testcase name='a'/><testcase name='b'/></testsuite>",
        ),
        (
            JunitParserLimits(1, 1, 100),
            "<testsuite><testcase name='a'><failure/><error/></testcase></testsuite>",
        ),
        (
            JunitParserLimits(1, 1, 4),
            (
                "<testsuite><testcase name='a'>"
                "<failure message='m'>xyz</failure></testcase></testsuite>"
            ),
        ),
    ],
)
def test_exceeding_any_resource_limit_rejects_the_complete_result(
    limits: JunitParserLimits,
    xml: str,
) -> None:
    with pytest.raises(JunitParsingError) as captured:
        parse_junit_xml(xml, limits)

    assert captured.value.code is JunitParseErrorCode.RESOURCE_LIMIT_EXCEEDED

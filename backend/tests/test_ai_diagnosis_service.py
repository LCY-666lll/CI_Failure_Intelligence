"""使用假模型验证 AI 诊断服务的调用顺序和失败映射。"""

import asyncio
from typing import cast

import pytest
from ci_failure_intelligence.schemas.ai_diagnosis import (
    AiDiagnosis,
    AiRootCause,
)
from ci_failure_intelligence.schemas.ai_evidence import (
    AiEvidencePack,
    AiFailureEvidence,
)
from ci_failure_intelligence.service.ai_diagnosis import (
    AiDiagnosisProviderError,
    AiDiagnosisStructuredOutputError,
    generate_ai_diagnosis,
)
from ci_failure_intelligence.service.ai_diagnosis_validation import (
    AiDiagnosisEvidenceReferenceError,
)
from langchain_core.messages import AIMessage, BaseMessage
from langchain_deepseek import ChatDeepSeek


class FakeStructuredRunnable:
    """记录消息并返回预设结果，不执行任何网络请求。"""

    def __init__(
        self,
        *,
        response: object | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.received_messages: list[BaseMessage] | None = None

    async def ainvoke(self, messages: list[BaseMessage]) -> object:
        """模拟 LangChain Runnable 的异步调用边界。"""

        self.received_messages = messages

        if self.error is not None:
            raise self.error

        return self.response


class FakeChatModel:
    """记录结构化输出配置，并返回注入的假 Runnable。"""

    def __init__(self, runnable: FakeStructuredRunnable) -> None:
        self.runnable = runnable
        self.received_schema: object | None = None
        self.received_method: str | None = None
        self.received_include_raw: bool | None = None

    def with_structured_output(
        self,
        schema: object,
        *,
        method: str,
        include_raw: bool,
    ) -> FakeStructuredRunnable:
        """模拟 ChatDeepSeek 的结构化 Runnable 创建方法。"""

        self.received_schema = schema
        self.received_method = method
        self.received_include_raw = include_raw
        return self.runnable


def build_evidence_pack() -> AiEvidencePack:
    """构造只允许引用 failure:3:0 的测试证据包。"""

    failure = AiFailureEvidence(
        evidence_id="failure:3:0",
        testcase_index=3,
        issue_index=0,
        suite_name="backend",
        classname="tests.test_orders",
        test_name="test_create_order",
        issue_type="failure",
        message="expected 200 but received 500",
        details="AssertionError: expected 200 but received 500",
        content_truncated=False,
    )
    return AiEvidencePack(
        event_id=42,
        repository_full_name="owner/repository",
        run_id=1001,
        run_attempt=1,
        job_key="backend_tests",
        workflow_name="Backend CI",
        job_name="Backend tests (Python 3.12)",
        commit_sha="a" * 40,
        head_branch="main",
        total_failure_count=1,
        included_failure_count=1,
        truncated=False,
        failures=[failure],
    )


def build_diagnosis(
    *,
    evidence_id: str = "failure:3:0",
) -> AiDiagnosis:
    """构造一份已经通过 Pydantic 校验的模型诊断。"""

    return AiDiagnosis(
        summary="数据库连接失败导致测试无法完成",
        root_causes=[
            AiRootCause(
                title="PostgreSQL 服务不可用",
                explanation="失败证据显示数据库连接被拒绝",
                confidence="high",
                evidence_ids=[evidence_id],
            )
        ],
        recommended_actions=["检查 PostgreSQL 容器健康状态"],
        needs_more_information=False,
        missing_information=[],
    )


def build_raw_message(
    *,
    include_metadata: bool = True,
) -> AIMessage:
    """构造只供服务提取模型名和 token usage 的原始消息。"""

    if not include_metadata:
        return AIMessage(content="untrusted raw model content")

    return AIMessage(
        content="untrusted raw model content",
        response_metadata={"model_name": " deepseek-flash "},
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 80,
            "total_tokens": 200,
        },
    )


def as_chat_deepseek(fake_model: FakeChatModel) -> ChatDeepSeek:
    """仅为静态类型检查声明假对象满足当前测试调用边界。"""

    return cast(ChatDeepSeek, cast(object, fake_model))


def test_successful_diagnosis_uses_structured_output_and_metadata() -> None:
    """成功路径应配置 JSON mode、校验证据并返回有限调用元数据。"""

    diagnosis = build_diagnosis()
    runnable = FakeStructuredRunnable(
        response={
            "raw": build_raw_message(),
            "parsed": diagnosis,
            "parsing_error": None,
        }
    )
    fake_model = FakeChatModel(runnable)

    result = asyncio.run(
        generate_ai_diagnosis(
            as_chat_deepseek(fake_model),
            build_evidence_pack(),
        )
    )

    assert fake_model.received_schema is AiDiagnosis
    assert fake_model.received_method == "json_mode"
    assert fake_model.received_include_raw is True
    assert runnable.received_messages is not None
    assert len(runnable.received_messages) == 2
    assert result.event_id == 42
    assert result.diagnosis is diagnosis
    assert result.model_name == "deepseek-flash"
    assert result.input_tokens == 120
    assert result.output_tokens == 80
    assert result.total_tokens == 200
    assert not hasattr(result, "raw")


def test_missing_provider_metadata_does_not_discard_valid_diagnosis() -> None:
    """provider未返回可选元数据时，结构和引用合法的诊断仍应成功。"""

    diagnosis = build_diagnosis()
    runnable = FakeStructuredRunnable(
        response={
            "raw": build_raw_message(include_metadata=False),
            "parsed": diagnosis,
            "parsing_error": None,
        }
    )

    result = asyncio.run(
        generate_ai_diagnosis(
            as_chat_deepseek(FakeChatModel(runnable)),
            build_evidence_pack(),
        )
    )

    assert result.diagnosis is diagnosis
    assert result.model_name is None
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.total_tokens is None


def test_provider_exception_is_mapped_without_leaking_prompt() -> None:
    """模型网络故障应转换为带事件上下文的稳定项目异常。"""

    provider_error = TimeoutError("test-only provider timeout")
    runnable = FakeStructuredRunnable(error=provider_error)

    with pytest.raises(AiDiagnosisProviderError) as error_info:
        asyncio.run(
            generate_ai_diagnosis(
                as_chat_deepseek(FakeChatModel(runnable)),
                build_evidence_pack(),
            )
        )

    assert error_info.value.event_id == 42
    assert error_info.value.__cause__ is provider_error
    assert "test_create_order" not in str(error_info.value)


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        ("not-a-dictionary", "unexpected_result_type"),
        (
            {
                "raw": build_raw_message(),
                "parsed": None,
                "parsing_error": None,
            },
            "missing_or_invalid_parsed",
        ),
        (
            {
                "raw": None,
                "parsed": build_diagnosis(),
                "parsing_error": None,
            },
            "missing_or_invalid_raw",
        ),
    ],
)
def test_invalid_structured_result_is_rejected(
    response: object,
    expected_reason: str,
) -> None:
    """缺少结构化结果关键部分时不能产生伪成功诊断。"""

    runnable = FakeStructuredRunnable(response=response)

    with pytest.raises(AiDiagnosisStructuredOutputError) as error_info:
        asyncio.run(
            generate_ai_diagnosis(
                as_chat_deepseek(FakeChatModel(runnable)),
                build_evidence_pack(),
            )
        )

    assert error_info.value.event_id == 42
    assert error_info.value.reason == expected_reason


def test_parsing_error_is_preserved_as_exception_cause() -> None:
    """LangChain解析错误应转换为项目异常并保留内部异常链。"""

    parsing_error = ValueError("test-only invalid JSON")
    runnable = FakeStructuredRunnable(
        response={
            "raw": build_raw_message(),
            "parsed": None,
            "parsing_error": parsing_error,
        }
    )

    with pytest.raises(AiDiagnosisStructuredOutputError) as error_info:
        asyncio.run(
            generate_ai_diagnosis(
                as_chat_deepseek(FakeChatModel(runnable)),
                build_evidence_pack(),
            )
        )

    assert error_info.value.reason == "parsing_error"
    assert error_info.value.__cause__ is parsing_error


def test_unknown_evidence_reference_is_not_wrapped_as_provider_error() -> None:
    """虚构证据属于业务完整性错误，不应伪装成provider故障。"""

    runnable = FakeStructuredRunnable(
        response={
            "raw": build_raw_message(),
            "parsed": build_diagnosis(evidence_id="failure:999:0"),
            "parsing_error": None,
        }
    )

    with pytest.raises(AiDiagnosisEvidenceReferenceError) as error_info:
        asyncio.run(
            generate_ai_diagnosis(
                as_chat_deepseek(FakeChatModel(runnable)),
                build_evidence_pack(),
            )
        )

    assert error_info.value.event_id == 42
    assert error_info.value.unknown_evidence_ids == ("failure:999:0",)

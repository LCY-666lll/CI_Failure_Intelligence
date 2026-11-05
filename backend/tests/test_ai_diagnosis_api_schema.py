"""验证 AI 诊断 HTTP 响应的状态与字段组合。"""

from datetime import UTC, datetime, timedelta

import pytest
from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis, AiRootCause
from ci_failure_intelligence.schemas.ai_diagnosis_api import AiDiagnosisResponse
from pydantic import ValidationError


def build_diagnosis() -> AiDiagnosis:
    """构造一份有效的结构化诊断。"""

    return AiDiagnosis(
        summary="订单测试因接口返回 500 而失败",
        root_causes=[
            AiRootCause(
                title="订单接口内部错误",
                explanation="失败证据显示预期 200，但实际返回 500",
                confidence="high",
                evidence_ids=["failure:0:0"],
            )
        ],
        recommended_actions=["检查订单接口日志"],
        needs_more_information=False,
        missing_information=[],
    )


@pytest.mark.parametrize("status", ["generated", "cached"])
def test_completed_response_requires_and_returns_diagnosis(status: str) -> None:
    """新生成和缓存命中都应返回已经校验的诊断及可选调用元数据。"""

    response = AiDiagnosisResponse.model_validate(
        {
            "event_id": 42,
            "status": status,
            "attempt_count": 1,
            "diagnosis": build_diagnosis().model_dump(mode="json"),
            "model_name": "deepseek-flash",
            "input_tokens": 120,
            "output_tokens": 80,
            "total_tokens": 200,
            "lease_expires_at": None,
        }
    )

    assert response.status == status
    assert response.diagnosis is not None
    assert response.total_tokens == 200


def test_in_progress_response_only_exposes_lease_expiration() -> None:
    """处理中状态只能说明任务仍在执行，不能暴露未完成模型结果。"""

    lease_expires_at = datetime.now(UTC) + timedelta(seconds=120)

    response = AiDiagnosisResponse(
        event_id=42,
        status="in_progress",
        attempt_count=2,
        lease_expires_at=lease_expires_at,
    )

    assert response.diagnosis is None
    assert response.model_name is None
    assert response.lease_expires_at == lease_expires_at


@pytest.mark.parametrize(
    "payload",
    [
        {
            "event_id": 42,
            "status": "generated",
            "attempt_count": 1,
            "diagnosis": None,
        },
        {
            "event_id": 42,
            "status": "cached",
            "attempt_count": 1,
            "diagnosis": build_diagnosis().model_dump(mode="json"),
            "lease_expires_at": datetime.now(UTC),
        },
        {
            "event_id": 42,
            "status": "in_progress",
            "attempt_count": 1,
            "lease_expires_at": None,
        },
        {
            "event_id": 42,
            "status": "in_progress",
            "attempt_count": 1,
            "diagnosis": build_diagnosis().model_dump(mode="json"),
            "lease_expires_at": datetime.now(UTC),
        },
        {
            "event_id": 42,
            "status": "in_progress",
            "attempt_count": 1,
            "model_name": "deepseek-flash",
            "lease_expires_at": datetime.now(UTC),
        },
    ],
)
def test_response_rejects_status_field_contradictions(
    payload: dict[str, object],
) -> None:
    """响应状态与诊断、租约或模型结果矛盾时必须拒绝构造。"""

    with pytest.raises(ValidationError):
        AiDiagnosisResponse.model_validate(payload)

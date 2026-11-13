"""验证 AI 诊断路由的认证、编排调用和 HTTP 响应映射。"""

from datetime import UTC, datetime, timedelta

import pytest
from ci_failure_intelligence.api.exception_handlers import (
    AI_DIAGNOSIS_EVIDENCE_UNAVAILABLE_DETAIL,
    AI_DIAGNOSIS_LEASE_LOST_DETAIL,
    AI_DIAGNOSIS_SERVICE_UNAVAILABLE_DETAIL,
    AI_DIAGNOSIS_UPSTREAM_INVALID_DETAIL,
)
from ci_failure_intelligence.api.routes import ai_diagnosis as ai_diagnosis_route
from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis, AiRootCause
from ci_failure_intelligence.service.ai_diagnosis import (
    AiDiagnosisProviderError,
    AiDiagnosisStructuredOutputError,
)
from ci_failure_intelligence.service.ai_diagnosis_orchestration import (
    AiDiagnosisOrchestrationResult,
    AiDiagnosisOrchestrationStatus,
)
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisLeaseLostError,
    AiDiagnosisPersistenceUnavailableError,
)
from ci_failure_intelligence.service.ai_diagnosis_validation import (
    AiDiagnosisEvidenceReferenceError,
)
from ci_failure_intelligence.service.ai_evidence import AiEvidenceEmptyError
from fastapi import FastAPI
from fastapi.testclient import TestClient


def build_diagnosis() -> AiDiagnosis:
    """构造一份由假编排服务返回的有效诊断。"""

    return AiDiagnosis(
        summary="订单接口返回 500，导致测试失败",
        root_causes=[
            AiRootCause(
                title="订单接口内部错误",
                explanation="证据显示接口返回了非预期的 500",
                confidence="high",
                evidence_ids=["failure:0:0"],
            )
        ],
        recommended_actions=["检查订单接口服务端日志"],
        needs_more_information=False,
        missing_information=[],
    )


def trigger_headers(settings: Settings) -> dict[str, str]:
    """构造只属于自动化测试的 AI 触发认证头。"""

    token = settings.ai_diagnosis_trigger_token.get_secret_value()
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("orchestration_status", "expected_http_status"),
    [
        (AiDiagnosisOrchestrationStatus.GENERATED, 200),
        (AiDiagnosisOrchestrationStatus.CACHED, 200),
    ],
)
def test_completed_diagnosis_returns_structured_http_response(
    client: TestClient,
    app: FastAPI,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    orchestration_status: AiDiagnosisOrchestrationStatus,
    expected_http_status: int,
) -> None:
    """新生成和缓存命中应返回 200、诊断内容和有限调用元数据。"""

    diagnosis = build_diagnosis()
    received_arguments: dict[str, object] = {}

    async def fake_orchestrate_ai_diagnosis(**kwargs: object):
        received_arguments.update(kwargs)
        return AiDiagnosisOrchestrationResult(
            event_id=42,
            status=orchestration_status,
            attempt_count=1,
            diagnosis=diagnosis,
            model_name="deepseek-flash",
            input_tokens=120,
            output_tokens=80,
            total_tokens=200,
        )

    monkeypatch.setattr(
        ai_diagnosis_route,
        "orchestrate_ai_diagnosis",
        fake_orchestrate_ai_diagnosis,
    )

    response = client.post(
        "/api/v1/ci-runs/42/diagnosis",
        headers=trigger_headers(test_settings),
    )

    assert response.status_code == expected_http_status
    payload = response.json()
    assert payload["event_id"] == 42
    assert payload["status"] == orchestration_status.value
    assert payload["diagnosis"]["summary"] == diagnosis.summary
    assert payload["total_tokens"] == 200
    assert payload["lease_expires_at"] is None
    assert received_arguments == {
        "graph": app.state.ai_diagnosis_graph,
        "context": app.state.ai_diagnosis_context,
        "event_id": 42,
    }
    context = app.state.ai_diagnosis_context
    assert context.session_factory is app.state.database_session_factory
    assert context.client is app.state.deepseek_client
    assert context.settings is test_settings


def test_in_progress_diagnosis_returns_202_without_result(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有效租约正在工作时应返回 202 和到期时间，不重复调用模型。"""

    lease_expires_at = datetime.now(UTC) + timedelta(seconds=120)

    async def fake_orchestrate_ai_diagnosis(**_: object):
        return AiDiagnosisOrchestrationResult(
            event_id=42,
            status=AiDiagnosisOrchestrationStatus.IN_PROGRESS,
            attempt_count=2,
            lease_expires_at=lease_expires_at,
        )

    monkeypatch.setattr(
        ai_diagnosis_route,
        "orchestrate_ai_diagnosis",
        fake_orchestrate_ai_diagnosis,
    )

    response = client.post(
        "/api/v1/ci-runs/42/diagnosis",
        headers=trigger_headers(test_settings),
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "in_progress"
    assert payload["attempt_count"] == 2
    assert payload["diagnosis"] is None
    assert payload["model_name"] is None
    assert payload["lease_expires_at"] is not None


def test_invalid_event_id_is_rejected_before_orchestration(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非正整数事件 ID 应由 FastAPI 返回 422，不进入业务编排。"""

    async def forbidden_orchestration(**_: object):
        raise AssertionError("非法 event_id 不得进入 AI 诊断编排")

    monkeypatch.setattr(
        ai_diagnosis_route,
        "orchestrate_ai_diagnosis",
        forbidden_orchestration,
    )

    response = client.post(
        "/api/v1/ci-runs/0/diagnosis",
        headers=trigger_headers(test_settings),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("business_error", "expected_status", "expected_detail"),
    [
        (
            AiEvidenceEmptyError(event_id=42),
            409,
            AI_DIAGNOSIS_EVIDENCE_UNAVAILABLE_DETAIL,
        ),
        (
            AiDiagnosisLeaseLostError(event_id=42),
            409,
            AI_DIAGNOSIS_LEASE_LOST_DETAIL,
        ),
        (
            AiDiagnosisProviderError(event_id=42),
            503,
            AI_DIAGNOSIS_SERVICE_UNAVAILABLE_DETAIL,
        ),
        (
            AiDiagnosisPersistenceUnavailableError(
                "test-only database connection detail"
            ),
            503,
            AI_DIAGNOSIS_SERVICE_UNAVAILABLE_DETAIL,
        ),
        (
            AiDiagnosisStructuredOutputError(
                event_id=42,
                reason="test-only-invalid-json",
            ),
            502,
            AI_DIAGNOSIS_UPSTREAM_INVALID_DETAIL,
        ),
        (
            AiDiagnosisEvidenceReferenceError(
                event_id=42,
                unknown_evidence_ids=["failure:999:0"],
            ),
            502,
            AI_DIAGNOSIS_UPSTREAM_INVALID_DETAIL,
        ),
    ],
)
def test_ai_diagnosis_business_errors_use_stable_http_contract(
    client: TestClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    business_error: Exception,
    expected_status: int,
    expected_detail: str,
) -> None:
    """诊断异常应转换为固定 HTTP 响应，不泄露内部异常内容。"""

    async def failing_orchestration(**_: object):
        raise business_error

    monkeypatch.setattr(
        ai_diagnosis_route,
        "orchestrate_ai_diagnosis",
        failing_orchestration,
    )

    response = client.post(
        "/api/v1/ci-runs/42/diagnosis",
        headers=trigger_headers(test_settings),
    )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert str(business_error) not in response.text

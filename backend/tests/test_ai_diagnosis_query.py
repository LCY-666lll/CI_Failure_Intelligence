"""验证诊断只读查询的状态、证据校验和数据库异常边界。"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis
from ci_failure_intelligence.service.ai_diagnosis_persistence import (
    AiDiagnosisPersistenceUnavailableError,
)
from ci_failure_intelligence.service.ai_diagnosis_query import get_ai_diagnosis
from ci_failure_intelligence.service.ci_failure_query import (
    CiFailureQueryNotFoundError,
)
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


def build_row(status: str = "completed") -> dict[str, object]:
    """构造符合真实表状态约束的查询结果，不包含内部任务凭据。"""

    now = datetime.now(UTC)
    diagnosis = {
        "summary": "订单测试返回了非预期状态码",
        "root_causes": [
            {
                "title": "接口返回错误",
                "explanation": "实际500与预期200不一致",
                "confidence": "medium",
                "evidence_ids": ["failure:0:0"],
            }
        ],
        "recommended_actions": ["检查接口日志"],
        "needs_more_information": False,
        "missing_information": [],
    }
    return {
        "event_id": 42,
        "status": status,
        "attempt_count": 2,
        "diagnosis": diagnosis if status == "completed" else None,
        "model_name": None,
        "input_tokens": 120 if status == "completed" else None,
        "output_tokens": 80 if status == "completed" else None,
        "total_tokens": 200 if status == "completed" else None,
        "lease_expires_at": now + timedelta(seconds=60)
        if status == "processing"
        else None,
        "error_code": "provider_unavailable" if status == "failed" else None,
        "error_message": "模型暂时不可用" if status == "failed" else None,
        "started_at": now,
        "finished_at": None if status == "processing" else now,
        "updated_at": now,
    }


def build_session(row: dict[str, object] | None) -> AsyncMock:
    """提供只有execute查询行为的假Session。"""

    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = result
    return session


@pytest.mark.parametrize("status", ["processing", "completed", "failed"])
def test_read_returns_saved_status_and_validated_diagnosis(status: str) -> None:
    """已有任务应保留状态，完成态JSON应转换成现有诊断模型。"""

    row = build_row(status)
    session = build_session(row)
    response = asyncio.run(get_ai_diagnosis(session, event_id=42))

    assert response.status == status
    assert response.attempt_count == 2
    assert response.error_code == row["error_code"]
    assert response.lease_expires_at == row["lease_expires_at"]
    if status == "completed":
        assert isinstance(response.diagnosis, AiDiagnosis)
        assert response.total_tokens == 200
    else:
        assert response.diagnosis is None


def test_existing_event_without_task_returns_not_started() -> None:
    """左连接的右表为空，表示尚未生成诊断，而不是事件不存在。"""

    row: dict[str, object] = {key: None for key in build_row()}
    row["event_id"] = 42
    response = asyncio.run(get_ai_diagnosis(build_session(row), event_id=42))

    assert response.status == "not_started"
    assert response.attempt_count == 0
    assert response.diagnosis is None
    assert response.started_at is None


def test_missing_event_preserves_not_found_error() -> None:
    """缺少左表事件应保留现有404业务异常。"""

    with pytest.raises(CiFailureQueryNotFoundError) as caught:
        asyncio.run(get_ai_diagnosis(build_session(None), event_id=404))

    assert caught.value.event_id == 404


def test_expired_processing_lease_is_not_modified_by_read() -> None:
    """读取过期任务照实返回，不擅自改变数据库状态。"""

    row = build_row("processing")
    row["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=60)
    response = asyncio.run(get_ai_diagnosis(build_session(row), event_id=42))

    assert response.status == "processing"
    assert response.lease_expires_at == row["lease_expires_at"]


@pytest.mark.parametrize("diagnosis", [None, {}, {"summary": "不完整JSON"}])
def test_completed_task_with_invalid_result_is_unavailable(diagnosis: object) -> None:
    """不能把缺失或损坏的完成结果作为可信诊断返回。"""

    row = build_row()
    row["diagnosis"] = diagnosis

    with pytest.raises(AiDiagnosisPersistenceUnavailableError) as caught:
        asyncio.run(get_ai_diagnosis(build_session(row), event_id=42))

    if diagnosis is not None:
        assert isinstance(caught.value.__cause__, ValidationError)


def test_database_error_is_translated_with_original_cause() -> None:
    """数据库故障由服务转换，保留服务端异常链。"""

    session = build_session(None)
    error = SQLAlchemyError("test-only database failure")
    session.execute.side_effect = error

    with pytest.raises(AiDiagnosisPersistenceUnavailableError) as caught:
        asyncio.run(get_ai_diagnosis(session, event_id=42))

    assert caught.value.__cause__ is error


def test_query_is_single_select_and_excludes_internal_credentials() -> None:
    """真实SQL结构必须是一次左连接SELECT，并显式选择安全字段。"""

    session = build_session(build_row())
    asyncio.run(get_ai_diagnosis(session, event_id=42))
    session.execute.assert_awaited_once()
    statement = session.execute.call_args.args[0]
    sql = str(statement)

    assert sql.startswith("SELECT ")
    assert "LEFT OUTER JOIN ci_failure_diagnoses" in sql
    assert "ci_failure_events.id =" in sql
    assert set(statement.compile().params.values()) == {42}
    assert {
        "lease_token",
        "evidence_sha256",
        "model_config_sha256",
        "junit_xml",
    }.isdisjoint(statement.selected_columns.keys())
    session.commit.assert_not_awaited()

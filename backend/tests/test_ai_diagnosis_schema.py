"""验证 DeepSeek 结构化诊断的字段和语义一致性契约。"""

import pytest
from ci_failure_intelligence.schemas.ai_diagnosis import AiDiagnosis
from pydantic import ValidationError


def build_diagnosis_payload(**overrides: object) -> dict[str, object]:
    """构造一份字段完整、默认不缺少额外信息的模型输出。"""

    payload: dict[str, object] = {
        "summary": "数据库连接失败导致测试无法完成",
        "root_causes": [
            {
                "title": "PostgreSQL 服务不可用",
                "explanation": "failure:3:0 显示数据库连接被拒绝",
                "confidence": "high",
                "evidence_ids": ["failure:3:0"],
            }
        ],
        "recommended_actions": ["检查 PostgreSQL 容器健康状态"],
        "needs_more_information": False,
        "missing_information": [],
    }
    payload.update(overrides)
    return payload


def test_valid_diagnosis_is_normalized() -> None:
    """合法诊断应去除展示文本首尾空白并保留证据引用。"""

    payload = build_diagnosis_payload(
        summary="  数据库连接失败导致测试无法完成  ",
        recommended_actions=["  检查 PostgreSQL 容器健康状态  "],
    )

    diagnosis = AiDiagnosis.model_validate(payload)

    assert diagnosis.summary == "数据库连接失败导致测试无法完成"
    assert diagnosis.recommended_actions == ["检查 PostgreSQL 容器健康状态"]
    assert diagnosis.root_causes[0].evidence_ids == ["failure:3:0"]


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("summary", "   "),
        ("root_causes", []),
        ("recommended_actions", []),
        ("needs_more_information", "true"),
    ],
)
def test_required_diagnosis_fields_reject_invalid_values(
    field_name: str,
    invalid_value: object,
) -> None:
    """空白内容、空列表和字符串布尔值不能伪装成有效诊断。"""

    with pytest.raises(ValidationError):
        AiDiagnosis.model_validate(
            build_diagnosis_payload(**{field_name: invalid_value})
        )


def test_unknown_output_field_is_rejected() -> None:
    """模型擅自增加的字段不能悄悄进入稳定诊断契约。"""

    with pytest.raises(ValidationError):
        AiDiagnosis.model_validate(
            build_diagnosis_payload(hidden_reasoning="test-only chain of thought")
        )


@pytest.mark.parametrize(
    "root_cause_overrides",
    [
        {"confidence": "certain"},
        {"evidence_ids": []},
        {"evidence_ids": ["detail:3:0"]},
        {"evidence_ids": ["failure:3:0", "failure:3:0"]},
    ],
)
def test_root_cause_rejects_invalid_confidence_or_evidence(
    root_cause_overrides: dict[str, object],
) -> None:
    """根因必须使用受支持置信度并引用非重复的稳定证据编号。"""

    root_cause: dict[str, object] = {
        "title": "PostgreSQL 服务不可用",
        "explanation": "数据库连接被拒绝",
        "confidence": "high",
        "evidence_ids": ["failure:3:0"],
    }
    root_cause.update(root_cause_overrides)

    with pytest.raises(ValidationError):
        AiDiagnosis.model_validate(build_diagnosis_payload(root_causes=[root_cause]))


def test_more_than_five_root_causes_are_rejected() -> None:
    """第一版不能让模型用大量低价值猜测撑大诊断结果。"""

    root_causes = [
        {
            "title": f"可能原因 {index}",
            "explanation": "根据当前失败证据形成的测试解释",
            "confidence": "low",
            "evidence_ids": ["failure:3:0"],
        }
        for index in range(6)
    ]

    with pytest.raises(ValidationError):
        AiDiagnosis.model_validate(build_diagnosis_payload(root_causes=root_causes))


def test_more_than_ten_recommended_actions_are_rejected() -> None:
    """建议数量必须有界，避免前端和输出 token 被低价值内容占满。"""

    with pytest.raises(ValidationError):
        AiDiagnosis.model_validate(
            build_diagnosis_payload(
                recommended_actions=[f"排查动作 {index}" for index in range(11)]
            )
        )


def test_missing_information_is_required_when_flag_is_true() -> None:
    """模型声称证据不足时必须具体说明还缺少哪些信息。"""

    with pytest.raises(ValidationError, match="必须说明"):
        AiDiagnosis.model_validate(build_diagnosis_payload(needs_more_information=True))


def test_missing_information_must_be_empty_when_flag_is_false() -> None:
    """模型声称信息充足时不能同时返回互相矛盾的缺失信息。"""

    with pytest.raises(ValidationError, match="必须为空"):
        AiDiagnosis.model_validate(
            build_diagnosis_payload(
                missing_information=["数据库容器日志"],
            )
        )


def test_consistent_missing_information_is_accepted() -> None:
    """证据不足标记和非空缺失列表一致时应允许进入下一层校验。"""

    diagnosis = AiDiagnosis.model_validate(
        build_diagnosis_payload(
            needs_more_information=True,
            missing_information=["数据库容器日志"],
        )
    )

    assert diagnosis.needs_more_information is True
    assert diagnosis.missing_information == ["数据库容器日志"]

"""将失败明细整理成稳定、有限且脱敏的检索文本。"""

from ci_failure_intelligence.schemas.ci_failure_query import CiFailureDetailResponse
from ci_failure_intelligence.service.ai_evidence import build_ai_failure_evidence

# 写入和检索必须使用相同的索引规则版本。
FAILURE_VECTOR_INDEX_VERSION = "v1"


def build_failure_index_text(
    detail: CiFailureDetailResponse,
) -> str:
    """为一条 failure/error 构造索引和查询共用的文本。

    Args:
        detail: 已通过查询响应模型校验的失败明细，
            包含测试身份、错误类型和原始错误内容。

    Returns:
        str: 复用现有脱敏及字段限长规则后，按固定顺序
            拼接的检索文本；不包含事件或数据库行身份。
    """

    # 复用现有证据处理规则，不另外维护一套脱敏实现。
    evidence = build_ai_failure_evidence(detail)

    # 错误类型和消息优先，降低长名称挤占模型输入窗口的影响。
    fields = (
        ("issue_type", evidence.issue_type),
        ("message", evidence.message),
        ("test_name", evidence.test_name),
        ("classname", evidence.classname),
        ("suite_name", evidence.suite_name),
        ("details", evidence.details),
    )

    # 用列表保存每个字段对应的一行文本
    lines = []

    # 遍历字段
    for name, value in fields:
        # 如果字段值是 None，跳过
        if value is None:
            continue
        line = f"{name}: {value}"
        lines.append(line)

    # 使用换行符连接列表中的字符串
    result = "\n".join(lines)

    return result

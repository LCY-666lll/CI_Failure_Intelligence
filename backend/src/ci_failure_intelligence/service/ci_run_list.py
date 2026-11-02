"""读取最近 CI 事件摘要和结构化失败数量。"""

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_detail import (
    CiFailureDetailModel,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.schemas.ci_run_list import (
    CiRunListItem,
    CiRunListResponse,
)

DEFAULT_CI_RUN_LIST_LIMIT = 20
MAXIMUM_CI_RUN_LIST_LIMIT = 100


class CiRunListUnavailableError(RuntimeError):
    """表示 PostgreSQL 无法可靠完成最近事件列表查询。"""


async def list_recent_ci_runs(
    session: AsyncSession,
    limit: int = DEFAULT_CI_RUN_LIST_LIMIT,
) -> CiRunListResponse:
    """读取最近接收的 CI 事件摘要和失败明细数量。

    Args:
        session: 已经连接项目 PostgreSQL 的异步 SQLAlchemy Session。
        limit: 本次最多返回的事件数量，必须位于1到100之间。

    Returns:
        CiRunListResponse: 按接收时间和事件主键倒序排列的事件摘要。

    Raises:
        ValueError: limit 不在允许的1到100范围内。
        CiRunListUnavailableError: PostgreSQL 无法可靠完成查询。
    """

    # 即使未来由 CLI 或后台任务直接调用，也不能绕过单批查询上限。
    if not 1 <= limit <= MAXIMUM_CI_RUN_LIST_LIMIT:
        raise ValueError(
            f"最近事件列表 limit 必须位于1到{MAXIMUM_CI_RUN_LIST_LIMIT}之间"
        )

    try:
        # 对当前事件计算所属明细数量。
        # scalar_subquery() 把这条 SELECT 转换成主查询中的一个标量字段。
        failure_count_subquery = (
            select(func.count(CiFailureDetailModel.id))
            .where(CiFailureDetailModel.event_id == CiFailureEventModel.id)
            # CiFailureEventModel 是外层查询使用的表，子查询要引用外层当前这一行
            .correlate(CiFailureEventModel)
            # 把 SELECT 查询转换成一个可以放进其他 SQL 中的标量(一个值)子查询表达式
            .scalar_subquery()
        )

        statement = (
            select(
                CiFailureEventModel.id.label("event_id"),
                CiFailureEventModel.repository_full_name,
                CiFailureEventModel.run_id,
                CiFailureEventModel.run_attempt,
                CiFailureEventModel.job_key,
                CiFailureEventModel.workflow_name,
                CiFailureEventModel.job_name,
                CiFailureEventModel.commit_sha,
                CiFailureEventModel.head_branch,
                CiFailureEventModel.received_at,
                CiFailureEventModel.parse_status,
                # 把前面那个统计子查询命名为 failure_count
                failure_count_subquery.label("failure_count"),
            )
            .order_by(
                CiFailureEventModel.received_at.desc(),
                CiFailureEventModel.id.desc(),
            )
            .limit(limit)
        )

        result = await session.execute(statement)
        event_rows = result.mappings().all()

        items = [
            CiRunListItem.model_validate(dict(event_row)) for event_row in event_rows
        ]

        return CiRunListResponse(items=items)

    except SQLAlchemyError as error:
        raise CiRunListUnavailableError("最近 CI 事件列表暂时无法查询") from error

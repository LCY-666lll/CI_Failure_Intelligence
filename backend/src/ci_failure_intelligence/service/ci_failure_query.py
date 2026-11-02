"""按事件 ID 读取 CI 失败状态和结构化失败证据。"""

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_detail import (
    CiFailureDetailModel,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.schemas.ci_failure_query import (
    CiFailureDetailResponse,
    CiFailureQueryResponse,
)


class CiFailureQueryNotFoundError(LookupError):
    """表示指定的 CI 失败事件不存在。"""

    def __init__(self, event_id: int) -> None:
        """记录没有找到的事件主键。

        Args:
            event_id: 调用方请求查询的 CI 失败事件内部主键。
        """
        self.event_id = event_id
        super().__init__(f"CI 失败事件不存在：event_id={event_id}")


class CiFailureQueryUnavailableError(RuntimeError):
    """表示 PostgreSQL 无法可靠完成 CI 失败详情查询。"""


async def get_ci_failure_details(
    session: AsyncSession,
    event_id: int,
) -> CiFailureQueryResponse:
    """读取一个 CI 事件的解析状态和有序失败明细。

    Args:
        session: 已经连接项目 PostgreSQL 的异步 SQLAlchemy Session。
        event_id: 要查询的 ci_failure_events 内部主键；调用前应为正整数。

    Returns:
        CiFailureQueryResponse: 事件运行信息、解析状态和按 XML 位置排序的
        failure/error 明细。

    Raises:
        CiFailureQueryNotFoundError: 指定 event_id 不存在。
        CiFailureQueryUnavailableError: PostgreSQL 无法可靠完成查询。
    """
    try:
        # 只选择响应需要的事件字段，不读取大体积 JUnit XML 和内容摘要。
        event_statement = select(
            CiFailureEventModel.id.label("event_id"),
            CiFailureEventModel.repository_full_name,
            CiFailureEventModel.run_id,
            CiFailureEventModel.run_attempt,
            CiFailureEventModel.job_key,
            CiFailureEventModel.workflow_name,
            CiFailureEventModel.job_name,
            CiFailureEventModel.commit_sha,
            CiFailureEventModel.head_branch,
            CiFailureEventModel.parse_status,
            CiFailureEventModel.parse_error_code,
            CiFailureEventModel.parse_error_message,
            CiFailureEventModel.parse_finished_at,
        ).where(CiFailureEventModel.id == event_id)

        event_result = await session.execute(event_statement)
        event_row = event_result.mappings().one_or_none()

        if event_row is None:
            raise CiFailureQueryNotFoundError(event_id)

        detail_statement = (
            select(
                CiFailureDetailModel.id,
                CiFailureDetailModel.testcase_index,
                CiFailureDetailModel.issue_index,
                CiFailureDetailModel.suite_name,
                CiFailureDetailModel.classname,
                CiFailureDetailModel.test_name,
                CiFailureDetailModel.issue_type,
                CiFailureDetailModel.message,
                CiFailureDetailModel.details,
            )
            .where(CiFailureDetailModel.event_id == event_id)
            .order_by(
                CiFailureDetailModel.testcase_index,
                CiFailureDetailModel.issue_index,
            )
        )

        detail_result = await session.execute(detail_statement)
        detail_rows = detail_result.mappings().all()

        failures = [
            CiFailureDetailResponse.model_validate(dict(detail_row))
            for detail_row in detail_rows
        ]

        response_payload = dict(event_row)
        response_payload["failures"] = failures

        return CiFailureQueryResponse.model_validate(response_payload)

    except CiFailureQueryNotFoundError:
        # 事件不存在是稳定业务结果，不能伪装成数据库故障。
        raise
    except SQLAlchemyError as error:
        raise CiFailureQueryUnavailableError(
            f"CI 失败详情暂时无法查询：event_id={event_id}"
        ) from error

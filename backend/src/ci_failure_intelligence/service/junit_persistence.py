"""协调 JUnit XML 解析与 PostgreSQL 明细持久化。"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_detail import (
    CiFailureDetailModel,
)
from ci_failure_intelligence.infrastructure.persistence.models.ci_failure_event import (
    CiFailureEventModel,
)
from ci_failure_intelligence.service.junit_parser import (
    JunitParseResult,
    JunitParsingError,
    parse_junit_xml,
)

# 与数据库 parse_error_message 的 CHECK 约束保持一致。
MAX_PARSE_ERROR_MESSAGE_LENGTH = 2_000


class JunitPersistenceStatus(StrEnum):
    """描述一次 JUnit 持久化处理的最终状态。"""

    PARSED = "parsed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JunitPersistenceResult:
    """返回一次事件解析和持久化的稳定结果。"""

    event_id: int
    status: JunitPersistenceStatus
    detail_count: int


class CiFailureEventNotFoundError(LookupError):
    """表示指定的原始 CI 事件不存在。"""

    def __init__(self, event_id: int) -> None:
        """记录未找到的事件主键。

        Args:
            event_id: ci_failure_events 表中的内部主键。
        """

        self.event_id = event_id
        super().__init__(f"CI 失败事件不存在：event_id={event_id}")


class JunitPersistenceUnavailableError(RuntimeError):
    """表示数据库无法可靠完成 JUnit 处理。"""


async def _read_junit_xml(session: AsyncSession, event_id: int) -> str:
    """读取一个原始 CI 事件保存的 JUnit XML。
    读取事务结束后才开始解析，避免解析 XML 时长期占用数据库事务。

    Args:
        session: 已经连接项目 PostgreSQL 的异步 SQLAlchemy 会话。
        event_id: ci_failure_events 表中的内部主键。

    Returns:
        str: 接入阶段已经可靠保存的 JUnit XML 原文。

    Raises:
        CiFailureEventNotFoundError: 指定事件不存在。
        SQLAlchemyError: 数据库查询失败。
    """

    async with session.begin():
        result = await session.execute(
            select(CiFailureEventModel.junit_xml).where(
                CiFailureEventModel.id == event_id
            )
        )
        junit_xml = result.scalar_one_or_none()
        if junit_xml is None:
            raise CiFailureEventNotFoundError(event_id)

        return junit_xml


async def _replace_junit_result(
    session: AsyncSession,
    event_id: int,
    # 解析成功的结果对象，或者解析异常对象允许是这两种类型之一
    outcome: JunitParseResult | JunitParsingError,
) -> JunitPersistenceResult:
    """在一个事务内整体替换事件的解析结果。

    Args:
        session: 已经连接项目 PostgreSQL 的异步 SQLAlchemy 会话。
        event_id: 正在处理的原始 CI 事件主键。
        outcome: 完整解析结果，或解析器产生的稳定业务错误。

    Returns:
        JunitPersistenceResult: 已提交的解析状态和明细数量。

    Raises:
        CiFailureEventNotFoundError: 事件在读取后被删除。
        SQLAlchemyError: 锁行、删除、插入或更新失败。
    """
    async with session.begin():
        # 同一个事件只能有一个处理者执行整体替换。
        lock_result = await session.execute(
            select(CiFailureEventModel.id)
            .where(CiFailureEventModel.id == event_id)
            .with_for_update()
        )
        locked_event_id = lock_result.scalar_one_or_none()

        if locked_event_id is None:
            raise CiFailureEventNotFoundError(event_id)

        # 无论本次解析成功还是失败，都先移除以前的派生结果。
        # 如果后续操作失败，当前事务会回滚，这次删除也不会提交。
        await session.execute(
            delete(CiFailureDetailModel).where(
                CiFailureDetailModel.event_id == event_id
            )
        )

        if isinstance(outcome, JunitParseResult):
            detail_rows = [
                {
                    "event_id": event_id,
                    "testcase_index": detail.testcase_index,
                    "issue_index": detail.issue_index,
                    "suite_name": detail.suite_name,
                    "classname": detail.classname,
                    "test_name": detail.test_name,
                    "issue_type": detail.issue_type,
                    "message": detail.message,
                    "details": detail.details,
                }
                for detail in outcome.details
            ]

            # 合法的零失败报告不需要执行空 INSERT，
            # 但事件仍然应该标记为 parsed。
            if detail_rows:
                await session.execute(
                    insert(CiFailureDetailModel),
                    detail_rows,
                )

            await session.execute(
                update(CiFailureEventModel)
                .where(CiFailureEventModel.id == event_id)
                .values(
                    parse_status=JunitPersistenceStatus.PARSED.value,
                    parse_error_code=None,
                    parse_error_message=None,
                    parse_finished_at=func.now(),
                )
            )

            return JunitPersistenceResult(
                event_id=event_id,
                status=JunitPersistenceStatus.PARSED,
                detail_count=len(detail_rows),
            )

        # 解析错误摘要必须与数据库的 2000 字符约束保持一致。
        error_message = str(outcome)[:MAX_PARSE_ERROR_MESSAGE_LENGTH]

        await session.execute(
            update(CiFailureEventModel)
            .where(CiFailureEventModel.id == event_id)
            .values(
                parse_status=JunitPersistenceStatus.FAILED.value,
                parse_error_code=outcome.code.value,
                parse_error_message=error_message,
                parse_finished_at=func.now(),
            )
        )

        return JunitPersistenceResult(
            event_id=event_id,
            status=JunitPersistenceStatus.FAILED,
            detail_count=0,
        )


async def process_junit_event(
    session: AsyncSession,
    event_id: int,
) -> JunitPersistenceResult:
    """解析一个已保存事件，并整体提交解析状态和失败明细。

    Args:
        session: 已经连接项目 PostgreSQL 的异步 SQLAlchemy 会话。
        event_id: ci_failure_events 表中的内部主键。

    Returns:
        JunitPersistenceResult: 数据库最终提交的解析状态和明细数量。

    Raises:
        CiFailureEventNotFoundError: 指定事件不存在。
        JunitPersistenceUnavailableError: PostgreSQL 无法可靠完成操作。
    """
    try:
        junit_xml = await _read_junit_xml(session, event_id)

        try:
            outcome: JunitParseResult | JunitParsingError = parse_junit_xml(junit_xml)
        except JunitParsingError as error:
            # 报告自身损坏属于可持久化的业务结果，
            # 不应伪装成数据库或服务不可用。
            outcome = error

        return await _replace_junit_result(
            session,
            event_id,
            outcome,
        )

    except SQLAlchemyError as error:
        # 调用层不需要依赖 SQLAlchemy 的具体异常类型，
        # 但通过异常链保留底层原因供日志和排查使用。
        raise JunitPersistenceUnavailableError(
            f"JUnit 解析结果暂时无法写入数据库：event_id={event_id}"
        ) from error

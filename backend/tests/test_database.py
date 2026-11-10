"""验证 PostgreSQL 异步 Engine 工厂的配置边界。"""

import asyncio
from collections.abc import Callable
from typing import Any, cast

from ci_failure_intelligence.core.config import Settings
from ci_failure_intelligence.infrastructure import database
from sqlalchemy.ext.asyncio import AsyncEngine


def test_database_url_uses_psycopg_and_hides_password(
    test_settings: Settings,
) -> None:
    database_url = database.create_database_url(test_settings)

    assert database_url.drivername == "postgresql+psycopg"
    assert database_url.username == "cfi_app"
    assert database_url.host == "127.0.0.1"
    assert database_url.port == 5432
    assert database_url.database == "ci_failure_intelligence"

    real_password = test_settings.postgres_password.get_secret_value()
    assert database_url.password == real_password
    assert real_password not in str(database_url)
    assert "***" in str(database_url)


def test_database_engine_receives_pool_and_timeout_settings(
    test_settings: Settings,
    monkeypatch: Any,
) -> None:
    captured_arguments: dict[str, object] = {}
    expected_engine = cast(AsyncEngine, object())

    def fake_create_async_engine(
        database_url: object,
        **keyword_arguments: object,
    ) -> AsyncEngine:
        captured_arguments["database_url"] = database_url
        captured_arguments.update(keyword_arguments)
        return expected_engine

    replacement = cast(Callable[..., AsyncEngine], fake_create_async_engine)
    monkeypatch.setattr(database, "create_async_engine", replacement)

    actual_engine = database.create_database_engine(test_settings)

    assert actual_engine is expected_engine
    assert captured_arguments["pool_size"] == 5
    assert captured_arguments["max_overflow"] == 5
    assert captured_arguments["pool_pre_ping"] is True
    assert captured_arguments["connect_args"] == {"connect_timeout": 3}
    assert captured_arguments["hide_parameters"] is True


def test_session_factory_creates_independent_sessions_for_same_engine(
    test_settings: Settings,
) -> None:
    engine = database.create_database_engine(test_settings)
    session_factory = database.create_database_session_factory(engine)

    first_session = session_factory()
    second_session = session_factory()

    try:
        assert first_session is not second_session
        assert first_session.bind is engine
        assert second_session.bind is engine
        assert first_session.sync_session.expire_on_commit is False
        assert second_session.sync_session.expire_on_commit is False
    finally:
        asyncio.run(first_session.close())
        asyncio.run(second_session.close())
        asyncio.run(engine.dispose())

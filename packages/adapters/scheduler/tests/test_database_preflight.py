from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.scheduler import main
from daimon.core.errors import DatabaseNotMigratedError


@pytest.mark.asyncio
async def test_run_checks_migrations_before_external_clients_and_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MagicMock()
    settings.sentry.dsn = None
    engine = MagicMock()
    engine.dispose = AsyncMock()
    sessionmaker = MagicMock()
    check = AsyncMock(side_effect=DatabaseNotMigratedError("database not migrated"))
    anthropic = MagicMock()
    acquire_lock = AsyncMock()

    monkeypatch.setattr(main, "load_settings", MagicMock(return_value=settings))
    monkeypatch.setattr(main, "configure_log_level", MagicMock())
    monkeypatch.setattr(main, "init_sentry", MagicMock())
    monkeypatch.setattr(main, "SchedulerSettings", MagicMock())
    monkeypatch.setattr(main, "_validate_mcp_settings", MagicMock())
    monkeypatch.setattr(main, "build_engine", MagicMock(return_value=engine))
    monkeypatch.setattr(main, "build_session_factory", MagicMock(return_value=sessionmaker))
    monkeypatch.setattr(main, "ensure_database_migrated", check)
    monkeypatch.setattr(main, "AsyncAnthropic", anthropic)
    monkeypatch.setattr(main, "_acquire_advisory_lock", acquire_lock)

    with pytest.raises(DatabaseNotMigratedError, match="database not migrated"):
        await main.run([])

    check.assert_awaited_once_with(sessionmaker)
    engine.dispose.assert_awaited_once_with()
    anthropic.assert_not_called()
    acquire_lock.assert_not_awaited()

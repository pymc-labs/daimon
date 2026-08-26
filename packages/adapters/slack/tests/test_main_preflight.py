from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.errors import DatabaseNotMigratedError


@pytest.mark.asyncio
async def test_main_checks_migrations_before_connecting_socket_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_module = importlib.import_module("daimon.adapters.slack.__main__")
    settings = MagicMock()
    settings.slack = MagicMock()
    settings.crypto.keys = [MagicMock()]
    settings.sentry.dsn = None
    sessionmaker = MagicMock()

    @asynccontextmanager
    async def runtime_context(_settings: object):
        yield SimpleNamespace(sessionmaker=sessionmaker)

    check = AsyncMock(side_effect=DatabaseNotMigratedError("database not migrated"))
    slack_app = MagicMock()
    socket_client = MagicMock()
    monkeypatch.setattr(main_module, "load_settings", MagicMock(return_value=settings))
    monkeypatch.setattr(main_module, "configure_log_level", MagicMock())
    monkeypatch.setattr(main_module, "init_sentry", MagicMock())
    monkeypatch.setattr(main_module, "build_runtime", runtime_context)
    monkeypatch.setattr(main_module, "ensure_database_migrated", check)
    monkeypatch.setattr(main_module, "SlackApp", slack_app)
    monkeypatch.setattr(main_module, "SocketModeClient", socket_client)

    with pytest.raises(DatabaseNotMigratedError, match="database not migrated"):
        await main_module.main()

    check.assert_awaited_once_with(sessionmaker)
    slack_app.assert_not_called()
    socket_client.assert_not_called()

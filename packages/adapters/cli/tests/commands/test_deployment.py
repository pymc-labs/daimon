"""daimon deployment reset-id."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.adapters.cli.commands import deployment
from daimon.adapters.cli.main import app
from daimon.core.stores.deployment_identity import get_or_stamp_deployment_id
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from typer.testing import CliRunner

pytestmark = pytest.mark.no_cli_local_seed


async def test_reset_id_replaces_the_stored_id_and_says_to_restart(
    db_nullpool_engine: AsyncEngine, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``daimon deployment reset-id --yes`` stores a new id and tells the operator what to do next."""
    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)

    @asynccontextmanager
    async def runtime(_settings: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(sessionmaker=sm)

    monkeypatch.setattr(deployment, "build_runtime", runtime)
    monkeypatch.setattr(deployment, "load_settings", lambda: None)
    async with sm.begin() as session:
        before = await get_or_stamp_deployment_id(session)
    console = Console(file=StringIO(), force_terminal=False, highlight=False, width=200)
    await deployment.deployment_reset_id(
        rt=cast(deployment.CliRuntime, SimpleNamespace(sessionmaker=sm)),
        console=console,
        confirmed=True,
    )
    async with sm.begin() as session:
        after = await get_or_stamp_deployment_id(session)
    out = cast(StringIO, console.file).getvalue()
    assert after != before, "the stored id must change"
    assert str(after) in out and "Restart" in out, out


def test_reset_id_is_listed_under_deployment() -> None:
    result = CliRunner().invoke(app, ["deployment", "--help"])
    assert "reset-id" in result.output, result.output

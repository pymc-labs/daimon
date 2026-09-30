"""`daimon channels budget set|clear|list` against the real database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from typing import cast

import pytest
import typer
from daimon.adapters.cli.commands.channels import budget_clear, budget_list, budget_set
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import StoreError
from daimon.core.stores import channel_budgets
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=160)


def _out(console: Console) -> str:
    return cast(StringIO, console.file).getvalue()


async def test_set_list_and_clear_a_budget(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = build_cli_runtime(db_session_factory)
    console = _console()
    tenant = await provision_tenant(db_session_factory, platform="slack", workspace_id="T1")
    args = {"rt": rt, "console": console, "platform": "slack", "workspace_id": "T1"}

    await budget_set(
        **args,
        channel_id="C1:1717.5",
        usd="20",
        window="fixed",
        starts_at="2026-07-01",
        ends_at="2026-07-03",
    )
    async with db_session_factory() as s:
        budget = await channel_budgets.get_channel_budget(
            s, tenant_id=tenant.tenant_id, platform="slack", channel_id="C1"
        )
    assert budget is not None, "a thread id budgets against its channel"
    assert (budget.limit_usd, budget.set_by_account_id) == (Decimal("20"), None)
    assert budget.ends_at == datetime(2026, 7, 3, tzinfo=UTC)
    assert "channel C1: $0.00 of $20.00 (2026-07-01 to 2026-07-03)" in _out(console)

    json_console = _console()
    await budget_list(**{**args, "console": json_console}, as_json=True)
    (row,) = json.loads(_out(json_console))
    assert (row["channel_id"], row["window"], row["active"]) == ("C1", "fixed", False)

    await budget_clear(**args, channel_id="C1")
    await budget_clear(**args, channel_id="C1")
    assert "C1: budget cleared" in _out(console)
    assert "C1: had no budget" in _out(console)


async def test_bad_requests_are_refused_before_any_write(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = build_cli_runtime(db_session_factory)
    console = _console()
    tenant = await provision_tenant(db_session_factory, platform="discord", workspace_id="g1")
    base = {"rt": rt, "console": console, "workspace_id": "g1", "channel_id": "123"}

    with pytest.raises(typer.BadParameter, match="window must be one of"):
        await budget_set(
            **base, platform="discord", usd="5", window="weekly", starts_at=None, ends_at=None
        )
    with pytest.raises(typer.BadParameter, match="unsupported platform"):
        await budget_set(
            **base, platform="cli", usd="5", window="monthly", starts_at=None, ends_at=None
        )
    with pytest.raises(StoreError, match="no tenant"):
        await budget_set(
            **{**base, "workspace_id": "missing"},
            platform="discord",
            usd="5",
            window="monthly",
            starts_at=None,
            ends_at=None,
        )
    async with db_session_factory() as s:
        assert await channel_budgets.list_channel_budgets(s, tenant_id=tenant.tenant_id) == []

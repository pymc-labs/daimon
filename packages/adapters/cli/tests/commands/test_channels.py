"""`daimon channels budget set|clear|list` against the real database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
import typer
from daimon.adapters.cli.commands.channels import budget_clear, budget_list, budget_set
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import DaimonError, StoreError
from daimon.core.stores import channel_budgets
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import FakeCliSettings, build_cli_runtime

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


def _discord_settings() -> object:
    return SimpleNamespace(
        cli=FakeCliSettings.cli,
        discord=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "t")),
    )


def _no_discord() -> object:
    return SimpleNamespace(cli=FakeCliSettings.cli, discord=None)


def _discord_channels(channels: dict[str, dict[str, object]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bot t", "looked up with the bot token"
        channel = channels.get(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json=channel) if channel else httpx.Response(404, json={})

    return httpx.MockTransport(handler)


async def test_a_discord_thread_budgets_its_parent_and_a_foreign_channel_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_discord_settings())
    tenant = await provision_tenant(db_session_factory, platform="discord", workspace_id="g1")
    transport = _discord_channels(
        {
            "900": {"id": "900", "type": 11, "guild_id": "g1", "parent_id": "100"},
            "200": {"id": "200", "type": 0, "guild_id": "g2"},
        }
    )
    args = {
        "rt": rt,
        "console": _console(),
        "platform": "discord",
        "workspace_id": "g1",
        "usd": "5",
        "window": "monthly",
        "starts_at": None,
        "ends_at": None,
        "discord_transport": transport,
    }

    await budget_set(**args, channel_id="900")
    with pytest.raises(DaimonError, match="not in server g1"):
        await budget_set(**args, channel_id="200")
    with pytest.raises(DaimonError, match="not visible to daimon"):
        await budget_set(**args, channel_id="404")
    with pytest.raises(DaimonError, match="DAIMON_DISCORD__BOT_TOKEN is not set"):
        await budget_set(
            **{**args, "rt": build_cli_runtime(db_session_factory, settings=_no_discord())},
            channel_id="900",
        )

    async with db_session_factory() as s:
        budgets = await channel_budgets.list_channel_budgets(s, tenant_id=tenant.tenant_id)
    assert [b.channel_id for b in budgets] == ["100"], "only the thread's parent was saved"

    clear = {"rt": rt, "console": args["console"], "platform": "discord", "workspace_id": "g1"}
    await budget_clear(**clear, channel_id="900", discord_transport=transport)
    async with db_session_factory() as s:
        assert await channel_budgets.list_channel_budgets(s, tenant_id=tenant.tenant_id) == [], (
            "clearing a thread clears its parent's budget"
        )
    async with db_session_factory.begin() as s:
        await channel_budgets.set_channel_budget(
            s,
            tenant_id=tenant.tenant_id,
            platform="discord",
            channel_id="404",
            limit_usd=Decimal("5"),
            window="monthly",
            starts_at=None,
            ends_at=None,
            set_by_account_id=None,
        )
    await budget_clear(**clear, channel_id="404", discord_transport=transport)
    assert "channel 404: budget cleared" in _out(args["console"]), (
        "a channel the bot cannot see is cleared as given"
    )

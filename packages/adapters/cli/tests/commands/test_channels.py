"""`daimon channels budget` and `daimon channels admins` against the real database."""

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
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands.channels import (
    budget_clear,
    budget_list,
    budget_set,
    channels_admins_clear,
    channels_admins_get,
    channels_admins_set,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import DaimonError, StoreError
from daimon.core.stores import channel_budgets
from daimon.core.stores.channel_admins import get_channel_admins
from daimon.testing.factories import make_tenant
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
    assert "channel C1: $0.00 of $20.00 (2026-07-01 until 2026-07-03)" in _out(console)

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


TEAMS_CHANNEL = "19:growth@thread.tacv2"
TEAMS_USER = "0b8e2c1a-1d2e-4f3a-9b4c-5d6e7f8a9b0c"


async def test_a_teams_thread_budgets_and_admins_its_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Teams channel ids contain ":", so only the `;messageid=` suffix is cut."""
    rt = build_cli_runtime(db_session_factory)
    console = _console()
    tenant = await provision_tenant(db_session_factory, platform="teams", workspace_id="tid")
    args = {"rt": rt, "console": console, "platform": "teams", "workspace_id": "tid"}
    thread = f"{TEAMS_CHANNEL};messageid=1717"

    await budget_set(
        **args, channel_id=thread, usd="5", window="monthly", starts_at=None, ends_at=None
    )
    await channels_admins_set(
        **args, channel_id=thread, roles=[], users=[TEAMS_USER.upper()], as_json=False
    )

    async with db_session_factory() as s:
        budget = await channel_budgets.get_channel_budget(
            s, tenant_id=tenant.tenant_id, platform="teams", channel_id=TEAMS_CHANNEL
        )
        admins = await get_channel_admins(
            s, tenant_id=tenant.tenant_id, platform="teams", channel_id=TEAMS_CHANNEL
        )
    assert budget is not None, "a Teams thread budgets against its whole channel id"
    assert admins is not None and admins.user_ids == (TEAMS_USER,), (
        "the channel's admins are stored under the channel, Entra ids lower-cased"
    )
    await budget_clear(**args, channel_id=thread)
    assert f"channel {TEAMS_CHANNEL}: budget cleared" in _out(console), "clear finds it"


GUILD = "900000000000000001"
CHANNEL = "900000000000000002"
ROLE = "900000000000000003"
USER = "900000000000000004"


async def test_set_get_clear_round_trip(
    db_session_factory: async_sessionmaker[AsyncSession], stub_anthropic: AsyncAnthropic
) -> None:
    async with db_session_factory.begin() as session:
        await make_tenant(session, platform="discord", workspace_id=GUILD)
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic)
    where = {"rt": rt, "platform": "discord", "workspace_id": GUILD}

    await channels_admins_set(
        **where,
        console=_console(),
        channel_id=CHANNEL,
        roles=[ROLE],
        users=[USER, USER],
        as_json=True,
    )
    listed = _console()
    await channels_admins_get(**where, console=listed, channel_id=None, as_json=True)
    (row,) = json.loads(_out(listed))
    assert (row["channel_id"], row["role_ids"], row["user_ids"]) == (CHANNEL, [ROLE], [USER]), (
        "json lists the saved grant"
    )

    cleared = _console()
    await channels_admins_clear(**where, console=cleared, channel_id=CHANNEL)
    assert "cleared" in _out(cleared), "clear reports it"
    after = _console()
    await channels_admins_get(**where, console=after, channel_id=CHANNEL, as_json=True)
    assert json.loads(_out(after)) == [], "nothing is left after clear"


async def test_set_refuses_bad_ids_empty_lists_and_unknown_tenants(
    db_session_factory: async_sessionmaker[AsyncSession], stub_anthropic: AsyncAnthropic
) -> None:
    async with db_session_factory.begin() as session:
        await make_tenant(session, platform="slack", workspace_id="T0ADMINS")
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic)
    slack = {"rt": rt, "console": _console(), "platform": "slack", "workspace_id": "T0ADMINS"}

    with pytest.raises(typer.BadParameter, match="no roles"):
        await channels_admins_set(
            **slack, channel_id="C0GROWTH", roles=["S1"], users=[], as_json=False
        )
    with pytest.raises(typer.BadParameter, match="use clear"):
        await channels_admins_set(**slack, channel_id="C0GROWTH", roles=[], users=[], as_json=False)
    with pytest.raises(StoreError, match="no tenant"):
        await channels_admins_set(
            rt=rt,
            console=_console(),
            platform="discord",
            workspace_id=GUILD,
            channel_id=CHANNEL,
            roles=[],
            users=[USER],
            as_json=False,
        )


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
        channel_id = request.url.path.rsplit("/", 1)[-1]
        if channel_id == "500":
            return httpx.Response(500, json={})
        channel = channels.get(channel_id)
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
        for raw in ("404", "901"):
            await channel_budgets.set_channel_budget(
                s,
                tenant_id=tenant.tenant_id,
                platform="discord",
                channel_id=raw,
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
    with pytest.raises(DaimonError, match="HTTP 500"):
        await budget_clear(**clear, channel_id="500", discord_transport=transport)
    no_token = {**clear, "rt": build_cli_runtime(db_session_factory, settings=_no_discord())}
    await budget_clear(**no_token, channel_id="901")
    assert "channel 901: budget cleared" in _out(args["console"]), (
        "without a bot token the id is cleared as given"
    )

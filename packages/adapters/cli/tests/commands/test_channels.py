"""`daimon channels budget` and `daimon channels admins` against the real database."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
import typer
from anthropic import AsyncAnthropic
from cryptography.fernet import Fernet
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands import channels as channels_mod
from daimon.adapters.cli.commands.channels import (
    agents_rule_set,
    budget_clear,
    budget_list,
    budget_set,
    channels_admins_clear,
    channels_admins_get,
    channels_admins_set,
    channels_list,
    channels_rule_set,
    channels_skills_add,
    channels_skills_list,
    channels_skills_remove,
)
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import DaimonError, StoreError
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores import channel_budgets
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.channel_admins import get_channel_admins
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.security_audit import list_events
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from ..harness import FakeCliSettings, build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed
OWN = ChannelRule(readers="own", writers="own")


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
    async with db_session_factory() as s:
        events = await list_events(s, tenant_id=tenant.tenant_id)
    assert sorted((e.tool_name, e.operation, e.reason) for e in events) == [
        ("cli/channels budget clear", "set_channel_budget", "channel:C1"),
        ("cli/channels budget set", "set_channel_budget", "channel:C1"),
    ], "each change is audited once; clearing nothing records nothing"


async def test_add_list_and_remove_a_channel_skill_audited(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await provision_tenant(db_session_factory, platform="slack", workspace_id="T1")
    title = tenant_scoped_display_title(tenant_id=tenant.tenant_id, name="pdf-tools")
    now = datetime(2026, 9, 13, tzinfo=UTC).isoformat()
    skill = {
        "id": "skill_lib",
        "created_at": now,
        "display_title": title,
        "latest_version": "v3",
        "source": "custom",
        "type": "skill",
        "updated_at": now,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/skills":
            return httpx.Response(200, json={"data": [skill], "next_page": None})
        return httpx.Response(200, json={"data": [], "next_page": None})

    rt = build_cli_runtime(db_session_factory, anthropic=build_fake_anthropic(handler))
    console = _console()
    args = {"rt": rt, "console": console, "platform": "slack", "workspace_id": "T1"}

    await channels_skills_add(**args, channel_id="C1:1717.5", skill="pdf-tools")
    with pytest.raises(DaimonError, match="No skill of this workspace"):
        await channels_skills_add(**args, channel_id="C1", skill="missing")
    json_console = _console()
    await channels_skills_list(**{**args, "console": json_console}, channel_id=None, as_json=True)
    (row,) = json.loads(_out(json_console))
    assert (row["channel_id"], row["skill_id"], row["version"]) == ("C1", "skill_lib", "v3")

    await channels_skills_remove(**args, channel_id="C1", skill="pdf-tools")
    await channels_skills_remove(**args, channel_id="C1", skill="pdf-tools")
    assert "channel C1 has no such skill" in _out(console)
    async with db_session_factory() as s:
        events = await list_events(s, tenant_id=tenant.tenant_id)
    assert sorted((e.tool_name, e.operation, e.reason) for e in events) == [
        ("cli/channels skills add", "set_channel_skills", "channel:C1"),
        ("cli/channels skills remove", "set_channel_skills", "channel:C1"),
    ], "each change is audited once; a refusal or a no-op records nothing"


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
OTHER_CHANNEL = "900000000000000005"


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

    with pytest.raises(typer.BadParameter, match="invalid slack group id"):
        await channels_admins_set(
            **slack, channel_id="C0GROWTH", roles=["<!subteam^S1>"], users=[], as_json=False
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


def _ma(tenant_id: uuid.UUID, *names: str) -> AsyncAnthropic:
    state = FakeMAState()
    for name in names:
        agent = ma_agent(id=f"agent_{name}", name=name, tenant_id=tenant_id)
        state.agents[agent.id] = agent.model_dump(mode="json")
    return build_fake_anthropic(make_fake_ma_handler(state))


async def test_rule_set_copies_an_agent_keeps_it_there_then_opens(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One command does what the panel's Permissions screen does: copy, keep, open."""
    tenant_id = await _isolatable(db_session_factory, "discord", GUILD)
    rt = build_cli_runtime(
        db_session_factory, anthropic=_ma(tenant_id, "shared"), settings=_isolate_settings()
    )
    console = _console()
    where = {"rt": rt, "console": console, "platform": "discord", "workspace_id": GUILD}

    with pytest.raises(typer.Exit):
        await channels_rule_set(**where, channel_id=CHANNEL, readers="own")
    assert "shared also answers outside this channel" in _out(console), _out(console)
    named = _discord_channels({CHANNEL: {"guild_id": GUILD, "name": "Team Alpha", "type": 0}})
    await channels_rule_set(
        **where, channel_id=CHANNEL, readers="own", copy_from="shared", discord_transport=named
    )

    async with db_session_factory() as s:
        policy = await load_access_policy(s, tenant_id=tenant_id)
    name = "team-alpha"  # the copy is named after the channel
    assert policy.channel_rules == {CHANNEL: OWN}, "only its own agents read it"
    assert policy.agent_rules == {name: AgentRule(runs_in=(CHANNEL,))}, "the copy runs there alone"
    assert "now readers own, writers own" in _out(console)
    assert f"{name}, a copy of shared, is its own agent" in _out(console)

    await channels_rule_set(**where, channel_id=CHANNEL, readers="inside")
    async with db_session_factory() as s:
        inside = await load_access_policy(s, tenant_id=tenant_id)
    assert inside.channel_rules == {CHANNEL: ChannelRule(readers="inside")}
    assert name in inside.agent_rules, "the agent rule stays unless released"
    await channels_rule_set(**where, channel_id=CHANNEL, readers="any", release_agents=True)
    async with db_session_factory() as s:
        opened = await load_access_policy(s, tenant_id=tenant_id)
    assert opened == TenantAccessPolicy(), "released, nothing is left"


async def _isolatable(
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: str,
    workspace_id: str,
    channels: tuple[str, str] = (CHANNEL, OTHER_CHANNEL),
) -> uuid.UUID:
    """A tenant whose channel answers with a shared agent, so keeping it to its own needs a copy."""
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id=workspace_id)
        for channel in channels:
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
                tenant_id=tenant.id,
                agent_name="shared",
                mode="agent",
            )
    return tenant.id


def _isolate_settings(*, keys: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        cli=FakeCliSettings.cli,
        discord=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "t")),
        mcp=SimpleNamespace(public_url=None),
        crypto=SimpleNamespace(
            keys=tuple(SimpleNamespace(get_secret_value=lambda k=k: k) for k in keys)
        ),
    )


async def test_rule_set_copies_without_a_label_when_discord_errors(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed channel lookup names the copy after the channel id, as the tool does."""
    tenant_id = await _isolatable(db_session_factory, "discord", GUILD)
    rt = build_cli_runtime(
        db_session_factory, anthropic=_ma(tenant_id, "shared"), settings=_isolate_settings()
    )
    console = _console()
    failing = httpx.MockTransport(lambda _request: httpx.Response(500, json={}))

    await channels_rule_set(
        rt=rt,
        console=console,
        platform="discord",
        workspace_id=GUILD,
        channel_id=CHANNEL,
        readers="own",
        copy_from="shared",
        discord_transport=failing,
    )

    async with db_session_factory() as s:
        policy = await load_access_policy(s, tenant_id=tenant_id)
    assert policy.channel_rules == {CHANNEL: OWN}, "the lookup error never aborts the change"
    assert "channel-000002, a copy of shared, is its own agent" in _out(console)


async def test_rule_set_names_a_slack_copy_after_the_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Slack name is read with the workspace's bot token, as the tool reads it."""
    key = Fernet.generate_key().decode()
    tenant_id = await _isolatable(db_session_factory, "slack", "T1", ("C0LAUNCH1", "C0OTHER1"))
    async with db_session_factory.begin() as session:
        await upsert_slack_bot_token(
            session,
            team_id="T1",
            encrypted_token=encrypt_token(build_multifernet((key,)), "xoxb-test"),
        )
    rt = build_cli_runtime(
        db_session_factory,
        anthropic=_ma(tenant_id, "shared"),
        settings=_isolate_settings(keys=(key,)),
    )
    console = _console()

    def conversations_info(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer xoxb-test", "with the bot token"
        assert request.url.params["channel"] == "C0LAUNCH1"
        return httpx.Response(200, json={"ok": True, "channel": {"name": "launch-room"}})

    await channels_rule_set(
        rt=rt,
        console=console,
        platform="slack",
        workspace_id="T1",
        channel_id="C0LAUNCH1",
        readers="own",
        copy_from="shared",
        slack_transport=httpx.MockTransport(conversations_info),
    )

    assert "launch-room, a copy of shared, is its own agent" in _out(console)


async def test_rule_set_takes_a_teams_thread_as_its_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Teams takes rules as Discord and Slack do; the copy is named from the channel id."""
    other = "19:other@thread.tacv2"
    tenant_id = await _isolatable(db_session_factory, "teams", "tid", (TEAMS_CHANNEL, other))
    rt = build_cli_runtime(
        db_session_factory, anthropic=_ma(tenant_id, "shared"), settings=_isolate_settings()
    )
    console = _console()

    await channels_rule_set(
        rt=rt,
        console=console,
        platform="teams",
        workspace_id="tid",
        channel_id=f"{TEAMS_CHANNEL};messageid=1700000000000",
        readers="own",
        copy_from="shared",
    )

    async with db_session_factory() as s:
        policy = await load_access_policy(s, tenant_id=tenant_id)
    assert policy.channel_rules == {TEAMS_CHANNEL: OWN}, "the thread's channel takes the rule"
    assert policy.agent_rules == {"channel-growth": AgentRule(runs_in=(TEAMS_CHANNEL,))}, (
        "the copy, named from the id, runs in the channel alone"
    )
    assert "channel-growth, a copy of shared, is its own agent" in _out(console)


async def test_rule_set_refuses_bad_flag_mixes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = build_cli_runtime(db_session_factory)
    base = {"rt": rt, "console": _console(), "workspace_id": "w", "channel_id": CHANNEL}
    with pytest.raises(typer.BadParameter, match="unsupported platform"):
        await channels_rule_set(**base, platform="cli", readers="own")
    with pytest.raises(typer.BadParameter, match="pass --readers, --writers or --release"):
        await channels_rule_set(**base, platform="discord")
    with pytest.raises(typer.BadParameter, match="--readers: expected one of any, inside, own"):
        await channels_rule_set(**base, platform="discord", readers="sealed")
    with pytest.raises(typer.BadParameter, match="takes only --writers"):
        await channels_rule_set(**base, platform="discord", readers="inside", category=True)


async def test_rule_set_keeps_a_thread_rule_to_the_thread_flag(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only `--thread` sets one thread's rule; a thread id alone names its channel."""
    tenant_id = await _isolatable(db_session_factory, "slack", "T1", ("C01AB", "C02CD"))
    rt = build_cli_runtime(
        db_session_factory, anthropic=_ma(tenant_id, "shared"), settings=_isolate_settings()
    )
    args = {"rt": rt, "console": _console(), "platform": "slack", "workspace_id": "T1"}
    thread = "C01AB:1700000000.000200"
    with pytest.raises(typer.BadParameter, match="--thread takes only --readers inside or any"):
        await channels_rule_set(**args, channel_id=thread, readers="own", thread=True)
    with pytest.raises(typer.BadParameter, match="not a Slack channel_id:thread_ts"):
        await channels_rule_set(**args, channel_id="C01AB", readers="inside", thread=True)
    await channels_rule_set(**args, channel_id=thread, readers="inside", thread=True)
    await channels_rule_set(**args, channel_id="C02CD:1700000000.000300", writers="none")
    async with db_session_factory() as s:
        policy = await load_access_policy(s, tenant_id=tenant_id)
    assert policy.channel_rules == {
        thread: ChannelRule(readers="inside"),
        "C02CD": ChannelRule(writers="none"),
    }


async def test_rule_set_writers_on_a_teams_channel_and_refusals_write_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _isolatable(db_session_factory, "teams", "tid", (TEAMS_CHANNEL, CHANNEL))
    rt = build_cli_runtime(
        db_session_factory, anthropic=_ma(tenant_id, "shared"), settings=_isolate_settings()
    )
    args = {"rt": rt, "platform": "teams", "workspace_id": "tid"}
    console = _console()
    await channels_rule_set(
        **args,
        console=console,
        channel_id=f"{TEAMS_CHANNEL};messageid=1700000000000",
        readers="inside",
        writers="none",
    )
    assert "now readers inside, writers none" in _out(console), _out(console)
    console = _console()
    with pytest.raises(typer.Exit):
        await channels_rule_set(**args, console=console, channel_id=TEAMS_CHANNEL, writers="own")
    assert "Nothing was changed" in _out(console), _out(console)
    async with db_session_factory() as s:
        policy = await load_access_policy(s, tenant_id=tenant_id)
    assert policy.channel_rules == {TEAMS_CHANNEL: ChannelRule(readers="inside", writers="none")}


async def test_agents_rule_set_limits_where_an_agent_runs(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _isolatable(db_session_factory, "discord", GUILD)
    rt = build_cli_runtime(
        db_session_factory, anthropic=_ma(tenant_id, "shared"), settings=_isolate_settings()
    )
    args = {"rt": rt, "platform": "discord", "workspace_id": GUILD, "agent_name": "shared"}
    with pytest.raises(typer.BadParameter, match="pass --runs-in, --anywhere or --nowhere"):
        await agents_rule_set(**args, console=_console(), anywhere=True, nowhere=True)
    console = _console()
    await agents_rule_set(**args, console=console, runs_in=[CHANNEL])
    assert f"shared runs only in {CHANNEL}" in _out(console), _out(console)
    assert "still set to answer outside" in _out(console), "it is OTHER_CHANNEL's default"
    async with db_session_factory() as s:
        policy = await load_access_policy(s, tenant_id=tenant_id)
    assert policy.agent_rules == {"shared": AgentRule(runs_in=(CHANNEL,))}
    await agents_rule_set(**args, console=_console(), anywhere=True)
    async with db_session_factory() as s:
        assert (await load_access_policy(s, tenant_id=tenant_id)).agent_rules == {}


async def test_list_shows_the_balance_and_each_configured_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The CLI twin of get_tenant_summary: the same JSON, and a table by default."""
    rt = build_cli_runtime(db_session_factory)
    await provision_tenant(db_session_factory, platform="slack", workspace_id="T1")
    args = {"rt": rt, "platform": "slack", "workspace_id": "T1"}
    await budget_set(
        **args,
        console=_console(),
        channel_id="C1",
        usd="5",
        window="monthly",
        starts_at=None,
        ends_at=None,
    )
    await channels_admins_set(
        **args, console=_console(), channel_id="C2", roles=[], users=["U1"], as_json=False
    )

    json_console = _console()
    await channels_list(**args, console=json_console, as_json=True)
    summary = json.loads(_out(json_console))
    channels = {c["channel_id"]: c for c in summary["channels"]}
    assert summary["balance_usd"] == "0.00", "a fresh tenant has no balance"
    assert channels["C1"]["budget"]["limit_usd"] == "5.00", "C1 is listed for its budget"
    assert channels["C2"]["admins"] == {"role_ids": [], "user_ids": ["U1"]}, "C2 for its admins"

    table = _console()
    await channels_list(**args, console=table, as_json=False)
    text = _out(table)
    assert "balance $0.00 (prepaid)" in text, "the table is headed by the balance"
    assert "$5.00" in text and "(monthly)" in text, "and shows the budget"


async def test_list_json_carries_each_channels_readers_and_writers(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An operator reads the access policy anyway, so the CLI adds both sides of the rule."""
    rt = build_cli_runtime(db_session_factory)
    await provision_tenant(db_session_factory, platform="slack", workspace_id="T1")
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id="T1")
    async with db_session_factory.begin() as s:
        await set_access_policy(
            s,
            tenant_id=tenant_id,
            policy=TenantAccessPolicy(
                channel_rules={"C1": OWN, "C2": ChannelRule(writers="none")},
                agent_rules={"a": AgentRule(runs_in=("C1",))},
            ),
        )

    await channels_admins_set(
        rt=rt,
        platform="slack",
        workspace_id="T1",
        console=_console(),
        channel_id="C2",
        roles=[],
        users=["U1"],
        as_json=False,
    )
    console = _console()
    await channels_list(rt=rt, platform="slack", workspace_id="T1", console=console, as_json=True)
    channels = {c["channel_id"]: c for c in json.loads(_out(console))["channels"]}
    assert (channels["C1"]["readers"], channels["C1"]["writers"]) == ("own", "own")
    assert (channels["C2"]["readers"], channels["C2"]["writers"]) == ("any", "none")


@pytest.mark.parametrize(
    ("argv", "target", "expected"),
    [
        (
            ["channels", "rule", "set", "discord", GUILD, CHANNEL, "--readers", "own"],
            "channels_rule_set",
            {"channel_id": CHANNEL, "readers": "own", "writers": None, "copy_from": None},
        ),
        (
            ["agents", "rule", "set", "discord", GUILD, "x", "--runs-in", CHANNEL],
            "agents_rule_set",
            {"agent_name": "x", "runs_in": [CHANNEL], "anywhere": False, "nowhere": False},
        ),
    ],
    ids=["channel", "agent"],
)
def test_rule_commands_parse_their_flags(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], target: str, expected: dict[str, object]
) -> None:
    calls: list[dict[str, object]] = []

    async def record(**kwargs: object) -> None:
        calls.append(kwargs)

    @asynccontextmanager
    async def no_runtime(_settings: object) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(channels_mod, target, record)
    monkeypatch.setattr(channels_mod, "build_runtime", no_runtime)
    monkeypatch.setattr(channels_mod, "load_settings", lambda: None)

    run = CliRunner().invoke(main_mod.app, argv)

    assert run.exit_code == 0, run.output
    assert len(calls) == 1 and expected.items() <= calls[0].items(), calls

"""The panel's shell reads: roster state, GitHub facts and creator attribution."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.hydrate import (
    BOT_INSTALL_PERMISSIONS,
    WEBHOOK_CHECK_LIMIT,
    answering_channel_ids,
    github_facts,
    load_roster_state,
    resolve_attributions,
    webhook_blocks,
    webhook_fix_url,
)
from daimon.adapters.discord.agent_setup.state import PanelState, WebhookBlock
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.roster import RosterAgent
from daimon.core.scope import ChannelConfigRow, DeploymentDefault
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.turn.deps import build_turn_deps
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 700100001
CHANNEL_ID = 222
THREAD_ID = 333


def _runtime(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: Any,
    default: DeploymentDefault,
    settings_overrides: dict[str, Any] | None = None,
) -> DiscordRuntime:
    payload: dict[str, Any] = {
        "database": {"url": "postgresql+asyncpg://test:test@localhost/daimon_test"},
        "anthropic": {"api_key": "test"},
    }
    payload.update(settings_overrides or {})
    settings = Settings.model_validate(payload)
    cache = new_resolver_cache()
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=default,
        resolver_cache=cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=default,
            resolver_cache=cache,
            billing_config=None,
        ),
    )


def _interaction(*, in_thread: bool) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    interaction.guild_id = GUILD_ID
    parent = MagicMock(spec=discord.TextChannel)
    parent.id = CHANNEL_ID
    parent.name = "general"
    if in_thread:
        thread = MagicMock(spec=discord.Thread)
        thread.id = THREAD_ID
        thread.parent = parent
        interaction.channel = thread
    else:
        interaction.channel = parent
    return interaction


# ---------------------------------------------------------------------------
# webhook_fix_url
# ---------------------------------------------------------------------------

APPLICATION_ID = 5550001


def _identity_runtime(*, enabled: bool) -> DiscordRuntime:
    settings = Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://test:test@localhost/daimon_test"},
            "anthropic": {"api_key": "test"},
            "agent_identity": {"enabled": enabled},
        }
    )
    return cast(DiscordRuntime, SimpleNamespace(settings=settings))


def _guild_interaction(*, manage_webhooks: bool, administrator: bool = False) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.application_id = APPLICATION_ID
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = GUILD_ID
    interaction.guild.me.guild_permissions = discord.Permissions(
        manage_webhooks=manage_webhooks, administrator=administrator
    )
    return interaction


def test_an_admin_without_manage_webhooks_gets_the_reauthorize_link() -> None:
    url = webhook_fix_url(
        _identity_runtime(enabled=True),
        _guild_interaction(manage_webhooks=False),
        is_admin=True,
    )

    assert BOT_INSTALL_PERMISSIONS == 326954503232
    assert url == (
        f"https://discord.com/oauth2/authorize?client_id={APPLICATION_ID}"
        "&permissions=326954503232&scope=bot+applications.commands"
        f"&guild_id={GUILD_ID}&disable_guild_select=true"
    ), "the link re-adds this bot to this server with the full install permission set"


@pytest.mark.parametrize(
    ("enabled", "manage_webhooks", "administrator", "is_admin", "reason"),
    [
        (True, True, False, True, "the permission is already granted"),
        (True, False, True, True, "administrator implies Manage Webhooks"),
        (True, False, False, False, "only an admin can re-authorize the bot"),
        (False, False, False, True, "with identity off there is nothing to fix"),
    ],
)
def test_the_reauthorize_link_is_withheld(
    enabled: bool, manage_webhooks: bool, administrator: bool, is_admin: bool, reason: str
) -> None:
    url = webhook_fix_url(
        _identity_runtime(enabled=enabled),
        _guild_interaction(manage_webhooks=manage_webhooks, administrator=administrator),
        is_admin=is_admin,
    )

    assert url is None, reason


def test_an_excluded_guild_gets_no_reauthorize_link() -> None:
    runtime = _identity_runtime(enabled=True)
    runtime.settings.agent_identity.excluded_discord_guild_ids = [str(GUILD_ID)]

    url = webhook_fix_url(runtime, _guild_interaction(manage_webhooks=False), is_admin=True)

    assert url is None, "a guild excluded from identity never posts through webhooks"


# ---------------------------------------------------------------------------
# webhook_blocks
# ---------------------------------------------------------------------------
#
# These run on real discord.py guild objects built from gateway payloads, so the
# effective permission comes from discord.py's own overwrite arithmetic rather
# than from a mock that answers whatever the test wanted.

BOT_ID = 900
CLIENT_ROLE_ID = 2001
BOT_ROLE_ID = 2002
WEBHOOKS = str(discord.Permissions(manage_webhooks=True).value)


def _role(role_id: int, name: str, *, position: int, permissions: int = 0) -> dict[str, Any]:
    return {
        "id": role_id,
        "name": name,
        "permissions": str(permissions),
        "position": position,
        "color": 0,
        "hoist": False,
        "managed": False,
        "mentionable": False,
    }


def _overwrite(target: int, *, allow: bool, member: bool = False) -> dict[str, Any]:
    return {
        "id": str(target),
        "type": 1 if member else 0,
        "allow": WEBHOOKS if allow else "0",
        "deny": "0" if allow else WEBHOOKS,
    }


def _channel(channel_id: int, name: str, *overwrites: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 0,
        "name": name,
        "position": 0,
        "guild_id": GUILD_ID,
        "permission_overwrites": list(overwrites),
    }


def _thread(thread_id: int, parent_id: int) -> dict[str, Any]:
    return {
        "id": thread_id,
        "type": 11,
        "name": "a thread",
        "guild_id": GUILD_ID,
        "parent_id": parent_id,
        "owner_id": 1,
        "message_count": 0,
        "member_count": 0,
        "rate_limit_per_user": 0,
        "thread_metadata": {
            "archived": False,
            "auto_archive_duration": 1440,
            "archive_timestamp": "2026-10-10T00:00:00+00:00",
            "locked": False,
        },
    }


def _guild(
    *channels: dict[str, Any],
    server_grant: bool = True,
    threads: tuple[dict[str, Any], ...] = (),
) -> discord.Guild:
    """A cached guild where Daimon holds the 'insighta client' and 'Daimon' roles."""
    state = MagicMock()
    state.self_id = BOT_ID
    state.user.id = BOT_ID
    state.member_cache_flags = discord.MemberCacheFlags.all()

    def store_user(data: Any, cache: bool = True) -> discord.User:
        return discord.User(state=state, data=data)

    state.store_user = store_user
    everyone = discord.Permissions(view_channel=True, manage_webhooks=server_grant)
    return discord.Guild(
        data=cast(
            Any,
            {
                "id": GUILD_ID,
                "name": "PyMC Labs",
                "owner_id": 1,
                "roles": [
                    _role(GUILD_ID, "@everyone", position=0, permissions=everyone.value),
                    _role(CLIENT_ROLE_ID, "insighta client", position=2),
                    _role(BOT_ROLE_ID, "Daimon", position=1),
                ],
                "members": [
                    {
                        "user": {
                            "id": BOT_ID,
                            "username": "Daimon",
                            "discriminator": "0",
                            "avatar": None,
                            "bot": True,
                        },
                        "roles": [str(CLIENT_ROLE_ID), str(BOT_ROLE_ID)],
                        "joined_at": None,
                        "deaf": False,
                        "mute": False,
                        "flags": 0,
                    }
                ],
                "channels": list(channels),
                "threads": list(threads),
            },
        ),
        state=state,
    )


def _guild_panel(guild: discord.Guild, *, channel_id: int = CHANNEL_ID) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.application_id = APPLICATION_ID
    interaction.guild = guild
    interaction.guild_id = GUILD_ID
    interaction.channel = guild.get_channel_or_thread(channel_id)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42
    return interaction


def test_a_role_overwrite_that_denies_webhooks_names_the_channel_and_role() -> None:
    guild = _guild(_channel(CHANNEL_ID, "insighta-client", _overwrite(CLIENT_ROLE_ID, allow=False)))

    assert webhook_blocks(guild, [str(CHANNEL_ID)]) == (
        WebhookBlock(
            channel_name="insighta-client", denied_by="roles", role_names=("insighta client",)
        ),
    ), "the server grant is undone by the 'insighta client' role's deny in that channel"


def test_an_everyone_overwrite_that_denies_webhooks_names_everyone() -> None:
    guild = _guild(_channel(CHANNEL_ID, "general", _overwrite(GUILD_ID, allow=False)))

    assert webhook_blocks(guild, [str(CHANNEL_ID)]) == (
        WebhookBlock(channel_name="general", denied_by="@everyone"),
    )


def test_a_member_deny_names_daimon_itself() -> None:
    guild = _guild(_channel(CHANNEL_ID, "general", _overwrite(BOT_ID, allow=False, member=True)))

    assert webhook_blocks(guild, [str(CHANNEL_ID)]) == (
        WebhookBlock(channel_name="general", denied_by="member"),
    )


@pytest.mark.parametrize(
    ("overwrites", "reason"),
    [
        (
            (_overwrite(CLIENT_ROLE_ID, allow=False), _overwrite(BOT_ID, allow=True, member=True)),
            "a member allow beats every role deny",
        ),
        (
            (_overwrite(CLIENT_ROLE_ID, allow=False), _overwrite(BOT_ROLE_ID, allow=True)),
            "one role's allow beats another role's deny",
        ),
        (
            (_overwrite(GUILD_ID, allow=False), _overwrite(BOT_ROLE_ID, allow=True)),
            "a role allow beats the @everyone deny",
        ),
        ((), "with no overwrite the server grant stands"),
    ],
)
def test_an_overriding_allow_leaves_the_channel_out(
    overwrites: tuple[dict[str, Any], ...], reason: str
) -> None:
    guild = _guild(_channel(CHANNEL_ID, "general", *overwrites))

    assert webhook_blocks(guild, [str(CHANNEL_ID)]) == (), reason


def test_a_thread_is_checked_through_its_parent_channel() -> None:
    guild = _guild(
        _channel(CHANNEL_ID, "insighta-client", _overwrite(CLIENT_ROLE_ID, allow=False)),
        threads=(_thread(THREAD_ID, CHANNEL_ID),),
    )

    blocks = webhook_blocks(guild, [str(THREAD_ID), str(CHANNEL_ID)])

    assert [block.channel_name for block in blocks] == ["insighta-client"], (
        "a thread posts through its parent's webhook, and the parent is named once"
    )


def test_unknown_and_unparseable_channel_ids_are_skipped() -> None:
    guild = _guild(_channel(CHANNEL_ID, "general", _overwrite(GUILD_ID, allow=False)))

    blocks = webhook_blocks(guild, ["not-a-snowflake", "999999", str(CHANNEL_ID)])

    assert [block.channel_name for block in blocks] == ["general"]


def test_only_the_first_ten_distinct_channels_are_checked() -> None:
    ids = range(5000, 5000 + WEBHOOK_CHECK_LIMIT + 3)
    guild = _guild(
        *(_channel(cid, f"room-{cid}", _overwrite(GUILD_ID, allow=False)) for cid in ids)
    )

    blocks = webhook_blocks(guild, [str(cid) for cid in ids])

    assert len(blocks) == WEBHOOK_CHECK_LIMIT == 10


def test_answering_channels_are_the_panel_then_settings_then_agent_rules() -> None:
    tenant_id = uuid.uuid4()
    policy = TenantAccessPolicy(
        agent_rules={"a": AgentRule(runs_in=("40", "30")), "b": AgentRule(runs_in=("30",))}
    )
    rows = [
        ChannelConfigRow(tenant_id=tenant_id, channel_id="20", agent_name="x"),
        ChannelConfigRow(tenant_id=tenant_id, channel_id="10", agent_name="y"),
    ]

    assert answering_channel_ids("1", rows, policy) == ["1", "10", "20", "30", "40"]


async def _panel_state(
    db_session_factory: async_sessionmaker[AsyncSession],
    guild: discord.Guild,
    *,
    channel_id: int = CHANNEL_ID,
    enabled: bool = True,
    is_admin: bool = True,
) -> PanelState:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
    router = MARouter()
    router.add_agent_list(ma_agent(id="ag_specialist", name="specialist", tenant_id=tenant.id))
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        default=DeploymentDefault(agent_name="specialist"),
        settings_overrides={"agent_identity": {"enabled": enabled}},
    )
    return await load_roster_state(
        runtime, _guild_panel(guild, channel_id=channel_id), tenant_id=tenant.id, is_admin=is_admin
    )


async def test_an_admin_panel_in_a_thread_reports_the_parents_blocking_overwrite(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild = _guild(
        _channel(CHANNEL_ID, "insighta-client", _overwrite(CLIENT_ROLE_ID, allow=False)),
        threads=(_thread(THREAD_ID, CHANNEL_ID),),
    )

    state = await _panel_state(db_session_factory, guild, channel_id=THREAD_ID)

    assert state.webhook_fix_url is None, "the server grants Manage Webhooks"
    assert state.webhook_blocks == (
        WebhookBlock(
            channel_name="insighta-client", denied_by="roles", role_names=("insighta client",)
        ),
    ), "the panel opened in a thread checks the parent channel"


async def test_a_missing_server_grant_gets_the_link_instead_of_channel_lines(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild = _guild(
        _channel(CHANNEL_ID, "insighta-client", _overwrite(CLIENT_ROLE_ID, allow=False)),
        server_grant=False,
    )

    state = await _panel_state(db_session_factory, guild)

    assert state.webhook_fix_url is not None, "without the server grant re-authorizing is the fix"
    assert state.webhook_blocks == (), "channel overwrites are beside the point without the grant"


@pytest.mark.parametrize(
    ("enabled", "is_admin", "reason"),
    [
        (True, False, "only an admin can change channel permissions"),
        (False, True, "with identity off agents post through no webhook"),
    ],
)
async def test_channel_blocks_are_withheld(
    enabled: bool,
    is_admin: bool,
    reason: str,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild = _guild(_channel(CHANNEL_ID, "insighta-client", _overwrite(CLIENT_ROLE_ID, allow=False)))

    state = await _panel_state(db_session_factory, guild, enabled=enabled, is_admin=is_admin)

    assert state.webhook_blocks == (), reason
    assert state.webhook_fix_url is None, reason


# ---------------------------------------------------------------------------
# github_facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("github", "expected_pat", "expected_app"),
    [
        ({}, False, False),
        ({"fallback_pat": "ghp_x"}, True, False),
        ({"app_id": "1234"}, False, False),
        ({"app_id": "1234", "app_private_key": "-----BEGIN KEY-----"}, False, True),
        (
            {
                "fallback_pat": "ghp_x",
                "app_id": "1234",
                "app_private_key": "-----BEGIN KEY-----",
            },
            True,
            True,
        ),
    ],
)
def test_github_facts_report_the_app_configured_only_with_both_halves(
    github: dict[str, str],
    expected_pat: bool,
    expected_app: bool,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=MagicMock(),
        default=DeploymentDefault(),
        settings_overrides={"github": github},
    )

    facts = github_facts(runtime)

    assert facts.has_fallback_pat is expected_pat, (
        "the fallback PAT is configured exactly when it is set"
    )
    assert facts.app_configured is expected_app, (
        "an app id with no private key mints nothing and must not read as configured"
    )


# ---------------------------------------------------------------------------
# resolve_attributions
# ---------------------------------------------------------------------------


async def test_resolve_attributions_names_a_creator_with_a_discord_principal(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant.id, platform="discord", external_id="12345"
        )
    runtime = _runtime(
        sessionmaker=db_session_factory, anthropic=MagicMock(), default=DeploymentDefault()
    )
    state = PanelState(
        roster=[],
        selected=None,
        account_id=principal.account_id,
        guild_account_id=derive_guild_account_uuid(tenant.id),
        guild_id=GUILD_ID,
    )
    agent = RosterAgent(
        name="churn-explorer",
        ma_agent_id="ag_churn",
        model_id="claude-sonnet-4-6",
        is_built_in=False,
        created_by_account_id=principal.account_id,
    )

    attributions = await resolve_attributions(runtime, state=state, agents=[agent])

    assert attributions == {"ag_churn": "<@12345>"}, (
        "a creator with a live Discord principal is rendered as a mention"
    )


async def test_resolve_attributions_omits_the_guild_stamp_and_unknown_accounts(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
    runtime = _runtime(
        sessionmaker=db_session_factory, anthropic=MagicMock(), default=DeploymentDefault()
    )
    guild_account_id = derive_guild_account_uuid(tenant.id)
    state = PanelState(
        roster=[],
        selected=None,
        account_id=account.id,
        guild_account_id=guild_account_id,
        guild_id=GUILD_ID,
    )
    agents = [
        RosterAgent(
            name="guild-made",
            ma_agent_id="ag_guild",
            model_id="claude-sonnet-4-6",
            is_built_in=False,
            created_by_account_id=guild_account_id,
        ),
        RosterAgent(
            name="no-principal",
            ma_agent_id="ag_orphan",
            model_id="claude-sonnet-4-6",
            is_built_in=False,
            created_by_account_id=account.id,
        ),
        RosterAgent(
            name="unstamped",
            ma_agent_id="ag_unstamped",
            model_id="claude-sonnet-4-6",
            is_built_in=False,
        ),
    ]

    attributions = await resolve_attributions(runtime, state=state, agents=agents)

    assert attributions == {}, (
        "the shared guild stamp names nobody, and an account with no Discord principal "
        "must not be turned into an invented handle"
    )


# ---------------------------------------------------------------------------
# load_roster_state
# ---------------------------------------------------------------------------


async def test_load_roster_state_reports_the_parent_channel_and_the_answering_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
    router = MARouter()
    router.add_agent_list(
        ma_agent(id="ag_specialist", name="specialist", tenant_id=tenant.id),
        ma_agent(
            id="ag_daimon", name="daimon", tenant_id=tenant.id, metadata={"daimon_managed": "true"}
        ),
    )
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        default=DeploymentDefault(agent_name="specialist"),
    )

    state = await load_roster_state(
        runtime, _interaction(in_thread=False), tenant_id=tenant.id, is_admin=True
    )

    assert state.channel_id == CHANNEL_ID, "the panel is about the channel it was opened in"
    assert state.channel_name == "general", "the header needs the channel's name, not its id"
    assert state.thread_id is None, "outside a thread there is no thread to resolve against"
    assert state.thread_context is None, "no thread, no thread line"
    assert state.answering is not None and state.answering.name == "specialist", (
        "the deployment default answers here when nothing else is set"
    )
    assert state.roster_agents[0].name == "specialist", "the answering agent leads the roster"
    assert state.selected_agent == state.answering, "setup starts pointed at whoever answers here"
    assert state.is_admin is True, "the caller's role is carried onto the state"
    assert state.guild_account_id == derive_guild_account_uuid(tenant.id), (
        "the guild stamp is needed to recognise agents nobody personally made"
    )


async def test_load_roster_state_reads_the_thread_binding_and_seeds_its_target(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        await create_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(CHANNEL_ID),
            thread_id=str(THREAD_ID),
            responder_ma_agent_id="ag_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id="ag_specialist",
            configuration_target_name="specialist",
        )
    router = MARouter()
    router.add_agent_list(
        ma_agent(id="ag_specialist", name="specialist", tenant_id=tenant.id),
        ma_agent(
            id="ag_daimon", name="daimon", tenant_id=tenant.id, metadata={"daimon_managed": "true"}
        ),
    )
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        default=DeploymentDefault(agent_name="specialist"),
    )

    state = await load_roster_state(
        runtime, _interaction(in_thread=True), tenant_id=tenant.id, is_admin=False
    )

    assert state.channel_id == CHANNEL_ID, "inside a thread the panel still describes the parent"
    assert state.thread_id == str(THREAD_ID), (
        "the thread has to survive so Details resolves the same responder the thread does"
    )
    assert state.thread_context is not None, "a live binding produces a thread line"
    assert state.thread_context.kind == "setup", "the binding's kind decides what the line says"
    assert state.thread_context.responder_name == "Daimon", "the line names the thread's responder"
    assert state.thread_context.target_name == "specialist", "and what is being set up"
    assert state.selected_agent is not None and state.selected_agent.name == "specialist", (
        "a setup thread seeds the selection with the agent it was opened about"
    )


async def test_load_roster_state_registers_the_caller_as_a_platform_principal(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    runtime = _runtime(
        sessionmaker=db_session_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        default=DeploymentDefault(),
    )

    state = await load_roster_state(
        runtime, _interaction(in_thread=False), tenant_id=tenant.id, is_admin=False
    )

    async with db_session_factory() as session, session.begin():
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant.id, platform="discord", external_id="42"
        )
    assert state.account_id == principal.account_id, (
        "the panel's writes are attributed to the caller's own principal, not a fresh one"
    )
    assert state.platform_principal_id == principal.id, "and to that principal's identity"
    assert state.roster_agents == (), "an empty install renders an empty roster, not an error"
    assert state.answering is None, "nothing answers where nothing is configured"


async def test_load_roster_state_refuses_a_place_with_no_channel() -> None:
    """A panel opened somewhere with no guild channel has nothing to describe."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = GUILD_ID
    interaction.channel = None
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 42

    with pytest.raises(DaimonError, match="server channel"):
        await load_roster_state(MagicMock(), interaction, tenant_id=uuid.uuid4(), is_admin=False)

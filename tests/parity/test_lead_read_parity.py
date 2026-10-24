"""Every lead reader decides alike: MCP tools, the `/leads` queue and a card's Details.

One fake Discord REST transport serves a guild whose members differ only in
their native permissions on the sales channel A the leads were captured for,
the channel B configured since, and the leads' dossier threads. No provider,
Discord or Gmail request leaves the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import discord.http
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from daimon.adapters.discord.commands.leads import LeadsCog
from daimon.adapters.discord.tool_confirmation import (
    LEAD_DETAILS_REFUSED_MESSAGE,
    discord_confirmation_hook,
)
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.services.lead_intake import HttpIntakePorts
from daimon.adapters.mcp.tools.discord._client import rest_client
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    RecruitmentSettings,
    Settings,
)
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.discord_membership import DiscordRestMembershipResolver
from daimon.core.github_credentials import build_multifernet
from daimon.core.lead_intake import (
    IntakeConfig,
    IntakeRecord,
    LeadEvidence,
    LeadSource,
    LeadSubmission,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.membership import Membership
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.lead_intake import IntakeStore
from daimon.core.stores.turn_origins import create_origin
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

SECRET = b"lead-read-parity-secret-32-bytes"
FERNET_KEY = Fernet.generate_key().decode("ascii")
GUILD, A, B, ORIGIN, THREAD, PRIVATE, HOME = "111", "222", "444", "333", "999", "998", "777"
ROLE = "555"
VIEW, SEND, HISTORY = 1 << 10, 1 << 11, 1 << 16
ROLE_PERMS = VIEW | SEND | HISTORY | (1 << 35) | (1 << 38)
# Who each member is, natively. All hold the admitted role.
A_ONLY, B_ONLY, NO_HISTORY, OUTSIDE_PRIVATE, BOTH = "42", "43", "44", "45", "46"
MEMBERS = (A_ONLY, B_ONLY, NO_HISTORY, OUTSIDE_PRIVATE, BOTH)
PRIVATE_THREAD_MEMBERS = {A_ONLY, BOTH}
KINDS = ("capture", "threaded", "private")
# Who may read each record captured for A, whatever is configured now.
READERS = {
    "capture": {A_ONLY, OUTSIDE_PRIVATE, BOTH},
    "threaded": {A_ONLY, OUTSIDE_PRIVATE, BOTH},
    "private": {A_ONLY, BOTH},
}


def _sentinel(kind: str) -> str:
    return f"Ada Lovelace SENTINEL {kind}"


def _deny(user: str, bits: int) -> dict[str, Any]:
    return {"id": user, "type": 1, "allow": "0", "deny": str(bits)}


def _guild() -> dict[str, Any]:
    everyone = {
        "id": GUILD,
        "name": "@everyone",
        "permissions": "0",
        "position": 0,
        "color": 0,
        "hoist": False,
        "managed": False,
        "mentionable": False,
        "flags": 0,
    }
    return {
        "id": GUILD,
        "name": "guild",
        "owner_id": "1",
        "afk_timeout": 0,
        "verification_level": 0,
        "default_message_notifications": 0,
        "explicit_content_filter": 0,
        "roles": [
            everyone,
            {**everyone, "id": ROLE, "name": "sales", "permissions": str(ROLE_PERMS)},
        ],
        "emojis": [],
        "features": [],
        "mfa_level": 0,
        "system_channel_flags": 0,
        "premium_tier": 0,
        "preferred_locale": "en-US",
        "nsfw_level": 0,
        "premium_progress_bar_enabled": False,
        "stickers": [],
    }


def _member(user: str) -> dict[str, Any]:
    return {
        "user": {
            "id": user,
            "username": f"u{user}",
            "discriminator": "0001",
            "global_name": None,
            "avatar": None,
            "bot": False,
            "flags": 0,
        },
        "roles": [ROLE],
        "joined_at": "2024-01-01T00:00:00+00:00",
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


def _text(channel: str, overwrites: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": channel,
        "type": 0,
        "guild_id": GUILD,
        "name": f"c{channel}",
        "position": 0,
        "permission_overwrites": overwrites,
        "nsfw": False,
        "rate_limit_per_user": 0,
        "parent_id": None,
    }


def _thread(thread: str, *, private: bool) -> dict[str, Any]:
    return {
        "id": thread,
        "parent_id": A,
        "owner_id": "1",
        "name": "dossier",
        "type": 12 if private else 11,
        "message_count": 1,
        "member_count": 1,
        "thread_metadata": {
            "archived": False,
            "auto_archive_duration": 1440,
            "archive_timestamp": "2026-05-09T00:00:00+00:00",
        },
        "guild_id": GUILD,
    }


CHANNELS: dict[str, dict[str, Any]] = {
    A: _text(A, [_deny(B_ONLY, VIEW), _deny(NO_HISTORY, HISTORY)]),
    B: _text(B, [_deny(A_ONLY, VIEW), _deny(OUTSIDE_PRIVATE, VIEW), _deny(NO_HISTORY, HISTORY)]),
    ORIGIN: _text(ORIGIN, []),
    HOME: _text(HOME, []),
    THREAD: _thread(THREAD, private=False),
    PRIVATE: _thread(PRIVATE, private=True),
}


async def _discord(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
    last = route.url.rstrip("/").rsplit("/", 1)[-1]
    if route.path == "/guilds/{guild_id}":
        return _guild()
    if route.path == "/guilds/{guild_id}/members/{member_id}":
        return _member(last)
    if route.method == "GET" and route.path == "/channels/{channel_id}":
        return CHANNELS[str(route.channel_id)]
    if route.path == "/channels/{channel_id}/thread-members/{user_id}":
        if str(route.channel_id) == PRIVATE and last in PRIVATE_THREAD_MEMBERS:
            return {
                "id": PRIVATE,
                "user_id": last,
                "join_timestamp": "2026-01-01T00:00:00+00:00",
                "flags": 0,
            }
        raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Member")
    raise AssertionError(f"unexpected Discord request {route.method} {route.path}")


@pytest.fixture(autouse=True)
def _transports(monkeypatch: pytest.MonkeyPatch) -> None:
    async def request(
        self: discord.http.HTTPClient, route: discord.http.Route, **kwargs: Any
    ) -> Any:
        return await _discord(route, kwargs)

    async def static_login(self: discord.http.HTTPClient, token: str) -> dict[str, Any]:
        return {"id": "1", "username": "bot", "discriminator": "0001", "avatar": None, "bot": True}

    async def membership(
        self: DiscordRestMembershipResolver, tenant_id: uuid.UUID, guild_id: str, user_id: str
    ) -> Membership:
        return Membership(is_member=True, role_ids=(ROLE,))

    async def no_mail(*_args: object) -> tuple[list[str], str | None]:
        return [], None

    monkeypatch.setattr(discord.http.HTTPClient, "request", request)
    monkeypatch.setattr(discord.http.HTTPClient, "static_login", static_login)
    monkeypatch.setattr(DiscordRestMembershipResolver, "_fetch_member", membership)
    monkeypatch.setattr(HttpIntakePorts, "scan", no_mail)


@pytest_asyncio.fixture
async def sessions(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@dataclass
class World:
    sessions: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    store: IntakeStore
    records: dict[str, uuid.UUID]
    tokens: dict[str, str]
    app: ASGIApp

    async def mcp(self, member: str, tool: str, **arguments: object) -> tuple[bool, str]:
        """(refused, text) for one MCP call as `member`."""
        result = await call_mcp_tool(
            self.app, token=self.tokens[member], name=tool, arguments=arguments
        )
        payload: dict[str, Any] = result.get("result", result)
        text = " ".join(str(item.get("text", "")) for item in payload.get("content", []))
        return bool(payload.get("isError")), text

    def runtime(self) -> MagicMock:
        runtime = MagicMock()
        runtime.settings.recruitment = RecruitmentSettings()
        runtime.sessionmaker = self.sessions
        runtime.turn_deps.fernet = build_multifernet((FERNET_KEY,))
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=Membership(is_member=True, role_ids=(ROLE,)))
        runtime.turn_deps.membership_resolver = resolver
        return runtime

    @contextlib.asynccontextmanager
    async def guild(self) -> AsyncIterator[discord.Guild]:
        async with rest_client("parity") as client:
            yield await client.fetch_guild(int(GUILD))

    async def leads(self, member: str, intake_id: uuid.UUID | None) -> str:
        """The ephemeral `/leads` reply for `member`."""
        async with self.guild() as guild:
            interaction = MagicMock()
            interaction.client.runtime = self.runtime()
            interaction.guild_id = int(GUILD)
            interaction.guild = guild
            interaction.channel = None
            interaction.channel_id = int(ORIGIN)
            interaction.user.id = int(member)
            interaction.response.defer = AsyncMock()
            interaction.followup.send = AsyncMock()
            cog = LeadsCog(MagicMock())
            await cog.leads.callback(  # pyright: ignore[reportCallIssue]
                cog, interaction, str(intake_id) if intake_id else None
            )
            return str(interaction.followup.send.await_args.args[0])

    async def details(
        self, member: str, intake_id: uuid.UUID, *, remove: str | None = None
    ) -> list[str]:
        """What `member` is sent on clicking Details of a resolve card for this lead."""
        call = ToolCall(
            tool_use_id="resolve",
            server_name=DAIMON_SERVER_NAME,
            tool_name="lead_intake_resolve",
            input={
                "intake_id": str(intake_id),
                "resolution": {"company_id": "200", "evidence": f"{_sentinel('card')} verified"},
            },
        )
        prompt = prompt_for_tool_call(
            call, requester_platform_user_id=A_ONLY, now=dt.datetime.now(dt.UTC)
        )
        async with self.guild() as guild:
            channel = MagicMock(spec=discord.TextChannel)
            channel.id, channel.guild = int(ORIGIN), guild
            posted: list[discord.ui.LayoutView] = []

            async def send(*, view: discord.ui.LayoutView, allowed_mentions: Any) -> MagicMock:
                posted.append(view)
                return MagicMock(edit=AsyncMock())

            channel.send = send
            hook = discord_confirmation_hook(
                channel, runtime=self.runtime(), tenant_id=self.tenant_id
            )
            waiting = asyncio.create_task(hook(prompt))
            while not posted:
                await asyncio.sleep(0)
            details = next(
                item
                for item in posted[0].walk_children()
                if isinstance(item, discord.ui.Button) and item.label == "Details"
            )
            if remove == "erase":
                from daimon.core.stores.lead_intake import erase_actor

                async with self.sessions() as session, session.begin():
                    await erase_actor(session, self.tenant_id, A_ONLY)
            click = MagicMock()
            click.user.id = int(member)
            click.response.defer = AsyncMock()
            click.followup.send = AsyncMock()
            with patch("daimon.core.stores.lead_intake.datetime", wraps=dt.datetime) as clock:
                clock.now.return_value = dt.datetime.now(dt.UTC) + dt.timedelta(
                    days=91 if remove == "expire" else 0
                )
                await details.callback(click)
            waiting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiting
            return [str(sent.args[0]) for sent in click.followup.send.await_args_list]


async def _world(
    sessions: async_sessionmaker[AsyncSession],
    *,
    reconfigure: bool = True,
    policy: TenantAccessPolicy | None = None,
    dm_origin: bool = False,
) -> World:
    now = dt.datetime.now(dt.UTC)
    tokens: dict[str, str] = {}
    async with sessions() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=GUILD)  # pyright: ignore[reportArgumentType]
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=policy or TenantAccessPolicy(invoker_role_ids=(ROLE,)),
        )
        for member in MEMBERS:
            principal = await make_platform_principal(
                session, platform="discord", external_id=member, tenant=tenant
            )
            await create_origin(
                session,
                tenant_id=tenant.id,
                account_id=principal.account_id,
                platform="discord",
                parent_channel_id=f"dm-{member}" if dm_origin else ORIGIN,
                thread_id=f"dm:{member}" if dm_origin else f"10{member}",
                responder_ma_agent_id="agent_x",
                responder_name="daimon",
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                expires_at=now + dt.timedelta(minutes=10),
                now=now,
                chat_session_id=f"session-{member}",
                attended=True,
                sender_foreign=False,
                shared_channel=False,
            )
            tokens[member], _, _ = await mint_chat_mcp_token(
                session,
                account_id=principal.account_id,
                agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_x"),
                secret=SECRET,
                chat_session_id=f"session-{member}",
                now=now,
            )
        await session.commit()
    store = IntakeStore(sessions, build_multifernet((FERNET_KEY,)), tenant.id)
    config = IntakeConfig(
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_x"),
        guild_id=GUILD,
        channel_id=A,
        source_connection_id="gmail",
        gmail_mailbox="owner@example.test",
        crm_connection_id="crm",
        portal_id="3",
        reconciliation_start=now,
    )
    credentials = {"hubspot_access_token": "synthetic"}
    await store.configure(config, credentials)
    records: dict[str, uuid.UUID] = {}
    for kind in KINDS:
        record = await store.accept(
            config,
            LeadSubmission(
                source=LeadSource(
                    kind="gmail",  # pyright: ignore[reportArgumentType]
                    mailbox="owner@example.test",
                    message_id=f"source-{kind}",
                ),
                evidence=LeadEvidence(original_text=_sentinel(kind)),
                classification="project",  # pyright: ignore[reportArgumentType]
                opportunity_description="Forecasting project",
                thread_title=_sentinel(kind),
                thread_content=_sentinel(kind),
            ),
            A_ONLY,
        )
        if kind != "capture":
            record.progress.discord_thread_id = THREAD if kind == "threaded" else PRIVATE
            await store.save(record)
        records[kind] = record.id
    if reconfigure:
        await store.configure(
            config.model_copy(update={"revision": 2, "channel_id": B}), credentials
        )
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr(SECRET.decode()), public_url=HttpUrl("https://x/mcp")
            ),
            discord=DiscordSettings(bot_token=SecretStr("parity-bot-token")),
            crypto=CryptoSettings(keys=(SecretStr(FERNET_KEY),)),
            _env_file=None,  # type: ignore[call-arg]  # isolate from repo .env
        ),
        sessionmaker=sessions,
    )
    return World(sessions, tenant.id, store, records, tokens, app)


async def test_mcp_get_leads_and_card_details_agree_for_every_member_and_record(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(sessions)
    for kind in KINDS:
        intake = world.records[kind]
        for member in MEMBERS:
            allowed = member in READERS[kind]
            refused, text = await world.mcp(member, "lead_intake_get", intake_id=str(intake))
            assert (not refused and _sentinel(kind) in text) is allowed, (kind, member, text)
            assert (_sentinel(kind) in text) is allowed, (kind, member)
            queue = await world.leads(member, intake)
            assert (_sentinel(kind) in queue) is allowed, (kind, member, queue)
            sent = await world.details(member, intake)
            shown = any(_sentinel(kind) in page for page in sent)
            assert shown is allowed, (kind, member, sent)
            assert shown or sent == [LEAD_DETAILS_REFUSED_MESSAGE]


async def test_list_and_resume_omit_or_refuse_what_the_member_cannot_read(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(sessions)
    for member in MEMBERS:
        _, listed = await world.mcp(member, "lead_intake_list")
        _, batch = await world.mcp(member, "lead_intake_resume")
        for kind in KINDS:
            allowed = member in READERS[kind]
            assert (_sentinel(kind) in listed) is allowed, (member, kind, listed)
            # Automatic batches skip captures fenced by old destination/revision.
            assert _sentinel(kind) not in batch, (member, kind)
            refused, single = await world.mcp(
                member, "lead_intake_resume", intake_id=str(world.records[kind])
            )
            assert (_sentinel(kind) in single) is (allowed and member == BOTH), (member, kind)
            assert refused is not (allowed and member == BOTH), (member, kind, single)


async def test_the_queue_hides_thread_less_records_from_view_without_history(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(sessions, reconfigure=False)
    hidden = await world.leads(NO_HISTORY, None)
    assert all(str(intake) not in hidden for intake in world.records.values()), hidden
    assert "SENTINEL" not in hidden
    shown = await world.leads(OUTSIDE_PRIVATE, None)
    assert str(world.records["capture"]) in shown and str(world.records["private"]) not in shown
    for kind in KINDS:
        reply = await world.leads(NO_HISTORY, world.records[kind])
        assert "SENTINEL" not in reply and str(world.records[kind]) not in reply


async def test_an_agent_held_to_its_own_channel_reads_no_lead_from_a_dm(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.adapters.mcp.tools._channel_policy as channel_policy

    agent = MagicMock(metadata={})
    agent.name = "sales"
    monkeypatch.setattr(
        channel_policy, "find_agent_by_derived_uuid_cached", AsyncMock(return_value=agent)
    )
    # The agent is HOME's own; the member can read sales channel A natively and
    # runs it in a DM, outside every channel. Nothing read may be carried home.
    held = TenantAccessPolicy(
        invoker_role_ids=(ROLE,),
        channel_rules={HOME: ChannelRule(readers="own", writers="own")},
        agent_rules={"sales": AgentRule(runs_in=(HOME,))},
    )
    world = await _world(sessions, reconfigure=False, policy=held, dm_origin=True)
    intake = str(world.records["capture"])
    for tool, arguments in (
        ("lead_intake_get", {"intake_id": intake}),
        ("lead_intake_list", {}),
        ("lead_intake_read_source", {"message_id": "source-new"}),
        ("lead_intake_health", {}),
    ):
        refused, text = await world.mcp(A_ONLY, tool, **arguments)
        assert "SENTINEL" not in text, (tool, text)
        if tool != "lead_intake_list":
            assert refused, (tool, text)
            assert "own_agents_only" in text, (tool, text)
    # The same member, without the hold, reads the lead.
    await set_policy(world, TenantAccessPolicy(invoker_role_ids=(ROLE,)))
    refused, text = await world.mcp(A_ONLY, "lead_intake_get", intake_id=intake)
    assert not refused and _sentinel("capture") in text, text


async def set_policy(world: World, policy: TenantAccessPolicy) -> None:
    async with world.sessions() as session:
        await set_access_policy(session, tenant_id=world.tenant_id, policy=policy)
        await session.commit()


@pytest.mark.parametrize("remove", ["erase", "expire"])
async def test_pending_card_loses_evidence_when_the_live_record_is_removed(
    sessions: async_sessionmaker[AsyncSession], remove: str
) -> None:
    world = await _world(sessions, reconfigure=False)
    intake = world.records["private"]
    assert await world.details(OUTSIDE_PRIVATE, intake) == [LEAD_DETAILS_REFUSED_MESSAGE]
    for member in (OUTSIDE_PRIVATE, A_ONLY):
        assert await world.details(member, intake, remove=remove) == [
            "This lead is no longer available."
        ]


@pytest.mark.parametrize("empty", [True, False])
async def test_resume_mailbox_checks_history_even_without_readable_records(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    from daimon.adapters.mcp.services.lead_intake import HttpIntakePorts

    scan = AsyncMock(return_value=(["synthetic-discovery"], None))
    monkeypatch.setattr(HttpIntakePorts, "scan", scan)
    world = await _world(sessions, reconfigure=False)
    if empty:
        from daimon.core.stores.lead_intake import erase_actor

        async with sessions() as session, session.begin():
            await erase_actor(session, world.tenant_id, A_ONLY)
    refused, text = await world.mcp(NO_HISTORY, "lead_intake_resume")
    assert refused and "synthetic-discovery" not in text
    scan.assert_not_awaited()
    refused, text = await world.mcp(BOTH, "lead_intake_resume")
    assert not refused and "synthetic-discovery" in text
    revoked = {
        **CHANNELS[A],
        "permission_overwrites": [*CHANNELS[A]["permission_overwrites"], _deny(BOTH, HISTORY)],
    }
    monkeypatch.setitem(CHANNELS, A, revoked)
    refused, text = await world.mcp(BOTH, "lead_intake_resume")
    assert refused and "synthetic-discovery" not in text


@pytest.mark.parametrize("hidden", ["private", "destination"])
async def test_list_pages_past_a_hundred_hidden_records(
    sessions: async_sessionmaker[AsyncSession], hidden: str
) -> None:
    world = await _world(sessions, reconfigure=False)
    config, _ = await world.store.configuration()
    template = (await world.store.get(world.records["capture"])).submission
    assert template is not None
    for index in range(101):
        draft = template.model_copy(
            update={"source": template.source.model_copy(update={"message_id": f"hidden-{index}"})}
        )
        record = await world.store.accept(config, draft, A_ONLY)
        record.progress.discord_thread_id = PRIVATE
        await world.store.save(record)
    member = OUTSIDE_PRIVATE
    if hidden == "destination":
        member = B_ONLY
        config = config.model_copy(update={"revision": 2, "channel_id": B})
        await world.store.configure(config, {"hubspot_access_token": "synthetic"})
    expected: list[str] = []
    for index in range(3):
        draft = template.model_copy(
            update={"source": template.source.model_copy(update={"message_id": f"visible-{index}"})}
        )
        record = await world.store.accept(config, draft, A_ONLY)
        expected.append(str(record.id))
    refused, first = await world.mcp(member, "lead_intake_list")
    assert not refused
    assert all(identity in first for identity in expected)
    refused, second = await world.mcp(member, "lead_intake_list", after_id=expected[0])
    assert not refused and expected[0] not in second
    assert all(identity in second for identity in expected[1:])


@pytest.mark.parametrize("fence", ["destination", "revision", "private_thread"])
async def test_resume_batch_skips_old_or_unreadable_captures_before_its_window(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, fence: str
) -> None:
    from daimon.core.lead_intake_service import IntakeService

    world = await _world(sessions, reconfigure=False)
    config, credentials = await world.store.configuration()
    template = (await world.store.get(world.records["capture"])).submission
    assert template is not None
    # More than a raw store page: skipping must precede both page and batch limits.
    for index in range(101):
        draft = template.model_copy(
            update={
                "source": template.source.model_copy(update={"message_id": f"deferred-{index}"})
            }
        )
        record = await world.store.accept(config, draft, A_ONLY)
        if fence == "private_thread":
            record.progress.discord_thread_id = PRIVATE
            await world.store.save(record)
    member = OUTSIDE_PRIVATE
    if fence != "private_thread":
        config = config.model_copy(
            update={"revision": 2, "channel_id": B if fence == "destination" else A}
        )
        await world.store.configure(config, credentials)
        member = B_ONLY if fence == "destination" else A_ONLY
    draft = template.model_copy(
        update={
            "source": template.source.model_copy(update={"message_id": "eligible-new"}),
            "evidence": template.evidence.model_copy(
                update={"original_text": "NEW ELIGIBLE EVIDENCE"}
            ),
            "thread_title": "NEW ELIGIBLE LEAD",
            "thread_content": "NEW ELIGIBLE CONTENT",
        }
    )
    eligible = await world.store.accept(config, draft, member)
    # Mode cutover keeps the capture revision; no live provider effects are needed.
    await world.store.configure(config.model_copy(update={"mode": "active"}), credentials)
    attempted: list[uuid.UUID] = []

    async def advance(service: IntakeService, intake_id: uuid.UUID) -> IntakeRecord:
        attempted.append(intake_id)
        return await service.store.get(intake_id)

    monkeypatch.setattr(IntakeService, "advance", advance)
    for _ in range(2):
        attempted.clear()
        refused, text = await world.mcp(member, "lead_intake_resume")
        assert not refused and "NEW ELIGIBLE LEAD" in text, text
        assert eligible.id in attempted and len(attempted) <= 25
        if fence == "private_thread":
            assert all(identity != world.records["private"] for identity in attempted)
        else:
            assert attempted == [eligible.id]
            assert "SENTINEL" not in text
            for index in range(101):
                assert f"deferred-{index}" not in text

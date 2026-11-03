"""A held credential's hold applied by every allowlisted MCP tool, before any no-rules shortcut.

The hold travels on the credential (`AuthIdentity.hold`), so these tests build identities
directly and drive the channel policy, the read policy and the tools that filter by it,
with real Postgres for the stores and no platform HTTP. Two tenant policies run each
case: the open one (no rules, the case a shortcut would wave through) and a ruled one.
Ids are synthetic.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools._channel_policy as channel_policy_mod
import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_policy import (
    ChannelReadRefused,
    load_read_policy,
    require_agent_creatable,
    require_channel_writable,
    require_dm_recipient_allowed,
    require_identity_changeable,
    require_publishable,
)
from daimon.adapters.mcp.tools._session_access import sessions_outside_seals
from daimon.adapters.mcp.tools.routines import (
    _create_routine_impl,  # pyright: ignore[reportPrivateUsage]
    _list_routines_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._visibility import check_channel_access
from daimon.adapters.mcp.tools.timers import (
    _cancel_timer_impl,  # pyright: ignore[reportPrivateUsage]
    _create_timer_impl,  # pyright: ignore[reportPrivateUsage]
    _list_timers_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import McpSettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.session_seal import origin_stamp
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import ChatPlatform, Role
from daimon.core.turn_hold import TurnHold
from daimon.core.turn_origin import turn_origin
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from mux.testing.drivers.anthropic.ma_models import ma_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SHARED = "C-SYN-SHARED"
ELSEWHERE = "C-SYN-ELSEWHERE"
LIMITED = "C-SYN-LIMITED"
DM = "D-SYN-DM"
TEAMS_SHARED = "19:syn-shared@thread.tacv2"
TEAMS_OTHER = "19:syn-other@thread.tacv2"
OUTSIDE = "outside"

OPEN = TenantAccessPolicy()
RULED = TenantAccessPolicy(
    channel_rules={
        SHARED: ChannelRule(readers="inside"),
        LIMITED: ChannelRule(readers="inside"),
    }
)
POLICIES = pytest.mark.parametrize("policy", [OPEN, RULED], ids=["open", "ruled"])


@dataclasses.dataclass
class _World:
    runtime: McpRuntime
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    sessionmaker: async_sessionmaker[AsyncSession]
    platform: ChatPlatform

    def auth(
        self,
        hold: TurnHold | None = None,
        *,
        admin: bool = False,
        foreign: bool = False,
        account_id: uuid.UUID | None = None,
    ) -> AuthIdentity:
        return AuthIdentity(
            account_id=account_id or self.account_id,
            tenant_id=self.tenant_id,
            role=Role.ADMIN if admin else Role.USER,
            platform=self.platform,
            platform_user_id="U-SYN",
            chat_agent_id=derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id="agent_x"),
            is_admin=admin and hold is None and not foreign,
            hold=hold,
            foreign_home_id="T-SYN-OTHER" if foreign else None,
            chat_session_id="fixture-session-grant",
        )


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.require_routine_destination = False
    return McpRuntime(
        session_factory=sessionmaker,
        client=build_stub_anthropic(lambda _r: httpx.Response(404)),
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name="daimon"),
        fernet=make_fernet(),
    )


async def _world(
    sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
    *,
    platform: ChatPlatform = "slack",
) -> _World:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id=f"W-{uuid.uuid4().hex}")
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform=platform, external_id="U-SYN", tenant=tenant, account=account
        )
        await set_access_policy(session, tenant_id=tenant.id, policy=policy)
    return _World(_runtime(sessionmaker), tenant.id, account.id, sessionmaker, platform)


def hold(channel_id: str = SHARED, *, platform: str = "slack", client: bool = True) -> TurnHold:
    return TurnHold(platform=platform, channel_id=channel_id, client=client)


async def _posts(world: _World, auth: AuthIdentity, channel_id: str, parent: str | None = None):
    await require_channel_writable(
        world.runtime, auth, channel_id=channel_id, parent_channel_id=parent
    )


# --- posting ------------------------------------------------------------------


@POLICIES
@pytest.mark.parametrize("client", [True, False], ids=["client", "shared-channel-member"])
async def test_a_held_identity_posts_inside_its_channel_and_the_threads_under_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
    client: bool,
) -> None:
    world = await _world(committing_sessionmaker, policy)
    auth = world.auth(hold(client=client))

    await _posts(world, auth, SHARED)
    await _posts(world, auth, f"{SHARED}:1700000000.000100")
    await _posts(world, auth, "T-THREAD", SHARED)


@POLICIES
async def test_a_held_teams_identity_posts_in_the_thread_forms_under_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    world = await _world(committing_sessionmaker, policy, platform="teams")
    auth = world.auth(hold(TEAMS_SHARED, platform="teams"))

    await _posts(world, auth, TEAMS_SHARED, TEAMS_SHARED)
    await _posts(world, auth, f"{TEAMS_SHARED};messageid=1700000000000", TEAMS_SHARED)
    with pytest.raises(ToolError, match="held to the channel it runs in"):
        await _posts(world, auth, f"{TEAMS_OTHER};messageid=1700000000000", TEAMS_OTHER)
    with pytest.raises(ToolError, match="held to the channel it runs in"):
        await _posts(world, auth, "a:syn-personal-chat")


@POLICIES
@pytest.mark.parametrize("admin", [False, True], ids=["member", "admin"])
async def test_a_held_identity_is_refused_another_channel_and_its_own_dm(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
    admin: bool,
) -> None:
    world = await _world(committing_sessionmaker, policy)
    auth = world.auth(hold(), admin=admin)

    for target, parent in ((ELSEWHERE, None), (DM, None), (f"{ELSEWHERE}:1700.1", None)):
        with pytest.raises(ToolError, match="held to the channel it runs in"):
            await _posts(world, auth, target, parent)
    # A thread whose parent is elsewhere is outside too, whatever its own id looks like.
    with pytest.raises(ToolError, match="held to the channel it runs in"):
        await _posts(world, auth, f"{SHARED}-lookalike", ELSEWHERE)


@POLICIES
async def test_an_unheld_identity_posts_wherever_it_could_before(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    world = await _world(committing_sessionmaker, policy)
    auth = world.auth(None, admin=True)

    for target in (SHARED, ELSEWHERE, DM):
        await _posts(world, auth, target)


async def test_a_held_identity_still_obeys_the_channels_own_writers_rule(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(
        committing_sessionmaker,
        TenantAccessPolicy(channel_rules={SHARED: ChannelRule(writers="none")}),
    )

    with pytest.raises(ToolError, match="writers to none"):
        await _posts(world, world.auth(hold()), SHARED)


@POLICIES
async def test_a_client_with_an_unheld_credential_is_refused_channel_tools(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    """Its principal is marked foreign, but this credential carries no hold."""
    world = await _world(committing_sessionmaker, policy)
    auth = world.auth(None, admin=True, foreign=True)

    assert auth.client and auth.hold is None
    assert mcp_subject(auth).is_admin is False
    with pytest.raises(ToolError, match="held to the channel it runs in"):
        await _posts(world, auth, SHARED)
    read = await load_read_policy(world.runtime, auth, origin_context_id=None)
    with pytest.raises(ChannelReadRefused, match="held to the channel it runs in"):
        read.require(SHARED)
    with pytest.raises(ToolError, match="held to the channel it runs in"):
        await require_dm_recipient_allowed(world.runtime, auth, recipient_id="U-SYN-2")


def test_mcp_subject_carries_the_client_from_the_credential() -> None:
    base = dict(account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN)
    held = AuthIdentity(**base, hold=hold(client=True))  # type: ignore[arg-type]
    member = AuthIdentity(**base, hold=hold(client=False))  # type: ignore[arg-type]
    foreign = AuthIdentity(**base, foreign_home_id="T-SYN")  # type: ignore[arg-type]
    plain = AuthIdentity(**base)  # type: ignore[arg-type]

    assert [mcp_subject(a, is_admin=True).client for a in (held, member, foreign, plain)] == [
        True,
        False,
        True,
        False,
    ]
    assert mcp_subject(held, is_admin=True).is_admin is False
    assert mcp_subject(plain, is_admin=True).is_admin is True


# --- reading ------------------------------------------------------------------


@POLICIES
async def test_a_held_read_policy_reads_lists_and_searches_only_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    world = await _world(committing_sessionmaker, policy)
    read = await load_read_policy(world.runtime, world.auth(hold()), origin_context_id=None)

    assert read.allows(SHARED)
    assert read.allows(f"{SHARED}:1700.1", SHARED), "a Slack thread under the channel"
    assert read.allows("T-THREAD", SHARED), "a Discord thread under the channel"
    for outside in (
        (ELSEWHERE, None),
        (DM, None),
        (LIMITED, None),
        (f"{ELSEWHERE}:1.2", ELSEWHERE),
    ):
        assert not read.allows(*outside), outside
        with pytest.raises(ChannelReadRefused, match="held to the channel it runs in"):
            read.require(*outside)
    assert read.lists(SHARED) and not read.lists(ELSEWHERE) and not read.lists(DM)
    assert read.withholds_hits, "a search total and page size must not count other channels' hits"


@POLICIES
async def test_a_held_admin_reads_no_more_than_a_held_member(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    world = await _world(committing_sessionmaker, policy)
    read = await load_read_policy(
        world.runtime, world.auth(hold(), admin=True), origin_context_id=None
    )

    assert read.allows(SHARED) and not read.allows(ELSEWHERE)


async def test_an_unheld_read_policy_is_unchanged(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, RULED)
    read = await load_read_policy(world.runtime, world.auth(), origin_context_id=None)

    assert read.hold is None and not read.subject.client
    assert read.allows(ELSEWHERE) and read.lists(ELSEWHERE)
    assert not read.allows(SHARED), "readers limited to inside still need an origin"


async def test_a_named_origin_never_widens_a_held_read(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The credential is the authority: an origin in a limited channel opens nothing."""
    world = await _world(committing_sessionmaker, RULED)
    auth = world.auth(hold())
    async with turn_origin(
        committing_sessionmaker,
        tenant_id=world.tenant_id,
        account_id=world.account_id,
        platform="slack",
        parent_channel_id=LIMITED,
        thread_id="1700.1",
        responder_ma_agent_id="agent_x",
        responder_name="daimon",
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        chat_session_id="fixture-session-grant",
    ) as origin:
        read = await load_read_policy(world.runtime, auth, origin_context_id=str(origin.id))

    assert not read.allows(LIMITED)
    assert read.allows(SHARED), "and the limited held channel needs no origin at all"


# --- no-rules shortcuts that must not drop the hold -----------------------------


@POLICIES
async def test_a_held_identity_cannot_message_anyone_create_agents_or_change_the_identity(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    world = await _world(committing_sessionmaker, policy)
    auth = world.auth(hold(), admin=True)

    with pytest.raises(ToolError, match="held to the channel it runs in"):
        await require_dm_recipient_allowed(world.runtime, auth, recipient_id="U-SYN-2")
    with pytest.raises(ToolError, match="held to the channel it runs in"):
        await require_agent_creatable(world.runtime, auth)
    with pytest.raises(ToolError, match="Nothing was changed"):
        await require_identity_changeable(world.runtime, auth, origin_context_id=None)
    # The same calls pass for an unheld identity with no rules.
    if policy is OPEN:
        plain = world.auth()
        await require_dm_recipient_allowed(world.runtime, plain, recipient_id="U-SYN-2")
        await require_agent_creatable(world.runtime, plain)


@POLICIES
async def test_publishing_from_a_held_identity_always_needs_the_requesters_card(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _world(committing_sessionmaker, policy)
    cards: list[bool] = []

    async def gap(_rt: object, _auth: object, origin: object, *, tool_name: str) -> str | None:
        cards.append(origin is not None)
        return None if origin is not None else "no_origin"

    monkeypatch.setattr(channel_policy_mod, "session_card_gap", gap)
    auth = world.auth(hold())

    with pytest.raises(ToolError, match="presses Approve"):
        await require_publishable(
            world.runtime, auth, tool_name="publish_report", origin_context_id=None
        )
    async with _origin(world, SHARED) as inside, _origin(world, ELSEWHERE) as outside:
        await require_publishable(
            world.runtime, auth, tool_name="publish_report", origin_context_id=str(inside.id)
        )
        # An origin outside the hold counts as none, so the card is not found.
        with pytest.raises(ToolError, match="presses Approve"):
            await require_publishable(
                world.runtime, auth, tool_name="publish_report", origin_context_id=str(outside.id)
            )
    if policy is OPEN:
        # Unheld and ruleless: no card, as before.
        await require_publishable(
            world.runtime, world.auth(), tool_name="publish_report", origin_context_id=None
        )


def _origin(world: _World, channel_id: str, *, account_id: uuid.UUID | None = None) -> Any:
    return turn_origin(
        world.sessionmaker,
        tenant_id=world.tenant_id,
        account_id=account_id or world.account_id,
        platform=world.platform,
        parent_channel_id=channel_id,
        thread_id="1700.1",
        responder_ma_agent_id="agent_x",
        responder_name="daimon",
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        chat_session_id="fixture-session-grant",
    )


# --- routines -----------------------------------------------------------------


@POLICIES
async def test_a_held_identity_and_a_client_use_no_routines(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    world = await _world(committing_sessionmaker, policy)

    for auth in (world.auth(hold(), admin=True), world.auth(None, foreign=True)):
        with pytest.raises(ToolError, match="can't use routines"):
            await _list_routines_impl(world.runtime, auth)
        with pytest.raises(ToolError, match="can't use routines"):
            await _create_routine_impl(
                world.runtime,
                auth,
                agent_name="daimon",
                cron_expr="0 9 * * *",
                timezone="UTC",
                trigger_message="syn",
                destination_kind="channel",
                destination_id=SHARED,
            )
    assert await _list_routines_impl(world.runtime, world.auth(admin=True)) == []


# --- timers -------------------------------------------------------------------


async def test_a_held_identity_creates_lists_and_cancels_only_timers_in_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, OPEN)
    held, unheld = world.auth(hold()), world.auth()
    fire_at = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0).isoformat()

    async with _origin(world, SHARED) as inside, _origin(world, ELSEWHERE) as outside:
        mine = await _create_timer_impl(
            world.runtime, held, origin_context_id=str(inside.id), fire_at=fire_at, note="syn-a"
        )
        with pytest.raises(ToolError, match="held to the channel it runs in"):
            await _create_timer_impl(
                world.runtime,
                held,
                origin_context_id=str(outside.id),
                fire_at=fire_at,
                note="syn-b",
            )
        elsewhere = await _create_timer_impl(
            world.runtime, unheld, origin_context_id=str(outside.id), fire_at=fire_at, note="syn-c"
        )

    assert {t.timer_id for t in await _list_timers_impl(world.runtime, held)} == {mine.timer_id}
    assert {t.timer_id for t in await _list_timers_impl(world.runtime, unheld)} == {
        mine.timer_id,
        elsewhere.timer_id,
    }
    with pytest.raises(ToolError, match="timer not found"):
        await _cancel_timer_impl(world.runtime, held, timer_id=elsewhere.timer_id)
    assert (await _cancel_timer_impl(world.runtime, held, timer_id=mine.timer_id)).cancelled
    assert (await _cancel_timer_impl(world.runtime, unheld, timer_id=elsewhere.timer_id)).cancelled


async def test_a_held_identity_requires_its_explicit_origin_inside_the_hold(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A held caller supplies its signed, explicit origin even with only one live turn."""
    world = await _world(committing_sessionmaker, OPEN)
    auth = world.auth(hold())
    fire_at = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0).isoformat()

    async with _origin(world, ELSEWHERE):
        with pytest.raises(ToolError, match="origin_context_id"):
            await _create_timer_impl(
                world.runtime, auth, origin_context_id="", fire_at=fire_at, note="syn"
            )
    async with _origin(world, SHARED) as origin:
        with pytest.raises(ToolError, match="origin_context_id"):
            await _create_timer_impl(
                world.runtime, auth, origin_context_id="", fire_at=fire_at, note="syn"
            )
        timer = await _create_timer_impl(
            world.runtime, auth, origin_context_id=str(origin.id), fire_at=fire_at, note="syn"
        )
    assert timer.thread_id == "1700.1"


# --- sessions -----------------------------------------------------------------


async def test_a_held_identity_lists_only_the_sessions_that_ran_in_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, OPEN)
    sessions = {
        "in-channel": ma_session(
            id="sesn_in", metadata=origin_stamp(channel_id=SHARED, thread_id="1700.1")
        ),
        "in-other": ma_session(
            id="sesn_other", metadata=origin_stamp(channel_id=ELSEWHERE, thread_id="1700.2")
        ),
        "headless": ma_session(id="sesn_headless", metadata={}),
    }

    held = await sessions_outside_seals(world.runtime, world.auth(hold()), list(sessions.values()))
    unheld = await sessions_outside_seals(world.runtime, world.auth(), list(sessions.values()))

    assert [s.id for s in held] == ["sesn_in"]
    assert {s.id for s in unheld} == {"sesn_in", "sesn_other", "sesn_headless"}


# --- Slack: a client is checked like a guest ----------------------------------


class _FakeSlack:
    """A workspace where U-SYN is a full member of a public channel it has not joined."""

    async def users_info(self, *, user: str) -> dict[str, Any]:
        return {"user": {"id": user, "is_restricted": False, "is_ultra_restricted": False}}

    async def conversations_members(self, **_kwargs: Any) -> dict[str, Any]:
        return {"members": []}


async def test_a_client_sees_a_public_channel_only_as_a_member() -> None:
    public = {"id": SHARED, "is_private": False}

    await check_channel_access(_FakeSlack(), channel=public, user_id="U-SYN")  # type: ignore[arg-type]
    with pytest.raises(ToolError):
        await check_channel_access(
            _FakeSlack(),  # type: ignore[arg-type]
            channel=public,
            user_id="U-SYN",
            is_client=True,
        )


# --- two credentials of one account ---------------------------------------------


@POLICIES
async def test_a_held_and_an_unheld_credential_of_one_account_do_not_narrow_or_widen_each_other(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
) -> None:
    """Both turns run at once (two origins), yet each decision rests on its own credential."""
    world = await _world(committing_sessionmaker, policy)
    held, unheld = world.auth(hold()), world.auth()

    async with _origin(world, SHARED), _origin(world, ELSEWHERE):
        # The unheld credential is not narrowed by the held turn running in SHARED.
        await _posts(world, unheld, ELSEWHERE)
        await _posts(world, unheld, DM)
        unheld_read = await load_read_policy(world.runtime, unheld, origin_context_id=None)
        assert unheld_read.allows(ELSEWHERE) and unheld_read.lists(ELSEWHERE)
        # The held credential is not widened by the unheld turn running elsewhere.
        with pytest.raises(ToolError, match="held to the channel it runs in"):
            await _posts(world, held, ELSEWHERE)
        held_read = await load_read_policy(world.runtime, held, origin_context_id=None)
        assert not held_read.allows(ELSEWHERE) and held_read.allows(SHARED)


async def test_a_foreign_principal_is_not_a_held_one_for_its_other_credentials_alone() -> None:
    """The marker follows the principal; the hold follows the credential."""
    held_member = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, hold=hold(client=False)
    )
    assert held_member.client is False
    marked = dataclasses.replace(held_member, foreign_home_id="T-SYN")
    assert marked.client is True and marked.hold == held_member.hold

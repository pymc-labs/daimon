"""A channel kept to its own agents through the MCP tools: the rule tool and every filtered surface.

C (``ROOM``) has readers and writers ``own``, with its default agent ``local``
its own agent (an agent rule naming C alone). ``shared`` answers in another
channel. A call is inside C when it executes as ``local``.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools._channel_policy as channel_policy_mod
import daimon.adapters.mcp.tools.routines as routines_mod
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _channel_target as channel_target
from daimon.adapters.mcp.tools import thread_participation as participation_mod
from daimon.adapters.mcp.tools._channel_policy import (
    load_read_policy,
    require_channel_writable,
    require_dm_recipient_allowed,
    require_identity_changeable,
    require_publishable,
    require_reader_source_publishable,
    turn_origin_place,
)
from daimon.adapters.mcp.tools._session_gate import CardGap
from daimon.adapters.mcp.tools.agents import (
    AgentInfo,
    _create_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _get_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _list_agents_impl,  # pyright: ignore[reportPrivateUsage]
    _update_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.channel_budgets import (
    _get_channel_budget_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.channel_environments import (
    _clear_channel_environment_impl,  # pyright: ignore[reportPrivateUsage]
    _set_channel_environment_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.channel_rules import (
    _set_channel_rule_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl
from daimon.adapters.mcp.tools.environments import (
    _get_environment_impl,  # pyright: ignore[reportPrivateUsage]
    _list_environments_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.propagation import (
    _clear_agent_default_impl,  # pyright: ignore[reportPrivateUsage]
    _explain_agent_resolution_impl,  # pyright: ignore[reportPrivateUsage]
    _set_agent_default_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.routines import (
    _create_routine_impl,  # pyright: ignore[reportPrivateUsage]
    _list_routines_impl,  # pyright: ignore[reportPrivateUsage]
    _update_routine_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.skills import (
    _get_impl,  # pyright: ignore[reportPrivateUsage]
    _list_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.tenant_summary import (
    _get_tenant_summary_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.thread_participation import (
    _get_thread_participation_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.timers import (
    _list_timers_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.channel_environments import save_scope_environment
from daimon.core.continuity.timers import schedule_timer
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.core.stores import routines as routines_store
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import create_origin, delete_origin, get_active_origin
from daimon.core.stores.user_skills import upsert_user_skill
from daimon.testing import ma_agent, ma_environment, ma_session, ma_session_agent
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

ROOM = "111111111111111111"
OTHER = "222222222222222222"
NEW_ROOM = "333333333333333333"
SETUP_THREAD = "555555555555555555"
OWN = ChannelRule(readers="own", writers="own")


class _World:
    def __init__(self, tenant_id: uuid.UUID, account_id: uuid.UUID, state: FakeMAState) -> None:
        self.tenant_id, self.account_id, self.state = tenant_id, account_id, state
        self.sessions: dict[str, dict[str, Any]] = {}

    def auth(self, *, admin: bool = True, executing: str | None = None) -> AuthIdentity:
        return AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.ADMIN if admin else Role.USER,
            platform="discord",
            platform_user_id="444444444444444444",
            is_admin=admin,
            chat_agent_id=None
            if executing is None
            else derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=executing),
        )


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], client: AsyncAnthropic) -> McpRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.github.oauth_scopes = ()
    return McpRuntime(
        session_factory=sessionmaker,
        client=client,
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name="daimon"),
        fernet=make_fernet(),
    )


async def _world(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    keep_own: bool = True,
    local: str = "local",
    uploaded: bool = False,
) -> tuple[_World, McpRuntime]:
    """``uploaded`` adds ``upload-notes``: a title naming no agent, owned by ``local`` by its upload."""
    titles = {"skill_local": f"{local}/notes", "skill_shared": "shared/notes"}
    if uploaded:
        titles["skill_upload"] = "upload-notes"
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        if uploaded:
            principal = await make_platform_principal(
                session, platform="discord", external_id="555", tenant=tenant, account=account
            )
            await upsert_user_skill(
                session,
                tenant_id=tenant.id,
                principal_id=principal.id,
                agent_name=local,
                name="upload-notes",
                source_repo_url="https://github.com/o/r",
                source_repo_branch="main",
                source_path="",
                content_hash="h",
                anthropic_id="skill_upload",
                anthropic_latest_version="1",
            )
        for channel, agent in ((ROOM, local), (OTHER, "shared")):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
                tenant_id=tenant.id,
                agent_name=agent,
                mode="agent",
            )
        if keep_own:
            await set_access_policy(
                session,
                tenant_id=tenant.id,
                policy=TenantAccessPolicy(
                    channel_rules={ROOM: OWN}, agent_rules={local: AgentRule(runs_in=(ROOM,))}
                ),
            )
    state = FakeMAState()
    for agent_id, name in (("agent_local", local), ("agent_shared", "shared")):
        agent = ma_agent(id=agent_id, name=name, tenant_id=tenant.id)
        state.agents[agent.id] = agent.model_dump(mode="json")
    skills = [
        SkillListResponse(
            id=skill_id,
            type="custom",
            display_title=tenant_scoped_display_title(tenant_id=tenant.id, name=body),
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        for skill_id, body in titles.items()
    ]

    def skills_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/skills":
            return list_response(skills)
        raise NotHandled

    world = _World(tenant.id, account.id, state)

    def sessions_handler(request: httpx.Request) -> httpx.Response:
        found = re.fullmatch(r"/v1/sessions/(?P<id>[^/]+)", request.url.path)
        if request.method != "GET" or found is None or found["id"] not in world.sessions:
            raise NotHandled
        return httpx.Response(200, json=world.sessions[found["id"]])

    client = build_fake_anthropic(
        combine_handlers(skills_handler, sessions_handler, make_fake_ma_handler(state))
    )
    return world, _runtime(sessionmaker, client)


async def test_nothing_changes_until_a_channel_is_kept_to_its_own_agents(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker, keep_own=False)
    names = {a.name for a in await _list_agents_impl(runtime, world.auth(), None)}
    assert names == {"local", "shared"}, "with no rule every agent is listed"
    inside = {
        a.name for a in await _list_agents_impl(runtime, world.auth(executing="agent_local"), None)
    }
    assert inside == {"local", "shared"}, "and every caller sees the same"


async def test_agents_and_skills_split_at_the_channels_line(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    outside, inside = world.auth(), world.auth(executing="agent_local")

    assert [a.name for a in await _list_agents_impl(runtime, outside, None)] == ["shared"]
    assert [a.name for a in await _list_agents_impl(runtime, inside, None)] == ["local"]
    with pytest.raises(ToolError, match="not found"):
        await _get_agent_impl(runtime, outside, "local")
    with pytest.raises(ToolError, match="not found"):
        await _get_agent_impl(runtime, inside, "shared")
    assert [s.name for s in await _list_impl(runtime, outside)] == ["shared/notes"]
    assert [s.name for s in await _list_impl(runtime, inside)] == ["local/notes"]


async def test_skill_owners_come_from_uploads_and_survive_shortened_titles(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    long_name = "l" * 64
    world, runtime = await _world(committing_sessionmaker, local=long_name, uploaded=True)
    outside, inside = world.auth(), world.auth(executing="agent_local")

    assert [s.name for s in await _list_impl(runtime, outside)] == ["shared/notes"], (
        "a shortened title and an uploaded skill both stay inside C"
    )
    inside_names = {s.name for s in await _list_impl(runtime, inside)}
    assert "upload-notes" in inside_names and "shared/notes" not in inside_names
    assert len(inside_names) == 2, "the long agent's own skill is listed inside"
    shortened = next(name for name in inside_names if name != "upload-notes")
    assert "/" not in shortened, "the title lost its '/' to shortening"
    for name in (shortened, "upload-notes"):
        with pytest.raises(ToolError):
            await _get_impl(runtime, outside, name)


async def test_routing_writes_keep_local_agents_in_and_shared_ones_out(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    admin = world.auth()
    with pytest.raises(ToolError, match="own agents"):
        await _set_agent_default_impl(runtime, admin, "shared", ROOM, "agent_shared")
    with pytest.raises(ToolError, match="own agents"):
        await _clear_agent_default_impl(runtime, admin, ROOM)
    with pytest.raises(ToolError, match="missing"):
        await _set_agent_default_impl(runtime, admin, "local", OTHER, "agent_local")
    async with committing_sessionmaker() as session:
        scope = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id=ROOM)
        )
    assert scope is not None and scope.agent_name == "local", "a refusal writes nothing"


async def test_routines_of_a_channel_kept_to_its_own_agents_show_only_inside_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        for agent_id, name, channel in (
            ("agent_local", "local", None),
            ("agent_shared", "shared", ROOM),
            ("agent_shared", "shared", OTHER),
        ):
            await routines_store.create_routine(
                session,
                tenant_id=world.tenant_id,
                created_by_user_id="444444444444444444",
                agent_id=agent_id,
                agent_name=name,
                cron_expr="0 * * * *",
                timezone_="UTC",
                trigger_message="hi",
                enabled=True,
                next_fire_at=None,
                channel_id=channel,
            )

    outside = await _list_routines_impl(runtime, world.auth())
    assert [(r.agent_name, r.channel_id) for r in outside] == [("shared", OTHER)], (
        "outside, neither the local agent's routine nor one posting into the room shows"
    )
    inside = await _list_routines_impl(runtime, world.auth(executing="agent_local"))
    assert [(r.agent_name, r.channel_id) for r in inside] == [("local", None)]
    with pytest.raises(ToolError, match="no agent named"):
        await _create_routine_impl(
            runtime,
            world.auth(),
            agent_name="local",
            cron_expr="0 * * * *",
            timezone="UTC",
            trigger_message="hi",
        )


async def test_routines_of_a_channel_kept_to_its_own_agents_deliver_inside_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no destination a routine reports by DM, outside every channel, so C's
    own agent needs a destination in C, on create and on every update."""
    world, runtime = await _world(committing_sessionmaker)

    async def destination(
        runtime: McpRuntime, auth: AuthIdentity, **kwargs: str | None
    ) -> str | None:
        return kwargs["destination_id"]

    monkeypatch.setattr(routines_mod, "_check_destination", destination)
    inside = world.auth(admin=False, executing="agent_local")
    every_hour = {"cron_expr": "0 * * * *", "timezone": "UTC", "trigger_message": "hi"}
    for kind, target in ((None, None), ("channel", OTHER)):
        with pytest.raises(ToolError, match="rule runs it only in certain channels"):
            await _create_routine_impl(
                runtime,
                inside,
                agent_name="local",
                destination_kind=kind,
                destination_id=target,
                **every_hour,
            )
    routine = await _create_routine_impl(
        runtime,
        inside,
        agent_name="local",
        destination_kind="channel",
        destination_id=ROOM,
        **every_hour,
    )
    updated = await _update_routine_impl(
        runtime, inside, routine_id=routine.id, trigger_message="hey"
    )
    assert updated.trigger_message == "hey", "a routine posting into C still updates"
    with pytest.raises(ToolError, match="rule runs it only in certain channels"):
        await _update_routine_impl(runtime, inside, routine_id=routine.id, clear_destination=True)
    with pytest.raises(ToolError, match="rule runs it only in certain channels"):
        await _update_routine_impl(
            runtime, inside, routine_id=routine.id, destination_kind="channel", destination_id=OTHER
        )


async def test_set_channel_rule_refuses_or_copies_and_releases(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker, keep_own=False)
    with pytest.raises(ToolError, match="Only a server or workspace admin"):
        await _set_channel_rule_impl(
            runtime, world.auth(admin=False), channel_id=ROOM, readers="own", writers="own"
        )
    with pytest.raises(ToolError, match="no default agent that could be its own"):
        await _set_channel_rule_impl(
            runtime, world.auth(), channel_id=NEW_ROOM, readers="own", writers="own"
        )

    kept = await _set_channel_rule_impl(
        runtime, world.auth(), channel_id=ROOM, readers="own", writers="own"
    )
    assert (kept.own_agent, kept.copied_from, kept.changed) == ("local", None, True)

    copied = await _set_channel_rule_impl(
        runtime, world.auth(), channel_id=NEW_ROOM, readers="own", writers="own", copy_from="shared"
    )
    assert copied.copied_from == "shared" and copied.own_agent == "channel-333333", (
        "without a readable channel name the copy is named from the id"
    )
    names = {str(agent["name"]) for agent in world.state.agents.values()}
    assert "channel-333333" in names, "the copy exists"
    async with committing_sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=world.tenant_id)
    assert policy.channel_rules == {ROOM: OWN, NEW_ROOM: OWN}
    assert policy.agent_rules == {
        "local": AgentRule(runs_in=(ROOM,)),
        "channel-333333": AgentRule(runs_in=(NEW_ROOM,)),
    }, "each channel's own agent runs only there"

    opened = await _set_channel_rule_impl(
        runtime, world.auth(), channel_id=ROOM, readers="any", release_agents=True
    )
    assert (opened.changed, opened.readers, opened.writers) == (True, "any", "any")
    assert opened.released_agents == ["local"], "releasing drops its agents' rules"


async def test_explain_agent_resolution_stays_on_the_callers_side(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    runtime = replace(
        runtime,
        deployment_default=DeploymentDefault(agent_name="daimon", environment_name="sandbox"),
    )
    outside, inside = world.auth(), world.auth(executing="agent_local")

    with pytest.raises(ToolError, match="other side of a channel kept to its own agents"):
        await _explain_agent_resolution_impl(runtime, outside, ROOM)
    with pytest.raises(ToolError, match="other side of a channel kept to its own agents"):
        await _explain_agent_resolution_impl(runtime, inside, OTHER)
    here = await _explain_agent_resolution_impl(runtime, inside, ROOM)
    assert (here.effective_agent_name, here.deployment_default) == ("local", None), (
        "inside, the shared fallback is not named"
    )
    assert here.deployment_environment is None, "inside, the workspace environments are hidden"
    there = await _explain_agent_resolution_impl(runtime, outside, OTHER)
    assert there.effective_agent_name == "shared"
    assert there.deployment_environment == "sandbox", "outside, they are shown as before"


async def test_posts_and_direct_messages_stay_on_their_side(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    outside, inside = world.auth(executing="agent_shared"), world.auth(executing="agent_local")

    with pytest.raises(ToolError, match="only they post in it"):
        await require_channel_writable(runtime, outside, channel_id=ROOM)
    with pytest.raises(ToolError, match="only they post in it"):
        await require_channel_writable(runtime, outside, channel_id="t1", parent_channel_id=ROOM)
    with pytest.raises(ToolError, match="rule runs it only in certain channels"):
        await require_channel_writable(runtime, inside, channel_id=OTHER)
    await require_channel_writable(runtime, inside, channel_id="t1", parent_channel_id=ROOM)
    await require_channel_writable(runtime, outside, channel_id=OTHER)
    await require_channel_writable(runtime, world.auth(), channel_id=ROOM)  # no agent: an operator
    with pytest.raises(ToolError, match="sends no direct messages"):
        await send_direct_message_impl(runtime, inside, recipient_id="123", content="hi")


async def _setup_thread_origin(
    sessionmaker: async_sessionmaker[AsyncSession],
    world: _World,
    *,
    channel: str = ROOM,
    responder: str = "shared",
) -> str:
    """A setup conversation in C (or `channel`), answered by the built-in (``shared`` here);
    with another `responder`, a plain conversation it answers there."""
    now = dt.datetime.now(dt.UTC)
    async with sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=world.tenant_id,
            account_id=world.account_id,
            platform="discord",
            parent_channel_id=channel,
            thread_id=SETUP_THREAD,
            responder_ma_agent_id=f"agent_{responder}",
            responder_name=responder,
            configuration_target_ma_agent_id="agent_local",
            configuration_target_name="local",
            role=Role.USER,
            expires_at=now + dt.timedelta(minutes=10),
            now=now,
            is_setup=responder == "shared",
        )
    return str(origin.id)


async def test_the_setup_thread_configures_its_channels_own_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """From C's setup thread, named by its verified origin, the built-in sees C's own
    agent and an admin configures it there; a member hits the pin guard as anywhere
    else. Without the origin, or on an agent key, it stays outside."""
    world, runtime = await _world(committing_sessionmaker)
    origin = await _setup_thread_origin(committing_sessionmaker, world)
    builtin = world.auth(admin=False, executing="agent_shared")

    listed = await _list_agents_impl(runtime, builtin, None, origin)
    assert [a.name for a in listed] == ["local"], "the setup thread sees C's own agents"
    found = await _get_agent_impl(runtime, builtin, "local", origin_context_id=origin)
    assert found.name == "local"
    world.state.agents["agent_local"]["metadata"]["daimon_account"] = str(world.account_id)

    async def update(auth: AuthIdentity) -> AgentInfo:
        return await _update_agent_impl(
            runtime,
            auth,
            "local",
            model=None,
            description="notes for the room",
            system=None,
            tools=None,
            mcp_servers=None,
            skills=None,
            expected_ma_agent_id="agent_local",
            origin_context_id=origin,
        )

    with pytest.raises(ToolError, match="No card was posted"):
        await update(builtin)
    updated = await update(world.auth(admin=True, executing="agent_shared"))
    assert updated.description == "notes for the room", "an admin configures C's agent there"

    with pytest.raises(ToolError, match="not found"):
        await _get_agent_impl(runtime, builtin, "local")
    agent_key = AuthIdentity(
        account_id=world.account_id,
        tenant_id=world.tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id="444444444444444444",
        agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_shared"),
    )
    with pytest.raises(ToolError, match="not found"):
        await _get_agent_impl(runtime, agent_key, "local", origin_context_id=origin)


async def test_the_setup_thread_is_held_to_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The built-in answering C's setup thread posts its cards into the thread and
    schedules C's agent into C, but sends nothing outside C."""
    world, runtime = await _world(committing_sessionmaker)
    origin_id = await _setup_thread_origin(committing_sessionmaker, world)
    builtin = world.auth(admin=False, executing="agent_shared")
    async with committing_sessionmaker() as session:
        row = await get_active_origin(
            session,
            origin_id=uuid.UUID(origin_id),
            tenant_id=world.tenant_id,
            account_id=world.account_id,
            platform="discord",
            now=dt.datetime.now(dt.UTC),
        )
    assert row is not None
    origin = turn_origin_place(row)

    await require_channel_writable(
        runtime, builtin, channel_id=SETUP_THREAD, parent_channel_id=ROOM, origin=origin
    )
    with pytest.raises(ToolError, match="only they post in it"):
        await require_channel_writable(
            runtime, builtin, channel_id=SETUP_THREAD, parent_channel_id=ROOM
        )
    with pytest.raises(ToolError, match="nothing said here is posted"):
        await require_channel_writable(runtime, builtin, channel_id=OTHER, origin=origin)

    async def destination(
        runtime: McpRuntime, auth: AuthIdentity, **kwargs: str | None
    ) -> str | None:
        return kwargs["destination_id"]

    monkeypatch.setattr(routines_mod, "_check_destination", destination)
    routine = await _create_routine_impl(
        runtime,
        builtin,
        agent_name="local",
        cron_expr="0 * * * *",
        timezone="UTC",
        trigger_message="hi",
        destination_kind="channel",
        destination_id=ROOM,
        origin_context_id=origin_id,
    )
    assert routine.agent_name == "local", "C's agent is scheduled into C from its setup thread"

    spec = AgentSpec(name="notes-bot", model="claude-sonnet-4-6", system="what C's room said")
    with pytest.raises(ToolError, match="creates no agents"):
        await _create_agent_impl(runtime, builtin, spec, origin_id)
    names = {str(agent["name"]) for agent in world.state.agents.values()}
    assert "notes-bot" not in names, "a new agent would carry the setup thread's text out of C"


@pytest.mark.parametrize(
    "origin_id",
    [None, "not-a-uuid", "6f9c2b1e-4d3a-4f8e-9b7c-1a2d3e4f5a6b"],
    ids=["none", "not-a-uuid", "unknown-uuid"],
)
async def test_a_chat_turn_naming_no_verified_origin_creates_no_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession], origin_id: str | None
) -> None:
    """Left out or made up, the origin could hide C's setup thread: refuse. A run with no
    origin, such as a routine, has nothing to retry with, so the refusal says not to.
    Once a turn of the agent runs in C, the call is held there whatever it names."""
    world, runtime = await _world(committing_sessionmaker)
    builtin = world.auth(admin=True, executing="agent_shared")
    spec = AgentSpec(name="notes-bot", model="claude-sonnet-4-6", system="what C's room said")

    with pytest.raises(ToolError, match="origin_context_id") as refused:
        await _create_agent_impl(runtime, builtin, spec, origin_id)
    assert str(refused.value).endswith(
        "Tell the caller to create the agent from a chat conversation. Do not retry."
    ), "nothing to retry with, so the model must not loop"
    await _setup_thread_origin(committing_sessionmaker, world)
    with pytest.raises(ToolError, match="held to a channel kept to its own agents"):
        await _create_agent_impl(runtime, builtin, spec, origin_id)

    names = {str(agent["name"]) for agent in world.state.agents.values()}
    assert "notes-bot" not in names, "nothing was created"


def _key(world: _World, *, bound: str | None) -> AuthIdentity:
    """An agent key of C's own agent, minted in C (``bound``) or anywhere else."""
    return AuthIdentity(
        account_id=world.account_id,
        tenant_id=world.tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id="444444444444444444",
        agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_local"),
        bound_channel_id=bound,
    )


def _own_agent_callers(world: _World) -> dict[str, AuthIdentity]:
    """Everyone C's own agent may run for: in C, in an admin's or C's channel admin's
    DM (the credential is the same wherever the turn runs), and its agent keys."""
    channel_admin = replace(
        world.auth(admin=False, executing="agent_local"), administered_channel_ids=frozenset({ROOM})
    )
    return {
        "member": world.auth(admin=False, executing="agent_local"),
        "admin": world.auth(admin=True, executing="agent_local"),
        "channel admin": channel_admin,
        "bound key": _key(world, bound=ROOM),
        "unbound key": _key(world, bound=None),
    }


async def test_an_own_agent_posts_and_messages_nowhere_outside_wherever_it_runs(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Send and DM paths: C's agent posts into C only, never another channel, a thread
    elsewhere or the requester's own DM, and sends no direct messages, for every caller."""
    world, runtime = await _world(committing_sessionmaker)
    for who, auth in _own_agent_callers(world).items():
        for channel, parent in ((OTHER, None), ("t9", OTHER), ("D123", None)):
            with pytest.raises(ToolError, match="rule runs it only in certain channels"):
                await require_channel_writable(
                    runtime, auth, channel_id=channel, parent_channel_id=parent
                )
        await require_channel_writable(runtime, auth, channel_id="t1", parent_channel_id=ROOM)
        with pytest.raises(ToolError, match="sends no direct messages"):
            await send_direct_message_impl(runtime, auth, recipient_id="123", content="hi")
        assert who, "every caller is held"


async def test_an_own_agent_writes_into_no_other_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Self-edit paths: from C the agent can't name another agent to edit, and what it
    writes into its own spec stays hidden outside C."""
    world, runtime = await _world(committing_sessionmaker)
    world.state.agents["agent_local"]["metadata"]["daimon_account"] = str(world.account_id)
    admin_inside = world.auth(admin=True, executing="agent_local")

    async def update(auth: AuthIdentity, name: str, agent_id: str) -> AgentInfo:
        return await _update_agent_impl(
            runtime,
            auth,
            name,
            model=None,
            description="the client's plans",
            system=None,
            tools=None,
            mcp_servers=None,
            skills=None,
            expected_ma_agent_id=agent_id,
        )

    with pytest.raises(ToolError, match="missing or changed"):
        await update(admin_inside, "shared", "agent_shared")
    own = await update(admin_inside, "local", "agent_local")
    assert own.description == "the client's plans", "C's agent edits itself"
    listed = await _list_agents_impl(runtime, world.auth(), None)
    assert [a.name for a in listed] == ["shared"], "outside, its edited spec never shows"
    with pytest.raises(ToolError, match="not found"):
        await _get_agent_impl(runtime, world.auth(), "local")


async def test_channel_admin_update_tool_changes_own_agent_but_refuses_another_teams_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    world.state.agents["agent_local"]["metadata"]["daimon_account"] = str(world.account_id)
    async with committing_sessionmaker.begin() as session:
        await set_channel_admins(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            channel_id=ROOM,
            role_ids=[],
            user_ids=["444444444444444444"],
            actor_account_id=None,
        )
    channel_admin = replace(
        world.auth(admin=False, executing="agent_local"),
        administered_channel_ids=frozenset({ROOM}),
    )

    async def update(name: str, agent_id: str) -> AgentInfo:
        return await _update_agent_impl(
            runtime,
            channel_admin,
            name,
            model=None,
            description=None,
            system="Team instructions",
            tools=None,
            mcp_servers=None,
            skills=None,
            expected_ma_agent_id=agent_id,
        )

    own = await update("local", "agent_local")
    assert own.applies is not None
    assert world.state.agents["agent_local"]["system"].endswith("Team instructions")
    with pytest.raises(ToolError, match="missing or changed"):
        await update("shared", "agent_shared")
    assert world.state.agents["agent_shared"]["system"] is None


async def test_an_own_agent_creates_no_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A new agent answers outside C, so a prompt C's agent wrote into it would carry
    C's content out: refused for every caller it runs for, before anything is made."""
    world, runtime = await _world(committing_sessionmaker)
    spec = AgentSpec(name="notes-bot", model="claude-sonnet-4-6", system="the client's plans")
    for auth in _own_agent_callers(world).values():
        with pytest.raises(ToolError, match="creates no agents"):
            await _create_agent_impl(runtime, auth, spec)
    names = {str(agent["name"]) for agent in world.state.agents.values()}
    assert names == {"local", "shared"}, "no agent was created"


_PUBLISH = "publish_report"


async def test_an_own_agent_publishes_only_once_approved_and_renames_daimon_nowhere(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link, or daimon's server nickname, shows outside C. Publishing waits for the
    requester's Approve, which no caller here has; the nickname is refused outright."""
    world, runtime = await _world(committing_sessionmaker)
    asked: list[str] = []

    async def no_card(*args: object, tool_name: str) -> CardGap:
        asked.append(tool_name)
        return "no_origin"

    monkeypatch.setattr(channel_policy_mod, "session_card_gap", no_card)
    for auth in _own_agent_callers(world).values():
        with pytest.raises(ToolError, match="presses Approve"):
            await require_publishable(runtime, auth, tool_name=_PUBLISH, origin_context_id=None)
        with pytest.raises(ToolError, match="server-wide name or avatar is refused"):
            await require_identity_changeable(runtime, auth, origin_context_id=None)
    assert set(asked) == {_PUBLISH}, "the card asked about is this tool's"


async def test_an_approved_call_publishes_from_inside(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the requester's Approve on the session, C's own agent and a call from C's
    setup thread publish; an agent key never asks."""
    world, runtime = await _world(committing_sessionmaker)

    async def approved(
        runtime: McpRuntime, auth: AuthIdentity, origin: object, *, tool_name: str
    ) -> CardGap | None:
        return None if origin is not None else "no_origin"  # as `session_card_gap`

    monkeypatch.setattr(channel_policy_mod, "session_card_gap", approved)
    local = world.auth(admin=False, executing="agent_local")
    own = await _setup_thread_origin(committing_sessionmaker, world, responder="local")
    await require_publishable(runtime, local, tool_name=_PUBLISH, origin_context_id=own)
    inside = await _setup_thread_origin(committing_sessionmaker, world)
    builtin = world.auth(executing="agent_shared")
    await require_publishable(runtime, builtin, tool_name=_PUBLISH, origin_context_id=inside)
    with pytest.raises(ToolError, match="presses Approve"):
        await require_publishable(
            runtime, _key(world, bound=ROOM), tool_name=_PUBLISH, origin_context_id=None
        )


async def test_publishing_is_held_by_the_turns_origin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A shared agent publishes freely from outside C; from C's setup thread it waits
    for Approve, and with no origin it is refused while C is kept to its own agents.
    Once a turn of it runs in C, a call naming the outside origin waits too. A call with
    no executing agent is held nowhere."""
    world, runtime = await _world(committing_sessionmaker)
    builtin = world.auth(executing="agent_shared")
    with pytest.raises(ToolError, match="origin_context_id"):
        await require_publishable(runtime, builtin, tool_name=_PUBLISH, origin_context_id=None)
    outside = await _setup_thread_origin(committing_sessionmaker, world, channel=OTHER)
    await require_publishable(runtime, builtin, tool_name=_PUBLISH, origin_context_id=outside)
    inside = await _setup_thread_origin(committing_sessionmaker, world)
    for named in (inside, outside):
        with pytest.raises(ToolError, match="presses Approve"):
            await require_publishable(runtime, builtin, tool_name=_PUBLISH, origin_context_id=named)
    await require_publishable(runtime, world.auth(), tool_name=_PUBLISH, origin_context_id=None)


async def test_an_agent_with_a_rule_publishes_only_once_approved_even_for_an_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker, keep_own=False)
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(agent_rules={"local": AgentRule(runs_in=(ROOM,))}),
        )
    with pytest.raises(ToolError, match="presses Approve"):
        await require_publishable(
            runtime, world.auth(executing="agent_local"), tool_name=_PUBLISH, origin_context_id=None
        )
    await require_publishable(
        runtime, world.auth(executing="agent_shared"), tool_name=_PUBLISH, origin_context_id=None
    )


CHAT_THREAD = "666666666666666666"
_UPLOAD = "create_attachment_upload_url"


async def _chat_turn_in_room(
    sessionmaker: async_sessionmaker[AsyncSession],
    world: _World,
    *,
    gated: str | None = _UPLOAD,
    live: bool = True,
) -> str:
    """A chat turn of C's own agent in C, as the 2026-10-08 publish ran: its verified
    origin, and its thread's live session holding `gated` on `always_ask`, as MA
    reports a session paused on the card."""
    now = dt.datetime.now(dt.UTC)
    async with sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=world.tenant_id,
            account_id=world.account_id,
            platform="discord",
            parent_channel_id=ROOM,
            thread_id=CHAT_THREAD,
            responder_ma_agent_id="agent_local",
            responder_name="local",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + dt.timedelta(minutes=10),
            now=now,
        )
        if not live:
            return str(origin.id)
        await create_thread_session(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            thread_id=CHAT_THREAD,
            account_id=world.account_id,
            ma_session_id="sesn_chat",
            ma_agent_id="agent_local",
        )
    configs = (
        []
        if gated is None
        else [{"name": gated, "enabled": True, "permission_policy": {"type": "always_ask"}}]
    )
    toolset = {
        "type": "mcp_toolset",
        "mcp_server_name": "daimon-mcp",
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        "configs": configs,
    }
    frozen = ma_session_agent(id="agent_local", name="local", tools=[toolset])
    world.sessions["sesn_chat"] = ma_session(id="sesn_chat", agent=frozen).model_dump(mode="json")
    return str(origin.id)


async def test_an_approved_publish_from_a_rule_held_channel_runs(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Production 2026-10-08 (D2): C's own agent asked to upload a notebook attachment,
    the person pressed Approve, and MA ran the call. Read from the session itself, with
    no stand-in for the gate, that call publishes."""
    world, runtime = await _world(committing_sessionmaker)
    origin = await _chat_turn_in_room(committing_sessionmaker, world)
    local = world.auth(admin=False, executing="agent_local")

    with capture_logs() as logs:
        await require_publishable(runtime, local, tool_name=_UPLOAD, origin_context_id=origin)

    assert not [log for log in logs if log["event"] == "publish_gate.needs_approval"]


@pytest.mark.parametrize(
    ("case", "gap", "named"),
    [
        ("turn_ended", "no_origin", True),
        ("no_origin_named", "no_origin", False),
        ("another_tool", "session_not_gated", True),
        ("session_not_gated", "session_not_gated", True),
        ("no_live_session", "no_live_session", True),
    ],
)
async def test_a_publish_without_its_card_stays_refused_and_says_why(
    committing_sessionmaker: async_sessionmaker[AsyncSession], case: str, gap: str, named: bool
) -> None:
    """A rule-held channel still asks (#412): a call whose turn already ended (the
    origin is gone, as when the driver dropped the 2026-10-08 turns), one naming no
    origin, a tool the session does not hold on the card, or a thread with no live
    session is refused. The refusal logs which proof of the card was missing."""
    world, runtime = await _world(committing_sessionmaker)
    origin = await _chat_turn_in_room(
        committing_sessionmaker,
        world,
        gated=None if case == "session_not_gated" else _UPLOAD,
        live=case != "no_live_session",
    )
    if case == "turn_ended":
        async with committing_sessionmaker.begin() as session:
            await delete_origin(session, origin_id=uuid.UUID(origin))
    tool = _PUBLISH if case == "another_tool" else _UPLOAD
    local = world.auth(admin=False, executing="agent_local")

    with capture_logs() as logs, pytest.raises(ToolError, match="presses Approve"):
        await require_publishable(
            runtime, local, tool_name=tool, origin_context_id=origin if named else None
        )

    refused = [log for log in logs if log["event"] == "publish_gate.needs_approval"]
    assert len(refused) == 1
    assert (refused[0]["tool"], refused[0]["card_gap"], refused[0]["origin_named"]) == (
        tool,
        gap,
        named,
    )


async def test_a_chat_turn_whose_agent_is_gone_is_refused_while_a_channel_keeps_its_own(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """It may have been C's own agent, so it must not be judged from outside."""
    world, runtime = await _world(committing_sessionmaker)
    with pytest.raises(ToolError, match="could not be found"):
        await _list_agents_impl(runtime, world.auth(executing="agent_gone"), None)


async def test_an_own_agents_channel_environment_name_shows_only_inside_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A name only C picks could name its client, so only callers inside C see it. Names
    other channels pick, and spare ones, show to all; an operator token sees every one."""
    world, runtime = await _world(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        for channel, name in ((ROOM, "acme-env"), (OTHER, "other-env")):
            await save_scope_environment(
                session,
                tenant_id=world.tenant_id,
                channel_id=channel,
                environment_name=name,
                actor_account_id=world.account_id,
            )
    environments = [
        ma_environment(id=f"env_{name}", name=name, tenant_id=world.tenant_id).model_dump(
            mode="json"
        )
        for name in ("acme-env", "other-env", "spare")
    ]

    def environments_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/environments":
            return list_response(environments)
        raise NotHandled

    runtime = replace(
        runtime,
        client=build_fake_anthropic(
            combine_handlers(environments_handler, make_fake_ma_handler(world.state))
        ),
    )

    async def names(auth: AuthIdentity) -> set[str]:
        return {e.name for e in await _list_environments_impl(runtime, auth, None)}

    assert await names(world.auth()) == {"other-env", "spare"}, "outside C its name is hidden"
    inside = world.auth(executing="agent_local")
    assert await names(inside) == {"acme-env", "other-env", "spare"}, "inside C it shows"
    operator = replace(
        world.auth(),
        token_kind="operator",
        token_jti=uuid.uuid4(),
        scopes=frozenset({"tenant:read"}),
    )
    assert await names(operator) == {"acme-env", "other-env", "spare"}
    with pytest.raises(ToolError, match="not found"):
        await _get_environment_impl(runtime, world.auth(), "acme-env")
    assert (await _get_environment_impl(runtime, inside, "acme-env")).name == "acme-env"
    with pytest.raises(ToolError, match="No environment named 'acme-env'"):
        await _set_channel_environment_impl(
            runtime, world.auth(), environment_name="acme-env", channel_id=None
        )

    async def summary_row(auth: AuthIdentity) -> tuple[str | None, str | None]:
        summary = await _get_tenant_summary_impl(runtime, auth)
        row = next(row for row in summary.channels if row.channel_id == ROOM)
        return row.agent_name, row.environment_name

    assert await summary_row(world.auth()) == (None, None), "the summary hides C's names outside"
    assert await summary_row(operator) == ("local", "acme-env"), "an operator token sees them"
    cleared = await _clear_channel_environment_impl(
        runtime, world.auth(), channel_id=ROOM, confirm_open_network=True
    )
    assert cleared.changed and cleared.previous_environment_name is None, "cleared, unnamed"


async def test_no_one_publishes_an_own_agents_reader(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A reader answers as its source for whoever holds the link: never C's own agent."""
    world, runtime = await _world(committing_sessionmaker)
    own, shared = (
        ma_agent(id=f"agent_{name}", name=name, tenant_id=world.tenant_id)
        for name in ("local", "shared")
    )
    with pytest.raises(ToolError, match="is a channel's own agent"):
        await require_reader_source_publishable(runtime, world.auth(), own)
    await require_reader_source_publishable(runtime, world.auth(), shared)


async def test_an_own_agent_schedules_nothing_outside(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routine paths: from C the agent can neither schedule another agent nor edit a
    routine running elsewhere, so no trigger carries C's content out."""
    world, runtime = await _world(committing_sessionmaker)

    async def destination(
        runtime: McpRuntime, auth: AuthIdentity, **kwargs: str | None
    ) -> str | None:
        return kwargs["destination_id"]

    monkeypatch.setattr(routines_mod, "_check_destination", destination)
    async with committing_sessionmaker.begin() as session:
        elsewhere = await routines_store.create_routine(
            session,
            tenant_id=world.tenant_id,
            created_by_user_id="444444444444444444",
            agent_id="agent_shared",
            agent_name="shared",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="hi",
            enabled=True,
            next_fire_at=None,
            destination_kind="channel",
            destination_id=OTHER,
            channel_id=OTHER,
        )
    for who in ("member", "admin", "channel admin"):
        auth = _own_agent_callers(world)[who]
        with pytest.raises(ToolError, match="no agent named"):
            await _create_routine_impl(
                runtime,
                auth,
                agent_name="shared",
                cron_expr="0 * * * *",
                timezone="UTC",
                trigger_message="the client's plans",
                destination_kind="channel",
                destination_id=OTHER,
            )
        with pytest.raises(ToolError, match="routine not found"):
            await _update_routine_impl(
                runtime, auth, routine_id=elsewhere.id, trigger_message="the client's plans"
            )


async def test_a_timer_set_in_a_channel_kept_to_its_own_agents_lists_only_inside_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A timer's note is the agent's words about C, so listing it from outside, even
    the same person's own, would carry them out."""
    world, runtime = await _world(committing_sessionmaker)
    when = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    for channel, agent_id, name in (
        (ROOM, "agent_local", "local"),
        (OTHER, "agent_shared", "shared"),
    ):
        await schedule_timer(
            committing_sessionmaker,
            tenant_id=world.tenant_id,
            platform="discord",
            parent_channel_id=channel,
            thread_id=channel,
            requester_account_id=world.account_id,
            requester_external_user_id="444444444444444444",
            target_ma_agent_id=agent_id,
            target_name=name,
            note=f"check on {name}",
            fire_at=when,
        )
    outside = await _list_timers_impl(runtime, world.auth(executing="agent_shared"))
    assert [t.note for t in outside] == ["check on shared"], "C's timer stays inside C"
    inside = await _list_timers_impl(runtime, world.auth(executing="agent_local"))
    assert [t.note for t in inside] == ["check on local"], "and only C's shows there"


async def test_a_running_held_turn_holds_calls_that_name_no_origin(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat token names no turn: while one of its turns runs in C, a call that
    leaves out the origin is held to C anyway, for reads, posts and lookups."""
    world, runtime = await _world(committing_sessionmaker)
    builtin = world.auth(admin=False, executing="agent_shared")
    free = await load_read_policy(runtime, builtin, origin_context_id=None)
    free.require(OTHER)  # no turn running: as before
    await require_channel_writable(runtime, builtin, channel_id=OTHER)
    await require_dm_recipient_allowed(runtime, builtin, recipient_id="U_SOMEONE")

    origin_id = await _setup_thread_origin(committing_sessionmaker, world)
    held = await load_read_policy(runtime, builtin, origin_context_id=None)
    with pytest.raises(ToolError, match="nothing outside it is read"):
        held.require(OTHER)
    with pytest.raises(ToolError, match="only they read it"):
        held.require(ROOM)
    with pytest.raises(ToolError, match="nothing said here is posted"):
        await require_channel_writable(runtime, builtin, channel_id=OTHER)
    with pytest.raises(ToolError, match="nothing said here is posted"):
        await require_channel_writable(runtime, builtin, channel_id="a:own-dm")
    with pytest.raises(ToolError, match="only they post in it"):
        await require_channel_writable(runtime, builtin, channel_id=ROOM)  # nothing granted
    with pytest.raises(ToolError, match="sends no direct messages"):
        await require_dm_recipient_allowed(runtime, builtin, recipient_id="U_SOMEONE")

    async def visible(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
        return channel_id

    async def verified(*args: object) -> None:
        return None

    monkeypatch.setattr(channel_target, "resolve_visible_channel", visible)
    monkeypatch.setattr(participation_mod, "verify_participation_scope", verified)
    with pytest.raises(ToolError, match="nothing outside it is read"):
        await _get_channel_budget_impl(runtime, builtin, OTHER)
    with pytest.raises(ToolError, match="nothing outside it is read"):
        await _get_thread_participation_impl(runtime, builtin, None, OTHER)
    await _get_channel_budget_impl(runtime, builtin, ROOM, origin_id)
    other_account = replace(builtin, account_id=uuid.uuid4())
    (await load_read_policy(runtime, other_account, origin_context_id=None)).require(OTHER)


async def test_turns_running_in_two_own_agents_channels_refuse_an_unnamed_call(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(
                channel_rules={ROOM: OWN, NEW_ROOM: OWN},
                agent_rules={"local": AgentRule(runs_in=(ROOM,))},
            ),
        )
        now = dt.datetime.now(dt.UTC)
        await create_origin(
            session,
            tenant_id=world.tenant_id,
            account_id=world.account_id,
            platform="discord",
            parent_channel_id=NEW_ROOM,
            thread_id=NEW_ROOM,
            responder_ma_agent_id="agent_shared",
            responder_name="shared",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + dt.timedelta(minutes=10),
            now=now,
        )
    origin_id = await _setup_thread_origin(committing_sessionmaker, world)
    builtin = world.auth(admin=False, executing="agent_shared")
    with pytest.raises(ToolError, match="more than one channel kept to its own agents"):
        await load_read_policy(runtime, builtin, origin_context_id=None)
    named = await load_read_policy(runtime, builtin, origin_context_id=origin_id)
    with pytest.raises(ToolError, match="nothing outside it is read"):
        named.require(NEW_ROOM)
    local = world.auth(admin=False, executing="agent_local")
    await load_read_policy(runtime, local, origin_context_id=None)  # its own channel holds it

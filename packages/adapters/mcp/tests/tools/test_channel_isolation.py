"""Channel isolation through the MCP tools: the admin tool and every filtered surface.

C (``ROOM``) is isolated: sealed, with its default agent ``local`` pinned to it.
``shared`` answers in another channel. A call is inside C when it executes as
``local``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.routines as routines_mod
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_channel_writable, turn_origin_place
from daimon.adapters.mcp.tools.agents import (
    _get_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _list_agents_impl,  # pyright: ignore[reportPrivateUsage]
    _update_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.channel_isolation import (
    _set_channel_isolation_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl
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
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores import routines as routines_store
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.turn_origins import create_origin, get_active_origin
from daimon.core.stores.user_skills import upsert_user_skill
from daimon.testing import ma_agent
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

ROOM = "111111111111111111"
OTHER = "222222222222222222"
NEW_ROOM = "333333333333333333"
SETUP_THREAD = "555555555555555555"


class _World:
    def __init__(self, tenant_id: uuid.UUID, account_id: uuid.UUID, state: FakeMAState) -> None:
        self.tenant_id, self.account_id, self.state = tenant_id, account_id, state

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
    isolate: bool = True,
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
        if isolate:
            await set_access_policy(
                session,
                tenant_id=tenant.id,
                policy=TenantAccessPolicy(
                    sealed_channel_ids=(ROOM,),
                    isolated_channel_ids=(ROOM,),
                    agent_channel_pins={local: (ROOM,)},
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

    client = build_fake_anthropic(combine_handlers(skills_handler, make_fake_ma_handler(state)))
    return _World(tenant.id, account.id, state), _runtime(sessionmaker, client)


async def test_nothing_changes_until_a_channel_is_isolated(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker, isolate=False)
    names = {a.name for a in await _list_agents_impl(runtime, world.auth(), None)}
    assert names == {"local", "shared"}, "with nothing isolated every agent is listed"
    inside = {
        a.name for a in await _list_agents_impl(runtime, world.auth(executing="agent_local"), None)
    }
    assert inside == {"local", "shared"}, "and every caller sees the same"


async def test_agents_and_skills_split_at_the_isolation_line(
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
    with pytest.raises(ToolError, match="isolated"):
        await _set_agent_default_impl(runtime, admin, "shared", ROOM, "agent_shared")
    with pytest.raises(ToolError, match="isolated"):
        await _clear_agent_default_impl(runtime, admin, ROOM)
    with pytest.raises(ToolError, match="missing"):
        await _set_agent_default_impl(runtime, admin, "local", OTHER, "agent_local")
    async with committing_sessionmaker() as session:
        scope = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id=ROOM)
        )
    assert scope is not None and scope.agent_name == "local", "a refusal writes nothing"


async def test_routines_of_an_isolated_channel_show_only_inside_it(
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


async def test_an_isolated_channels_routines_must_deliver_inside_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no destination a routine reports by DM, outside every channel, so C's
    pinned agent needs a destination in C, on create and on every update."""
    world, runtime = await _world(committing_sessionmaker)

    async def destination(
        runtime: McpRuntime, auth: AuthIdentity, **kwargs: str | None
    ) -> str | None:
        return kwargs["destination_id"]

    monkeypatch.setattr(routines_mod, "_check_destination", destination)
    inside = world.auth(admin=False, executing="agent_local")
    every_hour = {"cron_expr": "0 * * * *", "timezone": "UTC", "trigger_message": "hi"}
    for kind, target in ((None, None), ("channel", OTHER)):
        with pytest.raises(ToolError, match="pinned to specific channels"):
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
    with pytest.raises(ToolError, match="pinned to specific channels"):
        await _update_routine_impl(runtime, inside, routine_id=routine.id, clear_destination=True)
    with pytest.raises(ToolError, match="pinned to specific channels"):
        await _update_routine_impl(
            runtime, inside, routine_id=routine.id, destination_kind="channel", destination_id=OTHER
        )


async def test_set_channel_isolation_refuses_or_forks_and_ends(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker, isolate=False)
    with pytest.raises(ToolError, match="admin"):
        await _set_channel_isolation_impl(
            runtime, world.auth(admin=False), channel_id=ROOM, isolated=True
        )
    with pytest.raises(ToolError, match="no agent of its own"):
        await _set_channel_isolation_impl(runtime, world.auth(), channel_id=NEW_ROOM, isolated=True)

    kept = await _set_channel_isolation_impl(runtime, world.auth(), channel_id=ROOM, isolated=True)
    assert (kept.agent_name, kept.forked_from, kept.changed) == ("local", None, True)

    forked = await _set_channel_isolation_impl(
        runtime, world.auth(), channel_id=NEW_ROOM, isolated=True, fork_from="shared"
    )
    assert forked.forked_from == "shared" and forked.agent_name == "channel-333333", (
        "without a readable channel name the copy is named from the id"
    )
    names = {str(agent["name"]) for agent in world.state.agents.values()}
    assert "channel-333333" in names, "the copy exists"
    async with committing_sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=world.tenant_id)
    assert set(policy.isolated_channel_ids) == {ROOM, NEW_ROOM}
    assert policy.agent_channel_pins == {"local": (ROOM,), "channel-333333": (NEW_ROOM,)}, (
        "each channel's own agent is pinned to it"
    )

    ended = await _set_channel_isolation_impl(
        runtime, world.auth(), channel_id=ROOM, isolated=False
    )
    assert ended.changed and not ended.isolated, "ending isolation reports the change"


async def test_explain_agent_resolution_stays_on_the_callers_side(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    outside, inside = world.auth(), world.auth(executing="agent_local")

    with pytest.raises(ToolError, match="across an isolated channel's line"):
        await _explain_agent_resolution_impl(runtime, outside, ROOM)
    with pytest.raises(ToolError, match="across an isolated channel's line"):
        await _explain_agent_resolution_impl(runtime, inside, OTHER)
    here = await _explain_agent_resolution_impl(runtime, inside, ROOM)
    assert (here.effective_agent_name, here.deployment_default) == ("local", None), (
        "inside, the shared fallback is not named"
    )
    there = await _explain_agent_resolution_impl(runtime, outside, OTHER)
    assert there.effective_agent_name == "shared"


async def test_posts_and_direct_messages_stay_on_their_side(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime = await _world(committing_sessionmaker)
    outside, inside = world.auth(executing="agent_shared"), world.auth(executing="agent_local")

    with pytest.raises(ToolError, match="only its own agents post"):
        await require_channel_writable(runtime, outside, channel_id=ROOM)
    with pytest.raises(ToolError, match="only its own agents post"):
        await require_channel_writable(runtime, outside, channel_id="t1", parent_channel_id=ROOM)
    with pytest.raises(ToolError, match="pinned to its own channels"):
        await require_channel_writable(runtime, inside, channel_id=OTHER)
    await require_channel_writable(runtime, inside, channel_id="t1", parent_channel_id=ROOM)
    await require_channel_writable(runtime, outside, channel_id=OTHER)
    await require_channel_writable(runtime, world.auth(), channel_id=ROOM)  # no agent: an operator
    with pytest.raises(ToolError, match="sends no direct messages"):
        await send_direct_message_impl(runtime, inside, recipient_id="123", content="hi")


async def _setup_thread_origin(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World
) -> str:
    """A setup conversation in C, answered by the built-in (``shared`` here)."""
    now = dt.datetime.now(dt.UTC)
    async with sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=world.tenant_id,
            account_id=world.account_id,
            platform="discord",
            parent_channel_id=ROOM,
            thread_id=SETUP_THREAD,
            responder_ma_agent_id="agent_shared",
            responder_name="shared",
            configuration_target_ma_agent_id="agent_local",
            configuration_target_name="local",
            role=Role.USER,
            expires_at=now + dt.timedelta(minutes=10),
            now=now,
            is_setup=True,
        )
    return str(origin.id)


async def test_the_setup_thread_configures_its_channels_own_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """From C's setup thread, named by its verified origin, the built-in sees and
    configures C's own agent; without the origin, or on an agent key, it stays outside."""
    world, runtime = await _world(committing_sessionmaker)
    origin = await _setup_thread_origin(committing_sessionmaker, world)
    builtin = world.auth(admin=False, executing="agent_shared")

    listed = await _list_agents_impl(runtime, builtin, None, origin)
    assert [a.name for a in listed] == ["local"], "the setup thread sees C's own agents"
    found = await _get_agent_impl(runtime, builtin, "local", origin_context_id=origin)
    assert found.name == "local"
    world.state.agents["agent_local"]["metadata"]["daimon_account"] = str(world.account_id)
    updated = await _update_agent_impl(
        runtime,
        builtin,
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
    assert updated.description == "notes for the room", "a member configures C's agent there"

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
    with pytest.raises(ToolError, match="only its own agents post"):
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

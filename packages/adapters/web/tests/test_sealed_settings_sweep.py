"""Every mutating web route is swept for the inside-only rule of a private channel.

A channel with limited readers, and that channel's own agent, change only from inside the
channel, admins included (`daimon.core.permissions.sealed_outside`). The web app has no
verified origin, so an admin here is always outside. The routes come from the live route
registry, not a list of funnels: a route that is neither swept nor listed in `EXEMPT`, with
its reason, fails `test_every_mutating_route_is_swept_or_exempt`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import httpx
import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.config import Settings, TurnSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.permissions import SEALED_REFUSAL
from daimon.core.platform_names import ChannelSighting
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role as AccountRole
from daimon.core.stores.platform_names import upsert_channel_names
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.ma_models import ma_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, agent_client, agent_people, make_admin, sign_in_token
from .test_agent_keys_routes import _MCP  # pyright: ignore[reportPrivateUsage]

AGENT = "ag_w"
AGENT_NAME = "helper"
SEAL = "C_SEAL"
OPEN = "C_OPEN"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SKILL = "sk_1"
_SERVERS = [{"type": "url", "name": "notes", "url": "https://notes.example.com/mcp"}]

#: Routes that take the sealed channel or its own agent, with the minimal body that gets past
#: validation. The key is "METHOD path"; `{agent_id}` and `{channel_id}` are filled in.
SWEPT: dict[str, object] = {
    "POST /api/agents/{agent_id}/github/connect": None,
    "PUT /api/agents/{agent_id}/github/working": {"repo": None},
    "DELETE /api/agents/{agent_id}/github/repos/{owner}/{name}": None,
    "POST /api/agents/{agent_id}/github/resync": None,
    "PUT /api/agents/{agent_id}/keys/{key}": {"value": "secret"},
    "DELETE /api/agents/{agent_id}/keys/{key}": None,
    "POST /api/agents/{agent_id}/coding-keys": None,
    "POST /api/agents/{agent_id}/coding-keys/connect": None,
    "DELETE /api/agents/{agent_id}/coding-keys/{key_id}": None,
    "PUT /api/agents/{agent_id}/owners": {"owner": None, "co_owners": []},
    "PUT /api/agents/{agent_id}/tool-rules": {
        "tool": "notes/write",
        "rule": None,
        "scope": "chat",
        "expected_revision": 0,
    },
    "POST /api/agents/{agent_id}/archive": None,
    "POST /api/agents/{agent_id}/copy": {"name": "copy"},
    "PUT /api/agents/{agent_id}/skill-follows": {"skill_id": SKILL, "follow": True},
    "POST /api/agents/{agent_id}/skill-follows/{skill_id}/apply": None,
    "PATCH /api/agents/{agent_id}/draft": {"base_version": 1, "config": "AGENT_DOC"},
    "DELETE /api/agents/{agent_id}/draft": None,
    "POST /api/agents/{agent_id}/apply": {},
    "PUT /api/agents/{agent_id}/helpers": {"helpers": []},
    "POST /api/agents/{agent_id}/skills": {"files": {"SKILL.md": "x"}},
    "PUT /api/agents/{agent_id}/mcp/{key}/token": {"token": "secret"},
    "DELETE /api/agents/{agent_id}/mcp/{key}/token": None,
    "POST /api/agents/{agent_id}/revert": {"version": 1},
    "PATCH /api/admin/agent-access/{agent_id}": {"web": False},
    "PUT /api/channels/{channel_id}/default-agent": {"agent": None, "expected": None},
    "POST /api/channels/{channel_id}/kept-agents": {
        "action": "add",
        "agent": AGENT_NAME,
        "expected": [],
    },
    "PUT /api/channels/{channel_id}/access": {
        "readers": "inside",
        "expected": {"readers": "own", "writers": "own"},
        "keep_rules": True,
    },
    "PUT /api/channels/{channel_id}/budget": {"action": "clear", "expected": None},
    "PUT /api/channels/{channel_id}/environment": {"environment": None, "expected": None},
    "POST /api/channels/{channel_id}/skills": {"action": "add", "skill": SKILL, "expected": []},
    "PUT /api/channels/{channel_id}/tool-rules": {
        "agent": AGENT_NAME,
        "tool": "x/y",
        "rule": None,
        "expected_revision": 0,
    },
    "POST /api/channels/{channel_id}/provider-pin": {"value": "openai", "expected_local": None},
    "POST /api/channels/{channel_id}/provider-pin/clear": {"expected_local": 0},
    "POST /api/channels/{channel_id}/channel-copy/retire": {
        "name": AGENT_NAME,
        "agent_id": AGENT,
    },
    "PUT /api/features?channel={channel_id}": {
        "key": "progress_plans",
        "tier": "channel",
        "value": "on",
    },
    "PUT /api/features?agent={agent_id}": {"key": "long_jobs", "tier": "agent", "value": "on"},
    "POST /api/channels/{channel_id}/threads/clear": {"thread_id": "t1", "kind": "progress_plans"},
    # Archiving clears every channel's pick of the environment; `SHARED_ENV` is picked by SEAL and OPEN.
    "DELETE /api/agents/environments/{environment_id}": None,
}

_MANAGED = (
    "a seeded agent's managed refusal comes before the seal; the same handler reaches the seal "
    "for the custom agent in the same sweep"
)
_CANVAS = (
    "a custom agent's helpers change on its canvas (PATCH draft, swept), so this route is 422 "
    "for it before any seal check; the seeded agent in the same sweep reaches the seal"
)

#: Routes that refuse an outside admin in their own words, with the reason. The key is the
#: route and whether the agent is a seeded one (None: either). Each is still a 403 or a 422 that
#: changes nothing; the sealed words are the default. `test_every_other_refusal_has_a_twin_at_the_seal`
#: checks that the run of the other kind of agent reaches the seal.
OTHER_REFUSAL: dict[tuple[str, bool | None], tuple[int, str, str]] = {
    ("PATCH /api/admin/agent-access/{agent_id}", None): (
        403,
        "This agent is kept to its channel.",
        "the agent's own refusal: it is kept to its channel and cannot take web access from outside",
    ),
    ("DELETE /api/agents/{agent_id}/draft", True): (403, "is managed by defaults", _MANAGED),
    ("DELETE /api/agents/{agent_id}/mcp/{key}/token", True): (
        403,
        "is managed by defaults",
        _MANAGED,
    ),
    ("PATCH /api/agents/{agent_id}/draft", True): (403, "is managed by defaults", _MANAGED),
    ("POST /api/agents/{agent_id}/apply", True): (403, "is managed by defaults", _MANAGED),
    ("POST /api/agents/{agent_id}/revert", True): (403, "is managed by defaults", _MANAGED),
    ("POST /api/agents/{agent_id}/skills", True): (403, "is managed by defaults", _MANAGED),
    ("PUT /api/agents/{agent_id}/mcp/{key}/token", True): (
        403,
        "is managed by defaults",
        _MANAGED,
    ),
    ("PUT /api/agents/{agent_id}/helpers", False): (
        422,
        "Change this agent's helpers on its canvas, then apply.",
        _CANVAS,
    ),
}

_DM_ROUTING = (
    "workspace level: DM routing names agents but changes none; a channel's own agent "
    "answers only in its home"
)

_WORKFLOW = (
    "a workflow of the caller: its draft, versions, runs and triggers, held by the workflow's "
    "roles (core/workflows/access.py); content side, as a routine. It names channels and "
    "agents but changes no channel's or agent's settings"
)
_HOOK = (
    "an outside sender's delivery to a workflow trigger: signed by the trigger's secret, no "
    "caller; it starts a run and changes no channel's or agent's settings"
)

#: Every other mutating route, with why it is not swept.
EXEMPT: dict[str, str] = {
    "POST /auth/logout": ("signs the caller out"),
    "POST /auth/{provider_id}/connect": ("links the caller's own sign-in"),
    "DELETE /api/me/accounts/{key}": ("a person's own data, not a channel or an agent's settings"),
    "POST /api/workspaces/switch": ("switches the caller's own workspace"),
    "POST /api/profile/memory/{agent_id}/forget": (
        "the caller forgets their own memory of an agent; a person's own data, not a channel or an agent's settings"
    ),
    "POST /api/profile/connections/disconnect": (
        "the caller drops their own connection; a person's own data, not a channel or an agent's settings"
    ),
    "DELETE /api/profile/sign-ins/{identity_id}": (
        "a person's own data, not a channel or an agent's settings"
    ),
    "DELETE /api/profile/keys/{key_id}": (
        "a person's own data, not a channel or an agent's settings"
    ),
    "POST /api/profile/delete": ("a person's own data, not a channel or an agent's settings"),
    "POST /api/agents/server-skills": (
        "workspace level: no channel and no agent (the skill library)"
    ),
    "POST /api/agents/skills/{skill_id}/check": (
        "workspace level: no channel and no agent (the skill library)"
    ),
    "POST /api/agents/skills/{skill_id}/approve": (
        "workspace level: no channel and no agent (the skill library)"
    ),
    "POST /api/agents/skills/{skill_id}/fork": (
        "workspace level: no channel and no agent (the skill library)"
    ),
    "POST /api/agents/skills": ("workspace level: no channel and no agent (the skill library)"),
    "PUT /api/agents/skills/{skill_id}/files": (
        "workspace level: no channel and no agent (the skill library)"
    ),
    "POST /api/agents/skills/{skill_id}/restore": (
        "workspace level: no channel and no agent (the skill library)"
    ),
    "POST /api/agents/skills/sync": (
        "workspace level: no channel and no agent (the skill library)"
    ),
    "DELETE /api/agents/skills/{skill_id}": (
        "workspace level: no channel and no agent (the skill library)"
    ),
    "POST /api/agents/environments": ("workspace level: no channel and no agent (environments)"),
    "PUT /api/agents/environments/{environment_id}": (
        "workspace level: no channel and no agent (environments)"
    ),
    "POST /api/agents": ("a new agent has no channel yet (CREATE_AGENT)"),
    "PUT /api/channel-defaults/agent": (
        "workspace level: no channel and no agent (the default for channels with none of their own)"
    ),
    "PUT /api/channel-defaults/environment": (
        "workspace level: no channel and no agent (the default for channels with none of their own)"
    ),
    "PUT /api/support/limits": ("workspace level: no channel and no agent (SET_SUPPORT)"),
    "PUT /api/support/menus/use": ("workspace level: no channel and no agent (SET_SUPPORT)"),
    "PUT /api/support/menus/{key}": ("workspace level: no channel and no agent (SET_SUPPORT)"),
    "PUT /api/support/channel": (
        "names where requests for help are posted, and changes no channel (SET_SUPPORT)"
    ),
    "POST /api/support/requests/{request_id}/settle": (
        "workspace level: no channel and no agent (a support request)"
    ),
    "POST /api/support/requests/{request_id}/decline": (
        "workspace level: no channel and no agent (a support request)"
    ),
    "PUT /api/features": (
        "the workspace tier; the channel and agent tiers are swept as PUT /api/features?channel= and ?agent="
    ),
    "POST /api/chat/sessions": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "PATCH /api/chat/sessions/{session_id}": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "POST /api/chat/sessions/{session_id}/monitoring/dismiss": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "POST /api/chat/sessions/{session_id}/switch-back": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "PUT /api/chat/sessions/{session_id}/model": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION); it changes the session, not the agent"
    ),
    "DELETE /api/chat/sessions/{session_id}": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "POST /api/chat/sessions/{session_id}/messages": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "POST /api/chat/sessions/{session_id}/stop": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "POST /api/chat/sessions/{session_id}/cards/{card_id}": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "POST /api/chat/messages/{message_id}/feedback": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "POST /api/chat/uploads": (
        "a person's own chat session: running an agent is the content side (RUN_AGENT, READ_SESSION)"
    ),
    "POST /api/routines": (
        "a routine is a scheduled run or post, held by SAVE_ROUTINE (content side); a web caller also needs a linked chat account"
    ),
    "PATCH /api/routines/{routine_id}": ("as POST /api/routines"),
    "DELETE /api/routines/{routine_id}": ("as POST /api/routines"),
    "POST /api/routines/{routine_id}/pause": ("as POST /api/routines"),
    "POST /api/routines/{routine_id}/resume": ("as POST /api/routines"),
    "POST /api/routines/{routine_id}/run": ("as POST /api/routines"),
    "POST /api/credits/allowances": ("workspace level: no channel and no agent (credit)"),
    "PATCH /api/credits/allowances/{allowance_id}": (
        "workspace level: no channel and no agent (credit)"
    ),
    "DELETE /api/credits/allowances/{allowance_id}": (
        "workspace level: no channel and no agent (credit)"
    ),
    "POST /api/credits/redeem": ("workspace level: no channel and no agent (credit)"),
    "POST /api/credits/topup": ("workspace level: no channel and no agent (credit)"),
    "POST /api/integrations/tokens": (
        "workspace level: no channel and no agent (API tokens; MANAGE_INTEGRATIONS)"
    ),
    "DELETE /api/integrations/tokens/{jti}": (
        "workspace level: no channel and no agent (API tokens; MANAGE_INTEGRATIONS)"
    ),
    "POST /api/invites/{token}/accept": ("a person accepts their own invite"),
    "PATCH /api/admin/people/{person_id}": ("workspace level: no channel and no agent (people)"),
    "POST /api/admin/people/{person_id}/link": (
        "workspace level: no channel and no agent (people)"
    ),
    "DELETE /api/admin/people/{person_id}/link": (
        "workspace level: no channel and no agent (people)"
    ),
    "POST /api/admin/invites": ("workspace level: no channel and no agent (invites)"),
    "PATCH /api/admin/invites/{invite_id}": ("workspace level: no channel and no agent (invites)"),
    "DELETE /api/admin/invites/{invite_id}": ("workspace level: no channel and no agent (invites)"),
    "PATCH /api/admin/settings": (
        "workspace level: no channel and no agent (SET_WORKSPACE_SETTINGS)"
    ),
    "PATCH /api/admin/dm": _DM_ROUTING,
    "POST /api/admin/dm/rules": _DM_ROUTING,
    "PATCH /api/admin/dm/rules/{position}": _DM_ROUTING,
    "DELETE /api/admin/dm/rules/{position}": _DM_ROUTING,
    "POST /api/workflows": _WORKFLOW,
    "POST /api/workflows/runs/{run_id}/cancel": _WORKFLOW,
    "POST /api/workflows/runs/{run_id}/redrive": _WORKFLOW,
    "POST /api/workflows/runs/{run_id}/decide": _WORKFLOW,
    "POST /api/workflows/runs/{run_id}/rerun": _WORKFLOW,
    "POST /api/workflows/runs/{run_id}/fork": _WORKFLOW,
    "POST /api/workflows/inbox/{item_id}/decide": _WORKFLOW,
    "POST /api/workflows/from-routine/{routine_id}": _WORKFLOW,
    "PATCH /api/workflows/{workflow_id}": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/to-routine": _WORKFLOW,
    "DELETE /api/workflows/{workflow_id}": _WORKFLOW,
    "PUT /api/workflows/{workflow_id}/draft": _WORKFLOW,
    "DELETE /api/workflows/{workflow_id}/draft": _WORKFLOW,
    "DELETE /api/workflows/{workflow_id}/state": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/draft/publish": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/draft/request": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/validate": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/versions/{number}/restore": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/runs": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/triggers/{key}/preview": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/triggers/{key}/listen": _WORKFLOW,
    "DELETE /api/workflows/{workflow_id}/triggers/{key}/listen": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/triggers/{key}/rotate": _WORKFLOW,
    "POST /api/workflows/{workflow_id}/triggers/{key}/deliveries/{delivery}/replay": _WORKFLOW,
    "POST /hooks/w/{trigger_id}": _HOOK,
    "POST /hooks/w/{trigger_id}/test": _HOOK,
    "POST /hooks/callback/{run_id}/{node_id}/{signature}": (
        "a run's signed callback: it hands data to one Wait of that run and changes no "
        "channel's or agent's settings"
    ),
    "POST /api/admin/issues/{fingerprint}/state": (
        "workspace level: an issue's resolve or mute state, no channel and no agent settings"
    ),
    "DELETE /{path:path}": "the app's page fallback: it serves the page and changes nothing",
    "PATCH /{path:path}": "the app's page fallback: it serves the page and changes nothing",
    "POST /{path:path}": "the app's page fallback: it serves the page and changes nothing",
    "PUT /{path:path}": "the app's page fallback: it serves the page and changes nothing",
}

FILL = {
    "agent_id": AGENT,
    "channel_id": SEAL,
    "owner": "owner",
    "name": "repo",
    "key": "key",
    "key_id": str(uuid.uuid4()),
    "skill_id": SKILL,
    "environment_id": "env_shared",
}
SHARED_ENV = "Shared"
OPEN_ENV = "OpenOnly"
#: Each environment archive the provider received.
ARCHIVED: list[str] = []
#: The workspace of the last `_sealed` call.
TENANT: list[uuid.UUID] = []


@asynccontextmanager
async def _sealed(
    db_session: AsyncSession, factory: async_sessionmaker[AsyncSession], *, seeded: bool
) -> AsyncIterator[httpx.AsyncClient]:
    """An admin with no origin, a private channel and its own agent, and an open channel."""
    agents, owner, other = await agent_people(db_session, mcp_servers=_SERVERS)
    if seeded:
        metadata = {**agents.agent.metadata, MA_METADATA_KEY_MANAGED: "true"}
        agents.agent = agents.agent.model_copy(update={"metadata": metadata})
    await make_admin(factory, other)
    async with factory.begin() as db:
        # A server admin too, so only the seal can refuse a feature change.
        await set_role(db, other.account_id, AccountRole.ADMIN)
        await upsert_channel_names(
            db,
            tenant_id=owner.tenant_id,
            platform="discord",
            names={SEAL: ChannelSighting("vault"), OPEN: ChannelSighting("lobby")},
        )
        await set_access_policy(
            db,
            tenant_id=owner.tenant_id,
            policy=TenantAccessPolicy(
                channel_rules={SEAL: ChannelRule(readers="own", writers="own")},
                agent_rules={AGENT_NAME: AgentRule(runs_in=(SEAL,))},
            ),
        )
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        mcp=_MCP,  # pyright: ignore[reportArgumentType]
        turn=TurnSettings(channel_backends=True),
        tool_safety={"enabled": True, "agent_rules": True},  # pyright: ignore[reportArgumentType]
    )
    # An environment SEAL and OPEN both pick (so the isolation view shows it), and one only OPEN picks.
    shared = ma_environment(id="env_shared", name=SHARED_ENV, tenant_id=owner.tenant_id)
    open_only = ma_environment(id="env_open", name=OPEN_ENV, tenant_id=owner.tenant_id)
    agents.router.add_environment_list(shared, open_only, ma_environment(tenant_id=owner.tenant_id))
    agents.router.add_environment(ma_environment(tenant_id=owner.tenant_id))
    ARCHIVED.clear()
    TENANT[:] = [owner.tenant_id]

    def archive(_request: httpx.Request, found: re.Match[str]) -> httpx.Response:
        ARCHIVED.append(found.group(1))
        return httpx.Response(200, json=shared.model_dump(mode="json"))

    agents.router.add("POST", r"/v1/environments/(env_[a-z]+)/archive", archive)
    async with factory.begin() as db:
        for channel_id, name in ((SEAL, SHARED_ENV), (OPEN, SHARED_ENV)):
            await set_fields(
                db,
                scope=ChannelScopeRef(tenant_id=owner.tenant_id, channel_id=channel_id),
                tenant_id=owner.tenant_id,
                environment_name=name,
            )
    token = await sign_in_token(factory, other)
    async with agent_client(
        agents, owner.tenant_id, factory, token, settings=settings, with_turns=True
    ) as client:
        yield client


async def _call(client: httpx.AsyncClient, key: str, **fill: str) -> httpx.Response:
    method, path = key.split(" ", 1)
    body: object = SWEPT[key]
    if isinstance(body, dict):
        draft = cast("dict[str, object]", body)
        if draft.get("config") == "AGENT_DOC":
            body = {**draft, "config": (await client.get(f"/api/agents/{AGENT}")).json()}
    return await client.request(method, path.format_map({**FILL, **fill}), json=body, headers=OWN)  # pyright: ignore[reportUnknownArgumentType]


@pytest.mark.parametrize("seeded", [False, True], ids=["agent", "seeded_agent"])
@pytest.mark.parametrize("key", sorted(SWEPT))
async def test_an_admin_outside_changes_nothing_of_a_private_channel_or_its_agent(
    key: str,
    seeded: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _sealed(db_session, db_session_factory, seeded=seeded) as client:
        response = await _call(client, key)
    status, words, _reason = OTHER_REFUSAL.get(
        (key, seeded), OTHER_REFUSAL.get((key, None), (403, SEALED_REFUSAL, ""))
    )
    assert response.status_code == status, (key, response.status_code, response.text)
    assert words in response.text, (key, response.text)
    assert ARCHIVED == [], "no environment was archived"


def test_every_other_refusal_has_a_twin_at_the_seal() -> None:
    """A refusal in other words must not leave a route unswept: the other kind of agent reaches the seal."""
    for (key, seeded), (_status, _words, reason) in OTHER_REFUSAL.items():
        assert len(reason) > 20, (key, "give the reason")
        if seeded is not None:
            assert (key, not seeded) not in OTHER_REFUSAL and (key, None) not in OTHER_REFUSAL, key
        assert key in SWEPT, key


async def test_an_admin_outside_archives_no_environment_a_private_channel_picks(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _sealed(db_session, db_session_factory, seeded=False) as client:
        refused = await client.delete("/api/agents/environments/env_shared", headers=OWN)
        confirmed = await client.delete(
            "/api/agents/environments/env_shared?confirm_open_network=true", headers=OWN
        )
    for response in (refused, confirmed):
        assert response.status_code == 403, response.text
        assert SEALED_REFUSAL in response.text and "#vault" in response.text, response.text
    assert ARCHIVED == [], "nothing reached the provider"
    async with db_session_factory() as session:
        pick = await get_scope(session, scope=ChannelScopeRef(tenant_id=TENANT[0], channel_id=SEAL))
    assert pick is not None and pick.environment_name == SHARED_ENV, "the channel keeps its pick"


async def test_an_environment_only_open_channels_pick_archives_as_before(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _sealed(db_session, db_session_factory, seeded=False) as client:
        async with db_session_factory.begin() as db:
            await set_fields(
                db,
                scope=ChannelScopeRef(tenant_id=TENANT[0], channel_id=OPEN),
                tenant_id=TENANT[0],
                environment_name=OPEN_ENV,
            )
        response = await client.delete(
            "/api/agents/environments/env_open?confirm_open_network=true", headers=OWN
        )
    assert response.status_code == 200, response.text
    assert ARCHIVED == ["env_open"]


def _routes(client: httpx.AsyncClient) -> set[str]:
    """Every POST, PUT, PATCH and DELETE route of the live app, as "METHOD path"."""
    app = client._transport.app  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType, reportPrivateUsage]
    return {
        f"{method} {route.path}"  # pyright: ignore[reportUnknownMemberType]
        for route in app.routes  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        for method in getattr(route, "methods", None) or ()  # pyright: ignore[reportUnknownArgumentType]
        if method in MUTATING
    }


async def test_every_mutating_route_is_swept_or_exempt(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with _sealed(db_session, db_session_factory, seeded=False) as client:
        routes = _routes(client)
    swept = {key.split("?")[0] for key in SWEPT}
    # A route swept only for some of its places (`?channel=`) keeps an exempt entry for the rest.
    assert not {k for k in SWEPT if "?" not in k} & set(EXEMPT), "a route is swept or exempt"
    assert routes - swept - set(EXEMPT) == set(), (
        "a new mutating route must join SWEPT, or EXEMPT with its reason"
    )
    assert (swept | set(EXEMPT)) - routes == set(), "a listed route no longer exists"
    assert all(len(reason) > 10 for reason in EXEMPT.values())


@pytest.mark.parametrize(
    "key",
    sorted(
        k
        for k, body in SWEPT.items()
        if "{channel_id}" in k and AGENT_NAME not in repr(body) and AGENT not in repr(body)
    ),
)
async def test_an_open_channel_is_not_refused_as_private(
    key: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _sealed(db_session, db_session_factory, seeded=False) as client:
        response = await _call(client, key, channel_id=OPEN)
    assert SEALED_REFUSAL not in response.text, (key, response.text)

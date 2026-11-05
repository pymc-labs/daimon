"""`set_agent_helpers`: an admin, on a person's card, sets an agent's helpers in their own
workspace; the default agent's included."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _card
from daimon.adapters.mcp.tools import agent_edits as edits
from daimon.core.agent_view import AgentConfigDoc, HelperDoc, ModelChoice
from daimon.core.anthropic_guard import without_anthropic
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_helpers, web_audit
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec
from mux.profiles.selfhosted import LOOP
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Factory = async_sessionmaker[AsyncSession]


class _World:
    def __init__(self, sessionmaker: Factory) -> None:
        self.sessionmaker = sessionmaker
        self.agents: list[dict[str, object]] = []

    def add(
        self,
        tenant_id: uuid.UUID,
        agent_id: str,
        name: str,
        *,
        seeded: bool = False,
        owned: bool = False,
    ) -> None:
        metadata = {MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: name}
        if seeded:
            metadata[MA_METADATA_KEY_MANAGED] = "true"
        if owned:
            metadata[MA_METADATA_KEY_ACCOUNT] = str(uuid.uuid4())
        self.agents.append(
            ma_agent(id=agent_id, name=name, metadata=metadata).model_dump(mode="json")
        )

    def runtime(self) -> McpRuntime:
        router = MARouter()
        router.add("GET", r"/v1/agents", lambda _r, _m: list_response(self.agents))
        return McpRuntime(
            session_factory=self.sessionmaker,
            client=build_fake_anthropic(router.dispatch),
            settings=Settings(
                database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
                anthropic=AnthropicSettings(api_key=SecretStr("test")),
            ),
            deployment_default=DeploymentDefault(),
        )


async def _workspace(
    db: AsyncSession, world: _World, *, admin: bool = True, helper_of: uuid.UUID | None = None
) -> tuple[AuthIdentity, TurnOriginRow]:
    """A workspace with its own copy of the default agent and one other agent."""
    tenant = await make_tenant(db)
    account = await make_account(db, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="1.2",
        responder_ma_agent_id="ag_daimon",
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        expires_at=now + timedelta(minutes=10),
        now=now,
        chat_session_id="test-session",
        helper_of=helper_of,
    )
    await db.commit()
    world.add(tenant.id, "ag_daimon", "daimon", seeded=True)
    world.add(tenant.id, "ag_argon", "argon")
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.ADMIN if admin else Role.USER,
        platform="slack",
        chat_session_id="test-session",
        is_admin=admin,
        is_platform_admin=admin,
    )
    return auth, origin


@pytest.fixture(autouse=True)
def card_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def confirmed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(edits, "require_card", confirmed)


async def _set(
    world: _World, auth: AuthIdentity, origin: TurnOriginRow, agent: str, helpers: list[str]
) -> edits.AgentHelpersSet:
    return await edits._helpers_impl(  # pyright: ignore[reportPrivateUsage]
        world.runtime(), auth, agent, helpers, str(origin.id)
    )


async def _stored(factory: Factory, auth: AuthIdentity, agent_id: str) -> list[str]:
    async with factory() as session:
        return await agent_helpers.list_helper_ids(
            session, tenant_id=auth.tenant_id, agent_id=agent_id
        )


async def test_an_admin_sets_the_default_agents_helpers_by_name_or_id(
    db_session: AsyncSession, committing_sessionmaker: Factory
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world)

    out = await _set(world, auth, origin, "daimon", ["argon", "ag_argon"])

    assert [(h.agent_id, h.name) for h in out.helpers] == [("ag_argon", "argon")]
    assert out.agent_id == "ag_daimon"
    assert await _stored(committing_sessionmaker, auth, "ag_daimon") == ["ag_argon"]
    async with committing_sessionmaker() as session:
        rows = await web_audit.list_recent(session, tenant_id=auth.tenant_id)
    assert [r.action for r in rows] == ["Changed agent helpers"], "audited like an owners change"


async def test_a_non_admin_changes_nothing(
    db_session: AsyncSession, committing_sessionmaker: Factory
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world, admin=False)

    with pytest.raises(ToolError, match="requires"):
        await _set(world, auth, origin, "daimon", ["argon"])

    assert await _stored(committing_sessionmaker, auth, "ag_daimon") == []


async def test_a_helpers_turn_cannot_change_helpers(
    db_session: AsyncSession, committing_sessionmaker: Factory
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world, helper_of=uuid.uuid4())

    with pytest.raises(ToolError, match="helping another agent"):
        await _set(world, auth, origin, "daimon", ["argon"])

    assert await _stored(committing_sessionmaker, auth, "ag_daimon") == []


@pytest.mark.parametrize(
    ("agent", "helpers", "words"),
    [
        ("daimon", ["daimon"], "own helper"),
        ("daimon", ["nobody"], "No agent called 'nobody'"),
        ("nobody", ["argon"], "No agent called 'nobody'"),
    ],
)
async def test_the_same_checks_as_the_web_apply(
    db_session: AsyncSession,
    committing_sessionmaker: Factory,
    agent: str,
    helpers: list[str],
    words: str,
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world)

    with pytest.raises(ToolError, match=words):
        await _set(world, auth, origin, agent, helpers)

    assert await _stored(committing_sessionmaker, auth, "ag_daimon") == []


async def test_without_a_card_nothing_changes(
    db_session: AsyncSession,
    committing_sessionmaker: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world)
    monkeypatch.undo()

    async def no_origin(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(_card, "get_verified_origin", no_origin)

    with pytest.raises(ToolError, match="Nothing changed"):
        await _set(world, auth, origin, "daimon", ["argon"])

    assert await _stored(committing_sessionmaker, auth, "ag_daimon") == []


async def test_a_self_hosted_conversation_reads_its_own_agents(
    db_session: AsyncSession,
    committing_sessionmaker: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world)
    scope = Scope(
        tenant_id=str(auth.tenant_id),
        account_id=str(auth.account_id),
        principal_id="operator",
        authorization_id="test",
    )
    agents = SelfHostedAgents(committing_sessionmaker)
    ids = [
        (
            await agents.create(
                scope,
                AgentSpec(name=name, model=ModelRef(provider=LOOP.provider, id="gpt-6-luna")),
                key=name,
            )
        ).ref.id
        for name in ("lead-sh", "argon-sh")
    ]
    world.agents.clear()

    applied = _Applied()
    monkeypatch.setattr(edits.agent_changes, "apply_change", applied)

    with without_anthropic():
        out = await _set(world, auth, origin, "lead-sh", ["argon-sh"])

    assert applied.changes == [], "a self-hosted agent has no versions to record"

    assert [(h.agent_id, h.name) for h in out.helpers] == [(ids[1], "argon-sh")]
    assert await _stored(committing_sessionmaker, auth, ids[0]) == [ids[1]]


async def test_one_workspaces_edit_never_reaches_another(
    db_session: AsyncSession, committing_sessionmaker: Factory
) -> None:
    world = _World(committing_sessionmaker)
    auth_a, origin_a = await _workspace(db_session, world)
    auth_b, origin_b = await _workspace(db_session, world)
    world.add(auth_b.tenant_id, "ag_only_b", "only-b")

    await _set(world, auth_a, origin_a, "daimon", ["argon"])

    assert await _stored(committing_sessionmaker, auth_a, "ag_daimon") == ["ag_argon"]
    assert await _stored(committing_sessionmaker, auth_b, "ag_daimon") == [], (
        "the other workspace's default agent keeps its own, empty, list"
    )
    with pytest.raises(ToolError, match="No agent called 'only-b'"):
        await _set(world, auth_a, origin_a, "only-b", ["argon"])
    with pytest.raises(ToolError, match="No agent called 'only-b'"):
        await _set(world, auth_a, origin_a, "daimon", ["only-b"])
    assert await _stored(committing_sessionmaker, auth_b, "ag_only_b") == [], (
        "an admin here cannot touch an agent of another workspace"
    )
    assert auth_b.tenant_id != auth_a.tenant_id and origin_b.tenant_id == auth_b.tenant_id


def _config(*helpers: HelperDoc) -> AgentConfigDoc:
    return AgentConfigDoc(
        name="argon",
        purpose="",
        avatar="",
        model=ModelChoice(id="claude-opus", backend="anthropic", speed="standard", effort="high"),
        system=None,
        skills=[],
        tools=[],
        mcp_servers=[],
        places=None,
        helpers=list(helpers),
    )


class _Applied:
    """Stands in for `apply_change`, which the web and chat edit tests cover in full."""

    def __init__(self) -> None:
        self.changes: list[Any] = []
        self.notes: list[str] = []

    async def __call__(self, _deps: Any, **kw: Any) -> None:
        self.changes.append(kw["change"])
        self.notes.append(kw["note"])


async def test_a_helper_homed_where_the_caller_cannot_see_survives_the_edit(
    db_session: AsyncSession,
    committing_sessionmaker: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world)
    world.add(auth.tenant_id, "ag_hidden", "hidden")

    class _Sees:
        def sees_agent(self, agent: Any) -> bool:
            return agent.id != "ag_hidden"

    async def view(*_a: Any, **_k: Any) -> _Sees:
        return _Sees()

    monkeypatch.setattr(edits, "load_isolation_view", view)
    async with committing_sessionmaker.begin() as session:
        await agent_helpers.set_helpers(
            session, tenant_id=auth.tenant_id, agent_id="ag_daimon", helper_ids=["ag_hidden"]
        )

    out = await _set(world, auth, origin, "daimon", ["argon"])
    assert sorted(await _stored(committing_sessionmaker, auth, "ag_daimon")) == [
        "ag_argon",
        "ag_hidden",
    ], "the hidden helper stays beside the new one"
    assert [h.agent_id for h in out.helpers] == ["ag_argon"], "and is not shown to the caller"

    out = await _set(world, auth, origin, "daimon", [])
    assert await _stored(committing_sessionmaker, auth, "ag_daimon") == ["ag_hidden"], (
        "clearing the visible list leaves it"
    )
    assert out.helpers == [], "and the result lists nothing"


async def test_a_normal_agent_gets_a_recorded_version(
    db_session: AsyncSession,
    committing_sessionmaker: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world)
    world.add(auth.tenant_id, "ag_lead", "lead", owned=True)
    applied = _Applied()
    monkeypatch.setattr(edits.agent_changes, "apply_change", applied)

    out = await _set(world, auth, origin, "lead", ["argon"])

    assert applied.notes == ["Changed agent helpers"], "one versioned change, with its note"
    assert [h.agent_id for h in out.helpers] == ["ag_argon"]
    assert await _stored(committing_sessionmaker, auth, "ag_lead") == [], (
        "the list is written by the version, not directly"
    )
    config: AgentConfigDoc = applied.changes[0](_config(HelperDoc(id="ag_old", name="Old")))
    assert [(h.id, h.name) for h in config.helpers or []] == [("ag_argon", "argon")], (
        "the change sets exactly the wanted helpers"
    )


async def test_an_unchanged_list_writes_no_version(
    db_session: AsyncSession,
    committing_sessionmaker: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world)
    world.add(auth.tenant_id, "ag_lead", "lead", owned=True)
    async with committing_sessionmaker.begin() as session:
        await agent_helpers.set_helpers(
            session, tenant_id=auth.tenant_id, agent_id="ag_lead", helper_ids=["ag_argon"]
        )
    applied = _Applied()
    monkeypatch.setattr(edits.agent_changes, "apply_change", applied)

    out = await _set(world, auth, origin, "lead", ["argon"])

    assert applied.changes == [], "nothing to record"
    assert [h.agent_id for h in out.helpers] == ["ag_argon"]


async def test_a_seeded_agent_keeps_the_direct_write(
    db_session: AsyncSession,
    committing_sessionmaker: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(committing_sessionmaker)
    auth, origin = await _workspace(db_session, world)
    applied = _Applied()
    monkeypatch.setattr(edits.agent_changes, "apply_change", applied)

    await _set(world, auth, origin, "daimon", ["argon"])

    assert applied.changes == [], "a seeded agent has no canvas versions"
    assert await _stored(committing_sessionmaker, auth, "ag_daimon") == ["ag_argon"]


def test_with_helpers_keeps_a_listed_name_and_names_the_rest() -> None:
    config = edits.with_helpers(
        _config(HelperDoc(id="ag_a", name="Listed")), ["ag_a", "ag_b", "ag_c"], {"ag_b": "Roster"}
    )
    assert [(h.id, h.name) for h in config.helpers or []] == [
        ("ag_a", "Listed"),
        ("ag_b", "Roster"),
        ("ag_c", "ag_c"),
    ], "a listed name wins, then the roster's, then the id"

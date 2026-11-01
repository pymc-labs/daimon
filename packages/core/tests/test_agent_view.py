"""The web read model of an agent: what each viewer sees, then the shell against a fake MA."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core import agent_view
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.agent_view import AgentFacts, AgentViewDeps, AgentViewer, OwnMcpServer, PlaceDoc
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_tool_listing import ListedTool, McpToolLister
from daimon.core.scope import ChannelConfigRow, DeploymentDefault
from daimon.core.stores import (
    agent_drafts,
    agent_owners,
    mcp_tokens,
    platform_names,
    turn_outcomes,
    usage_events,
)
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_usage_event,
    make_web_person,
)
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

_OWNER = uuid.uuid4()
_SERVER = {"name": "docs", "type": "url", "url": "https://docs.example.com/mcp"}
_PLACES = (
    PlaceDoc(id="c1", kind="channel", platform="discord", name="general"),
    PlaceDoc(id="c2", kind="channel", platform="discord", name="board", confidential=True),
)


def _facts(**meta: str) -> AgentFacts:
    agent = ma_agent(
        id="ag_v",
        name="helper",
        tenant_id=uuid.uuid4(),
        metadata={"daimon_account": str(_OWNER), **meta},
        system="Be brief.",
        description="Answers questions",
        mcp_servers=[_SERVER],
    )
    return AgentFacts(agent=agent, owner_account_id=_OWNER, places=_PLACES)


def test_member_gets_no_prompt_places_or_server_urls() -> None:
    view = agent_view.build_agent_view(
        _facts(), AgentViewer(account_id=uuid.uuid4(), is_admin=False, may_edit=True)
    )
    assert view.system is None, "a member never reads the prompt"
    assert view.places is None, "a member never reads where the agent answers"
    assert view.mcp_servers == [], "a member sees no server names, as `get_agent` shows"
    assert view.can_edit is False and view.can_view_details is False, "a member only views"
    assert view.purpose == "Answers questions", "the purpose is public"


def test_member_still_sees_daimons_own_server_without_its_url() -> None:
    server = {"name": "daimon-mcp", "type": "url", "url": "https://daimon.example.com/mcp"}
    facts = AgentFacts(
        agent=ma_agent(
            id="ag_v", name="helper", tenant_id=uuid.uuid4(), mcp_servers=[server, _SERVER]
        ),
        owner_account_id=_OWNER,
    )
    view = agent_view.build_agent_view(
        facts, AgentViewer(account_id=uuid.uuid4(), is_admin=False, may_edit=False)
    )
    assert [(s.key, s.url, s.tools) for s in view.mcp_servers] == [("daimon-mcp", "", [])], (
        "Daimon's own server is a capability, shown without URL or tool rules"
    )


def test_listed_tools_are_the_truth_and_unrecorded_ones_default_to_allow() -> None:
    facts = AgentFacts(
        agent=_facts().agent,
        mcp_tools={
            "docs": [
                ListedTool(name="search", description="Find", read_only_hint=True),
                ListedTool(name="delete", description="Drop", destructive_hint=True),
            ]
        },
    )
    (server,) = agent_view.config_of(facts).mcp_servers
    assert [(t.name, t.description, t.rule) for t in server.tools] == [
        ("search", "Find", "allow"),
        ("delete", "Drop", "allow"),
    ], "every listed tool shows, under allow where no rule is recorded"
    assert [t.effect for t in server.tools] == ["read", "write"], "hints classify the effect"


def test_skill_description_comes_from_the_shipped_skills() -> None:
    agent = ma_agent(
        id="ag_s",
        name="helper",
        tenant_id=uuid.uuid4(),
        skills=[{"type": "custom", "skill_id": "skill_1", "version": "1"}],
    )
    facts = AgentFacts(
        agent=agent,
        skill_titles={"skill_1": "channel-tidy"},
        skill_descriptions={"channel-tidy": "Keep channels clean."},
    )
    (skill,) = agent_view.config_of(facts).skills
    assert (skill.title, skill.description) == ("channel-tidy", "Keep channels clean.")


_TENANT = uuid.uuid4()


def _places(
    *,
    rule: AgentRule | None = None,
    channels: tuple[ChannelConfigRow, ...] = (),
    own: tuple[str, ...] = (),
) -> list[PlaceDoc]:
    policy = TenantAccessPolicy(
        agent_rules={"helper": rule} if rule is not None else {},
        channel_rules={c: ChannelRule(readers="own", writers="own") for c in own},
    )
    return agent_view.places_of(
        names=("helper",),
        ma_agent_id="ag_v",
        policy=policy,
        tenant=None,
        channels=channels,
        default=DeploymentDefault(),
        dm_origins=[],
        platform="discord",
        channel_names={"c1": "general", "c2": "board"},
    )


def test_places_are_the_channels_it_is_default_of_plus_its_rule_and_web() -> None:
    default = ChannelConfigRow(tenant_id=_TENANT, channel_id="c1", agent_name="helper")
    places = _places(rule=AgentRule(runs_in=("c1", "c2", "web")), channels=(default,))
    assert [(p.id, p.default) for p in places] == [("c1", True), ("c2", None), ("web", None)], (
        "a default channel is marked, a rule-only channel is listed, web is named by the rule"
    )
    assert [p.name for p in places[:2]] == ["general", "board"], "channels carry their names"


def test_a_rule_that_leaves_web_out_drops_the_web_chat_and_no_rule_keeps_it() -> None:
    assert [p.id for p in _places(rule=AgentRule(runs_in=("c1",)))] == ["c1"]
    assert [p.id for p in _places()] == ["web"], "an unruled agent answers in web chat"
    assert [p.id for p in _places(rule=AgentRule(runs_in=()))] == [], "nowhere is no place"


def test_a_channel_kept_to_its_own_agents_is_confidential() -> None:
    places = _places(rule=AgentRule(runs_in=("c2",)), own=("c2",))
    assert [(p.id, p.confidential) for p in places] == [("c2", True)]


def test_roster_runs_in_lists_each_platform_once() -> None:
    places = [
        PlaceDoc(id="a", kind="channel", platform="slack", name="a"),
        PlaceDoc(id="b", kind="channel", platform="slack", name="b"),
        PlaceDoc(id="web", kind="web", platform="web", name="Web chat"),
    ]
    assert agent_view.runs_in_of(places) == ["slack", "web"]


def test_owner_sees_details_but_not_confidential_places() -> None:
    view = agent_view.build_agent_view(
        _facts(), AgentViewer(account_id=_OWNER, is_admin=False, may_edit=True)
    )
    assert view.system == "Be brief.", "the owner reads the prompt"
    assert view.mcp_servers[0].url == _SERVER["url"], "the owner reads server URLs"
    assert [p.id for p in view.places or []] == ["c1"], "confidential places stay admin-only"
    assert view.can_edit is True, "an owner with the edit capability edits"


def test_admin_sees_confidential_places() -> None:
    view = agent_view.build_agent_view(
        _facts(), AgentViewer(account_id=uuid.uuid4(), is_admin=True, may_edit=False)
    )
    assert [p.id for p in view.places or []] == ["c1", "c2"], "an admin sees every place"
    assert view.can_edit is False, "details without the edit capability stay read-only"


def test_seeded_agent_withholds_its_prompt_even_from_an_admin() -> None:
    view = agent_view.build_agent_view(
        _facts(daimon_managed="true"),
        AgentViewer(account_id=uuid.uuid4(), is_admin=True, may_edit=True),
    )
    assert view.system is None, "a seeded agent is changed by copying it"
    assert view.can_edit is False, "a seeded agent is never edited in place"


def test_hidden_setup_copy_hides_details_from_its_member_owner() -> None:
    view = agent_view.build_agent_view(
        _facts(daimon_setup_hidden="true"),
        AgentViewer(account_id=_OWNER, is_admin=False, may_edit=True),
    )
    assert view.system is None, "an inherited setup stays an admin's to read"


async def test_load_agent_view_reads_owner_and_refuses_another_tenants_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await agent_owners.record_creator(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_v"),
        account_id=account.id,
    )
    await db_session.commit()
    router = MARouter()
    stamp = {"daimon_account": str(account.id)}
    router.add_agent(
        ma_agent(id="ag_v", name="helper", tenant_id=tenant.id, metadata=stamp, system="Hi")
    )
    router.add_agent(ma_agent(id="ag_other", name="other", tenant_id=uuid.uuid4()))
    deps = AgentViewDeps(
        client=build_fake_anthropic(router.dispatch),
        session_factory=db_session_factory,
        markup=Decimal("2"),
    )
    viewer = AgentViewer(account_id=account.id, is_admin=False, may_edit=True)
    month = datetime(2026, 10, 1, tzinfo=UTC)

    view = await agent_view.load_agent_view(
        deps, tenant_id=tenant.id, ma_agent_id="ag_v", viewer=viewer, month_start=month
    )
    assert view is not None, "the tenant's own agent loads"
    assert view.owner.id != "", "the recorded creator is the owner"
    assert view.system == "Hi", "the owner reads the prompt"
    foreign = await agent_view.load_agent_view(
        deps, tenant_id=tenant.id, ma_agent_id="ag_other", viewer=viewer, month_start=month
    )
    assert foreign is None, "another tenant's agent reads as missing"


async def _view_of(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    viewer: AgentViewer,
    **metadata: str,
) -> agent_view.AgentView:
    """One agent named `Researcher`, stamped with `metadata`, as `viewer` reads it."""
    await db_session.commit()
    router = MARouter()
    router.add_agent(ma_agent(id="ag_v", name="Researcher", tenant_id=tenant_id, metadata=metadata))
    deps = AgentViewDeps(
        client=build_fake_anthropic(router.dispatch),
        session_factory=db_session_factory,
        markup=Decimal("2"),
    )
    view = await agent_view.load_agent_view(
        deps,
        tenant_id=tenant_id,
        ma_agent_id="ag_v",
        viewer=viewer,
        month_start=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert view is not None, "the tenant's own agent loads"
    return view


async def test_the_agent_is_named_as_chat_names_it_not_by_its_slug(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    viewer = AgentViewer(account_id=uuid.uuid4(), is_admin=False, may_edit=True)
    view = await _view_of(
        db_session, db_session_factory, tenant_id=tenant.id, viewer=viewer, daimon_name="researcher"
    )
    assert view.name == "Researcher", "list, canvas and chat read one name"


async def test_a_chat_only_owner_is_named_from_the_platform_name(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="d1", tenant=tenant, account=account
    )
    await platform_names.upsert_user_names(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        names={"d1": platform_names.KnownName(display_name="Maya")},
    )
    await agent_owners.record_creator(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_v"),
        account_id=account.id,
    )
    viewer = AgentViewer(account_id=uuid.uuid4(), is_admin=False, may_edit=True)
    view = await _view_of(db_session, db_session_factory, tenant_id=tenant.id, viewer=viewer)
    assert (view.owner.name, view.updated_by.name) == ("Maya", "Maya"), (
        "an owner with no web sign-in is the person their chat platform names, not Daimon"
    )


async def test_an_agent_with_no_recorded_owner_is_named_after_its_maker_without_giving_access(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    person = await make_web_person(db_session, tenant=tenant, display_name="Priya Raman")
    viewer = AgentViewer(account_id=person.account_id, is_admin=False, may_edit=True)
    view = await _view_of(
        db_session,
        db_session_factory,
        tenant_id=tenant.id,
        viewer=viewer,
        daimon_account=str(person.account_id),
    )
    assert view.owner.name == "Priya Raman", "the account the agent was made by is shown"
    assert not view.can_view_details, "naming a maker grants no owner rights"
    elsewhere = await _view_of(
        db_session,
        db_session_factory,
        tenant_id=tenant.id,
        viewer=viewer,
        daimon_account=str(uuid.uuid4()),
    )
    assert elsewhere.owner.name == "Daimon", "a maker nothing names stays Daimon"


async def test_month_usd_is_the_billed_spend_by_agent_and_last_used_is_the_latest_turn(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await make_usage_event(db_session, tenant=tenant, agent_id="ag_v", platform_user_id="u1")
    await make_usage_event(db_session, tenant=tenant, agent_id="ag_other", platform_user_id="u1")
    await db_session.commit()
    month = datetime(2000, 1, 1, tzinfo=UTC)
    raw = (
        await usage_events.costs_by_agent_in_tenant_since(
            db_session, tenant_id=tenant.id, since=month
        )
    )["ag_v"]
    assert raw > 0, "the event has a cost"
    router = MARouter()
    agent = ma_agent(id="ag_v", name="helper", tenant_id=tenant.id)
    router.add_agent(agent)
    deps = AgentViewDeps(
        client=build_fake_anthropic(router.dispatch),
        session_factory=db_session_factory,
        markup=Decimal("2"),
    )
    (facts,) = await agent_view.load_agent_facts(
        deps, tenant_id=tenant.id, agents=[agent], month_start=month
    )
    assert facts.month_usd == float(Decimal(str(raw)) * 2), "spend is the cost times the markup"
    assert facts.last_used_at is None, "no turn outcome means never used"
    assert [p.id for p in facts.places] == ["web"], "an unruled agent answers in web chat"
    assert await turn_outcomes.last_started_at(db_session, tenant.id, "ag_v") is None


async def test_load_agent_view_lists_the_servers_tools_for_the_agent_credential(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    router = MARouter()
    router.add_agent(ma_agent(id="ag_v", name="helper", tenant_id=tenant.id, mcp_servers=[_SERVER]))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host)
        return httpx.Response(200, json={"result": {"tools": [{"name": "search"}]}})

    deps = AgentViewDeps(
        client=build_fake_anthropic(router.dispatch),
        session_factory=db_session_factory,
        markup=Decimal(1),
        mcp_tools=McpToolLister(transport=httpx.MockTransport(handler)),
    )
    assert deps.fernet is None, "without a key store the recorded rules stand in"
    view = await agent_view.load_agent_view(
        deps,
        tenant_id=tenant.id,
        ma_agent_id="ag_v",
        viewer=AgentViewer(account_id=uuid.uuid4(), is_admin=True, may_edit=True),
        month_start=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert view is not None and view.mcp_servers[0].tools == [], "nothing is recorded or listed"
    assert requests == [], "no credential, no call"


def test_a_server_not_listed_or_not_signed_in_says_so_instead_of_no_tools() -> None:
    sign_in = {"name": "crm", "type": "url", "url": "https://crm.example.com/mcp"}
    facts = AgentFacts(
        agent=ma_agent(
            id="ag_v", name="helper", tenant_id=uuid.uuid4(), mcp_servers=[_SERVER, sign_in]
        ),
        owner_account_id=_OWNER,
        tools_read=True,
        mcp_tools={"crm": [ListedTool(name="find")]},
        sign_ins={"docs": False, "crm": True},
    )
    view = agent_view.build_agent_view(
        facts, AgentViewer(account_id=_OWNER, is_admin=False, may_edit=True)
    )
    shown = {s.key: (s.listed, s.credential, len(s.tools)) for s in view.mcp_servers}
    assert shown == {"docs": (False, "missing", 0), "crm": (True, "connected", 1)}, (
        "an unread list is marked not listed; sign-in state is the viewer's own"
    )


_OWN = "https://daimon.example.com/mcp"


def _answer(request: httpx.Request) -> httpx.Response:
    """An MCP server offering one tool, `search`."""
    body = json.loads(request.content)
    if "id" not in body:
        return httpx.Response(202)
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "search"}]}}
    )


async def _own_server_view(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    handler: httpx.MockTransport,
    *,
    is_admin: bool,
) -> tuple[agent_view.AgentView | None, uuid.UUID]:
    """The view of an agent on the deployment's own server, by a fresh viewer."""
    tenant = await make_tenant(db_session)
    viewer = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    router = MARouter()
    server = {"name": "daimon-mcp", "type": "url", "url": _OWN}
    router.add_agent(ma_agent(id="ag_v", name="helper", tenant_id=tenant.id, mcp_servers=[server]))
    deps = AgentViewDeps(
        client=build_fake_anthropic(router.dispatch),
        session_factory=factory,
        markup=Decimal(1),
        mcp_tools=McpToolLister(transport=handler),
        own_server=OwnMcpServer(
            url=_OWN, secret=b"s" * 32, now=lambda: datetime(2026, 10, 2, tzinfo=UTC)
        ),
    )
    view = await agent_view.load_agent_view(
        deps,
        tenant_id=tenant.id,
        ma_agent_id="ag_v",
        viewer=AgentViewer(account_id=viewer.id, is_admin=is_admin, may_edit=True),
        month_start=datetime(2026, 10, 1, tzinfo=UTC),
    )
    return view, viewer.id


async def test_the_deployments_own_server_is_listed_with_a_token_minted_for_the_viewer(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return _answer(request)

    view, account = await _own_server_view(
        db_session, db_session_factory, httpx.MockTransport(handler), is_admin=True
    )
    assert view is not None, "the agent exists"
    (server,) = view.mcp_servers
    assert ([t.name for t in server.tools], server.listed) == (["search"], True), (
        "the viewer sees the server's real tools"
    )
    assert seen and all(h.startswith("Bearer ") for h in seen), "the server was asked"
    tokens = [
        t
        for t in await mcp_tokens.list_mcp_tokens(
            db_session, now=datetime(2026, 10, 2, tzinfo=UTC), include_inactive=True
        )
        if t.account_id == account
    ]
    assert len(tokens) == 1 and tokens[0].revoked_at is not None, (
        "the viewer's token is revoked once the list is read"
    )


async def test_an_unreadable_own_server_shows_not_listed_and_logs_why(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with capture_logs() as logs:
        view, _account = await _own_server_view(
            db_session, db_session_factory, httpx.MockTransport(handler), is_admin=True
        )
    assert view is not None and view.mcp_servers[0].listed is False, "no list, said so"
    (event_,) = [e for e in logs if e["event"] == "agent_view.tools_unlisted"]
    assert (event_["server"], event_["error"]) == ("daimon-mcp", "McpStatusError"), (
        "the failure names the server and the error type"
    )


async def test_a_viewer_without_details_gets_no_token_and_no_list(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _answer(request)

    view, _account = await _own_server_view(
        db_session, db_session_factory, httpx.MockTransport(handler), is_admin=False
    )
    assert view is not None and view.mcp_servers[0].listed is False, "a member sees not listed"
    assert seen == [], "nothing is minted or asked for a viewer who can't see details"


async def _fact_statements(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    agents: list[BetaManagedAgentsAgent],
) -> tuple[int, list[AgentFacts]]:
    """The SQL statements `load_agent_facts` runs for `agents`, and what it returns."""
    deps = AgentViewDeps(
        client=build_fake_anthropic(MARouter().dispatch),
        session_factory=factory,
        markup=Decimal(1),
    )
    statements: list[str] = []

    def capture(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        statements.append(statement)

    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    event.listen(bind.sync_engine, "before_cursor_execute", capture)
    try:
        facts = await agent_view.load_agent_facts(
            deps, tenant_id=tenant_id, agents=agents, month_start=datetime(2000, 1, 1, tzinfo=UTC)
        )
    finally:
        event.remove(bind.sync_engine, "before_cursor_execute", capture)
    return len(statements), facts


async def test_facts_for_many_agents_cost_no_more_queries_than_for_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agents = [ma_agent(id=f"ag_{n}", name=f"helper{n}", tenant_id=tenant.id) for n in range(6)]
    await agent_owners.record_creator(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_3"),
        account_id=account.id,
    )
    await db_session.commit()

    one, _ = await _fact_statements(db_session, db_session_factory, tenant.id, agents[3:4])
    many, facts = await _fact_statements(db_session, db_session_factory, tenant.id, agents)

    assert many == one, f"one read per kind of fact: {one} statements for 1 agent, {many} for 6"
    assert [f.owner_account_id for f in facts] == [None, None, None, account.id, None, None], (
        "each agent keeps its own owner"
    )


async def test_latest_version_by_agent_names_each_agents_newest_version(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    first, second = (
        await make_account(db_session, tenant=tenant),
        await make_account(db_session, tenant=tenant),
    )
    for agent_id, version, account in (("ag_a", 1, first), ("ag_a", 2, second), ("ag_b", 1, first)):
        await agent_drafts.record_version(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            version=version,
            account_id=account.id,
            note=None,
            changes=[],
            config={},
        )

    latest = await agent_drafts.latest_version_by_agent(
        db_session, tenant_id=tenant.id, agent_ids=["ag_a", "ag_b", "ag_none"]
    )

    assert {a: (v.version, v.account_id) for a, v in latest.items()} == {
        "ag_a": (2, second.id),
        "ag_b": (1, first.id),
    }, "the newest version of each agent, and nothing for one with no record"

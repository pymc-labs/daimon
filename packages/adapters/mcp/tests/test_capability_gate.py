"""A capability feature Off refuses its tools at the server, on every path.

Drives the real app: the live registry, the real identity middleware and `call_tool` proxy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.capability_gate import CapabilityGateMiddleware, caller_places
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.tools._web_url import tenant_links
from daimon.core.agent_chat_requester import UNREAD, record_driver
from daimon.core.capability_tools import CAPABILITY_TOOLS
from daimon.core.config import (
    ComputeSettings,
    NotebookSettings,
    ReportHostSettings,
    SelfKnowledgeSettings,
    Settings,
    WebSettings,
)
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.tool_gate import SELF_KNOWLEDGE_TOOLS, STOP_TOOLS, server_gate_key
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.repo_memory_tools import REPO_MEMORY_TOOLS
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import FeatureTarget, Role
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.session_settle_state import (
    SessionProvenance,
    erase_agent_chat_provenance_for_account,
)
from daimon.core.stores.turn_origins import create_origin
from daimon.core.tool_safety import RENAMED_DAIMON_TOOLS
from daimon.testing.factories import make_account, make_routine, make_tenant
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import auth_identity
from .test_tool_schema_snapshot import (
    _fully_configured_settings,  # pyright: ignore[reportPrivateUsage]
)

OFF = "is turned off here"
UNSET = "isn't set up here yet"


def _set_up() -> Settings:
    """Every capability's setup present, so only a feature Off refuses."""
    return _fully_configured_settings().model_copy(
        update={
            "report_host": ReportHostSettings(
                host_url=HttpUrl("http://report-host:8002"), admin_secret=SecretStr("r" * 32)
            ),
            "compute": ComputeSettings(
                enabled=True,
                deployment_id="d",
                cloud_run_project="p",
                cloud_run_service_account="s@p.iam.gserviceaccount.com",
            ),
        }
    )


async def _app(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings | None = None
) -> FastMCP:
    app = create_mcp_app(
        settings=settings or _set_up(),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    return app.state.mcp


async def _tenant(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessionmaker() as session, session.begin():
        return (await make_tenant(session)).id


async def _set(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    key: str,
    value: str,
    target: FeatureTarget | None = None,
) -> None:
    async with sessionmaker() as session, session.begin():
        await set_feature_value(
            session,
            tenant_id=tenant_id,
            target=target or FeatureTarget("workspace"),
            key=key,
            value=value,
            set_by=None,
            surface="chat",
        )


def _as(monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, **claims: object) -> None:
    token = AccessToken(
        token="t",
        client_id="c",
        scopes=[],
        claims={"sub": str(uuid.uuid4()), "role": "user", "tenant_id": str(tenant_id), **claims},
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.mcp_identity.get_access_token", lambda: token
    )


async def _error(client: Client[FastMCPTransport], tool: str, arguments: dict[str, object]) -> str:
    try:
        await client.call_tool(tool, arguments)
    except ToolError as exc:
        return str(exc)
    return ""


async def test_every_capability_tool_when_its_feature_is_off_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    registered = {t.name for t in await mcp.local_provider.list_tools()}
    gated = {tool for tools in CAPABILITY_TOOLS.values() for tool in tools}
    assert gated <= registered, f"capability tools missing from the registry: {gated - registered}"
    tenant_id = await _tenant(sessionmaker)
    for key in CAPABILITY_TOOLS:
        await _set(sessionmaker, tenant_id, key, "off")
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        for key, tools in sorted(CAPABILITY_TOOLS.items()):
            for tool in sorted(tools - STOP_TOOLS):
                message = await _error(client, tool, {})
                assert OFF in message, f"{tool} ran with {key} Off: {message!r}"
        for tool in sorted(STOP_TOOLS):
            message = await _error(client, tool, {})
            assert OFF not in message, f"{tool} must stay callable to stop running work"


@pytest.mark.parametrize("proxied", [False, True])
async def test_a_call_to_a_renamed_tool_says_its_new_name(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, proxied: bool
) -> None:
    """A conversation from before the rename may still ask for `fly_*`. No old name
    runs anything: the caller is told where the tool went, in plain words."""
    mcp = await _app(sessionmaker)
    registered = {t.name for t in await mcp.local_provider.list_tools()}
    assert set(RENAMED_DAIMON_TOOLS.values()) <= registered, "every new name is a real tool"
    assert not set(RENAMED_DAIMON_TOOLS) & registered, "no old name is kept as a second tool"
    _as(monkeypatch, await _tenant(sessionmaker), platform="cli")
    async with Client(mcp) as client:
        for old_name, new_name in sorted(RENAMED_DAIMON_TOOLS.items()):
            message = (
                await _error(client, "call_tool", {"name": old_name, "arguments": {}})
                if proxied
                else await _error(client, old_name, {})
            )
            assert f"{old_name} is now called {new_name}. Call {new_name} instead." in message
            assert "Unknown tool" not in message


async def test_repo_memory_tools_when_memory_is_off_for_the_workspace_are_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    await _set(sessionmaker, tenant_id, "repo_memory", "off")
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        for tool in sorted(REPO_MEMORY_TOOLS):
            message = await _error(client, tool, {})
            assert OFF in message, f"{tool} ran with repo_memory Off: {message!r}"


async def test_a_capability_tool_when_its_feature_is_on_is_not_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        message = await _error(client, "list_routines", {})
    assert OFF not in message, f"routines is On by default: {message!r}"


async def test_a_proxied_call_when_the_feature_is_off_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    await _set(sessionmaker, tenant_id, "routines", "off")
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        message = await _error(client, "call_tool", {"name": "list_routines", "arguments": {}})
    assert OFF in message, f"the call_tool proxy reached list_routines: {message!r}"
    assert message.endswith("Ask an admin or an owner to turn it on."), "an owner may turn it on"


async def test_an_agent_key_when_its_channel_turns_the_feature_off_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    await _set(sessionmaker, tenant_id, "github", "off", FeatureTarget("channel", channel_id="c1"))
    agent_id = str(uuid.uuid4())
    # Self-hosted: the key's agent is looked up in the records, where it has none.
    key = {"platform": "discord", "agent_id": agent_id, "anthropic_disabled": True}
    _as(monkeypatch, tenant_id, **key, bound_channel_id="c1")
    async with Client(mcp) as client:
        bound = await _error(client, "github_connect", {})
        _as(monkeypatch, tenant_id, **key, bound_channel_id="c2")
        elsewhere = await _error(client, "github_connect", {})
    assert OFF in bound, f"the key bound to the Off channel reached github_connect: {bound!r}"
    assert OFF not in elsewhere, f"a key in another channel was refused: {elsewhere!r}"


async def test_an_agent_key_whose_agent_has_the_web_app_off_is_kept_from_the_web_app(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The key's derived uuid maps to its agent, whose agent-tier Off then applies."""
    tenant_id = await _tenant(sessionmaker)
    web = WebSettings(public_url="https://app.example.com/")  # type: ignore[arg-type]
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=_set_up().model_copy(update={"web": web}),
        deployment_default=DeploymentDefault(),
    )
    derived = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agent_k")
    runtime.resolver_cache.derived_agent_ids[(tenant_id, derived)] = "agent_k"
    auth = auth_identity(
        uuid.uuid4(),
        tenant_id,
        is_admin=False,
        is_platform_admin=False,
        platform="discord",
        agent_id=derived,
    )
    assert await tenant_links(runtime, auth) is not None, "On: the caller gets links"
    await _set(sessionmaker, tenant_id, "web_awareness", "off", FeatureTarget("agent", "agent_k"))
    assert await tenant_links(runtime, auth) is None, "Off at the key's agent: no web_url"
    web_tool = await _gate(sessionmaker, auth, "list_web_invites", runtime)
    lookup = await _gate(sessionmaker, auth, "list_web_people", runtime)
    assert OFF in web_tool, f"the key's agent has the web app Off but reached it: {web_tool!r}"
    assert OFF not in lookup, f"allowances and owners need the people lookup: {lookup!r}"


async def test_the_tool_list_when_a_feature_is_off_drops_its_tools_for_an_external_client(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    _as(monkeypatch, tenant_id, platform="cli")
    notebooks = CAPABILITY_TOOLS["notebooks"]
    async with Client(mcp) as client:
        before = {t.name for t in await client.list_tools()}
        await _set(sessionmaker, tenant_id, "notebooks", "off")
        after = {t.name for t in await client.list_tools()}
    assert before & notebooks, "the notebook tools are listed while notebooks is On"
    assert not after & notebooks, f"Off notebook tools listed: {after & notebooks}"
    assert "get_long_job" in after, "a stop tool stays listed though long jobs is Off"


async def test_cloud_computers_stay_off_until_a_workspace_turns_them_on(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off by default even once the operator set compute up: nothing is offered or runs."""
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    _as(monkeypatch, tenant_id, platform="cli")
    launch = {"template": "mmm", "origin_context_id": str(uuid.uuid4())}
    # The ones listed by name; the rest are found through search.
    compute = frozenset({"compute_launch_session", "compute_extend_session"})
    async with Client(mcp) as client:
        before = {t.name for t in await client.list_tools()}
        refused = await _error(client, "compute_launch_session", launch)
        await _set(sessionmaker, tenant_id, "compute", "on")
        after = {t.name for t in await client.list_tools()}
        allowed = await _error(client, "compute_launch_session", launch)
        # Turned Off again with a machine running: list and stop stay, the rest is refused.
        await _set(sessionmaker, tenant_id, "compute", "off")
        on_machine = {
            "session_id": str(uuid.uuid4()),
            "origin_context_id": launch["origin_context_id"],
        }
        run = await _error(client, "compute_run_bash", {**on_machine, "command": "id"})
        kept = [
            await _error(client, "compute_list_sessions", {"origin_context_id": str(uuid.uuid4())}),
            await _error(client, "compute_stop_session", on_machine),
        ]
    assert not before & compute, f"cloud computer tools listed while Off: {before & compute}"
    assert refused == (
        "Cloud computers is turned off here, so compute_launch_session can't run. "
        "Nothing was done. Ask a server admin to turn it on."
    ), "only a server admin can turn it on, so the refusal names one"
    assert compute <= after, "a workspace On offers them"
    assert OFF not in allowed, f"the gate let the call through: {allowed!r}"
    assert OFF in run, f"a command ran with cloud computers Off: {run!r}"
    assert not [message for message in kept if OFF in message], "list and stop stay callable"


async def test_the_tool_list_for_a_chat_credential_is_left_whole(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native turn checks its live catalogue against its allowlist: the list must not shrink."""
    mcp = await _app(sessionmaker)
    tenant_id = await _tenant(sessionmaker)
    _as(monkeypatch, tenant_id, platform="discord", chat_agent_id=str(uuid.uuid4()))
    async with Client(mcp) as client:
        before = {t.name for t in await client.list_tools()}
        await _set(sessionmaker, tenant_id, "routines", "off")
        after = {t.name for t in await client.list_tools()}
    assert before == after, f"a chat credential's list changed: {before ^ after}"


async def test_notebook_tools_when_the_server_lacks_a_host_leave_a_chat_credentials_list(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker can't judge the notebook host, so the server drops the tools from a chat
    credential's whole list too, and refuses a call as not set up rather than turned off."""
    unset = _set_up().model_copy(update={"notebook": NotebookSettings()})
    mcp = await _app(sessionmaker, unset)
    tenant_id = await _tenant(sessionmaker)
    notebooks = CAPABILITY_TOOLS["notebooks"]
    _as(monkeypatch, tenant_id, platform="discord", chat_agent_id=str(uuid.uuid4()))
    async with Client(mcp) as client:
        listed = {t.name for t in await client.list_tools()}
        message = await _error(client, "list_notebooks", {})
    assert not listed & notebooks, f"notebook tools listed without a host: {listed & notebooks}"
    assert "publish_report" in listed, "a tool that is set up stays listed"
    assert f"{FEATURES['notebooks'].name} {UNSET}" in message, f"wrong refusal: {message!r}"
    assert OFF not in message, f"a missing host is not a feature turned off: {message!r}"


def test_server_gate_key_covers_every_gated_tool_but_the_stop_tools() -> None:
    for key, tools in {**CAPABILITY_TOOLS, "repo_memory": REPO_MEMORY_TOOLS}.items():
        for tool in tools:
            expected = None if tool in STOP_TOOLS else key
            assert server_gate_key(tool) == expected, f"{tool} should be gated by {expected}"
    assert server_gate_key("search_tools") is None, "an ungated tool needs no feature read"


async def _two_running_origins(
    sessionmaker: async_sessionmaker[AsyncSession], channels: tuple[str, str]
) -> tuple[uuid.UUID, AuthIdentity]:
    """A session with one running turn in each of `channels` (same thread name)."""
    ma_agent = "agent_gate"
    now = datetime.now(UTC)
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        for channel in channels:
            await create_origin(
                session,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                parent_channel_id=channel,
                thread_id=channel,
                responder_ma_agent_id=ma_agent,
                responder_name="daimon",
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                expires_at=now + timedelta(minutes=5),
                now=now,
                sender_foreign=False,
                shared_channel=False,
                chat_session_id="session-1",
            )
    auth = auth_identity(
        account.id,
        tenant.id,
        role=Role.USER,
        platform="discord",
        chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent),
        chat_session_id="session-1",
    )
    return tenant.id, auth


class _Context:
    """The part of a middleware context the gate reads."""

    def __init__(self, auth: AuthIdentity, tool: str) -> None:
        self.message = SimpleNamespace(name=tool, arguments={})
        self.fastmcp_context = SimpleNamespace(get_state=self._state)
        self._auth = auth

    async def _state(self, _name: str) -> AuthIdentity:
        return self._auth


async def _gate(
    sessionmaker: async_sessionmaker[AsyncSession],
    auth: AuthIdentity,
    tool: str,
    runtime: McpRuntime | None = None,
) -> str:
    gate = CapabilityGateMiddleware(
        runtime
        or McpRuntime(
            session_factory=sessionmaker,
            client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
            settings=_set_up(),
            deployment_default=DeploymentDefault(),
        )
    )

    async def ran(_context: object) -> str:
        return "ran"

    try:
        return str(await gate.on_call_tool(_Context(auth, tool), ran))  # type: ignore[arg-type]
    except ToolError as exc:
        return str(exc)


async def test_a_session_with_two_running_turns_is_refused_when_either_place_has_it_off(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Turns in different channels cannot be told apart, so a channel Off in either refuses."""
    tenant_id, auth = await _two_running_origins(sessionmaker, ("chan-a", "chan-b"))
    await _set(
        sessionmaker, tenant_id, "routines", "off", FeatureTarget("channel", channel_id="chan-b")
    )
    refused = await _gate(sessionmaker, auth, "list_routines")
    assert OFF in refused, f"one turn's channel is Off, yet the call ran: {refused!r}"
    other = await _gate(sessionmaker, auth, "github_connect")
    assert OFF not in other, f"a feature that is On in both places was refused: {other!r}"


async def test_a_session_with_two_turns_in_one_place_reads_that_place(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, auth = await _two_running_origins(sessionmaker, ("chan-a", "chan-a"))
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=_set_up(),
        deployment_default=DeploymentDefault(),
    )
    places = await caller_places(runtime, auth, None)
    assert [(p.channel_id, p.thread_id) for p in places] == [("chan-a", None)], (
        f"two turns in one channel and thread are one place: {places}"
    )
    await _set(
        sessionmaker, tenant_id, "routines", "off", FeatureTarget("channel", channel_id="chan-a")
    )
    assert OFF in await _gate(sessionmaker, auth, "list_routines"), "that place's Off holds"


async def test_self_knowledge_tools_follow_the_feature_for_every_caller(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Off: hidden and refused, the `call_tool` proxy too; On (the default), they read this
    release's docs, web pages left out where the web app is Off. A chat credential's list,
    the catalogue a pinned provider install keeps exactly, never changes with it."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/slack.md").write_text("# Slack\n\nInstall the app.\n")
    (tmp_path / "docs/web.md").write_text("---\nknowledge: web\n---\n# Web\n")
    roots = SelfKnowledgeSettings(docs_root=tmp_path / "docs", source_root=tmp_path)
    mcp = await _app(sessionmaker, _set_up().model_copy(update={"self_knowledge": roots}))
    tenant_id = await _tenant(sessionmaker)
    page, search = {"page": "slack.md"}, {"query": "Daimon's own documentation"}
    await _set(sessionmaker, tenant_id, "self_knowledge", "off")
    _as(monkeypatch, tenant_id, platform="discord", chat_agent_id=str(uuid.uuid4()))
    async with Client(mcp) as client:
        chat_before = {t.name for t in await client.list_tools()}
        chat_found_off = str(await client.call_tool("search_tools", search))
    _as(monkeypatch, tenant_id, platform="cli")
    async with Client(mcp) as client:
        hidden = str(await client.call_tool("search_tools", search))
        refused = await _error(client, "read_daimon_docs", page)
        proxied = await _error(client, "call_tool", {"name": "read_daimon_docs", "arguments": page})
        await _set(sessionmaker, tenant_id, "self_knowledge", "on")
        shown = str(await client.call_tool("search_tools", search))
        read = await client.call_tool("call_tool", {"name": "read_daimon_docs", "arguments": page})
        web_on = await _error(client, "read_daimon_docs", {"page": "web.md"})
        await _set(sessionmaker, tenant_id, "web_awareness", "off")
        web_off = await _error(client, "read_daimon_docs", {"page": "web.md"})
    _as(monkeypatch, tenant_id, platform="discord", chat_agent_id=str(uuid.uuid4()))
    async with Client(mcp) as client:
        chat_after = {t.name for t in await client.list_tools()}
        chat_found_on = str(await client.call_tool("search_tools", search))
    assert "read_daimon_docs" not in hidden, "Off: search does not find it"
    assert OFF in refused and OFF in proxied, f"Off refuses, the proxy too: {proxied!r}"
    assert "read_daimon_docs" in shown, "On, search finds it"
    assert "Install the app." in str(read.content), f"On reads the page: {read.content!r}"
    assert not web_on, f"a web page reads where the web app is On: {web_on!r}"
    assert "no page" in web_off, f"a web page is hidden where the web app is Off: {web_off!r}"
    assert chat_before == chat_after, "a chat credential's list is left whole"
    assert "read_daimon_docs" not in chat_found_off, "Off: a chat credential's search skips it"
    assert "read_daimon_docs" in chat_found_on, "On: and finds it"
    assert not chat_after & SELF_KNOWLEDGE_TOOLS, "behind search: a pinned catalogue keeps"


_UNREADABLE = "Couldn't check whether this tool is turned on here. Nothing was done. Try again."
#: The grant an agent-chat session's Daimon credential signs (`chat_session_id`).
_GRANT = "agent-chat-grant"


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, *agents: str
) -> McpRuntime:
    """A runtime that already knows `agents` by their derived uuids, as after a first lookup."""
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=_set_up(),
        deployment_default=DeploymentDefault(),
    )
    for agent in agents:
        derived = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent)
        runtime.resolver_cache.derived_agent_ids[(tenant_id, derived)] = agent
    return runtime


async def _sent(
    sessionmaker: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID | None,
    *,
    grant: str | None = _GRANT,
    channel: str | None = None,
    session_id: str = "sesn_chat",
    resumed_by_person: bool = False,
) -> None:
    """A send on an agent-chat session, or a signed-in person's resume of it, recorded as
    the send paths record it."""
    await record_driver(
        sessionmaker,
        managed_session_id=session_id,
        signed_in_person=resumed_by_person,
        session_status="idle",
        earlier=UNREAD,
        provenance=SessionProvenance(grant=grant, channel_id=channel, account_id=account_id),
        resuming=resumed_by_person,
    )


def _agent_chat(
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent: str,
    *,
    grant: str = _GRANT,
    **fields: object,
) -> AuthIdentity:
    """The credential of an agent-chat session's own agent: a chat execution with no turn
    origin, no routine and no agent key, signing the session's grant."""
    return auth_identity(
        account_id,
        tenant_id,
        role=Role.USER,
        platform="discord",
        is_chat_execution=True,
        chat_agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent),
        chat_session_id=grant,
        **fields,
    )


async def _tenant_and_account(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session)
        return tenant.id, (await make_account(session, tenant=tenant)).id


async def test_an_agent_chat_session_reads_its_own_agents_tier(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Its session's tools were chosen with its agent (`agent_chat.py`), so the gate reads that
    agent's tier too. Cloud computers is a capability: an Off set above is a ceiling; below
    the default or an On, the agent's own value decides."""
    tenant_id, account_id = await _tenant_and_account(sessionmaker)
    await _sent(sessionmaker, account_id)
    runtime = _runtime(sessionmaker, tenant_id, "agent_chat")
    auth = _agent_chat(account_id, tenant_id, "agent_chat")
    agent = FeatureTarget("agent", "agent_chat")
    await _set(sessionmaker, tenant_id, "compute", "on", agent)
    agent_on = await _gate(sessionmaker, auth, "compute_launch_session", runtime)
    await _set(sessionmaker, tenant_id, "compute", "off")
    workspace_off = await _gate(sessionmaker, auth, "compute_launch_session", runtime)
    await _set(sessionmaker, tenant_id, "compute", "on")
    await _set(sessionmaker, tenant_id, "compute", "off", agent)
    agent_off = await _gate(sessionmaker, auth, "compute_launch_session", runtime)
    assert agent_on == "ran", f"On for the agent, default for the workspace: {agent_on!r}"
    assert OFF in workspace_off, (
        f"an Off above is a ceiling the agent can't lift: {workspace_off!r}"
    )
    assert OFF in agent_off, f"Off for the agent, On for the workspace, yet it ran: {agent_off!r}"


async def test_an_agent_chat_session_keeps_the_channel_its_tools_were_chosen_for(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A session a channel-bound key started was given that channel's tools. A channel Off set
    after the session was made still refuses, though its agent has the feature On."""
    tenant_id, account_id = await _tenant_and_account(sessionmaker)
    await _sent(sessionmaker, account_id, channel="chan-k")
    runtime = _runtime(sessionmaker, tenant_id, "agent_chat")
    auth = _agent_chat(account_id, tenant_id, "agent_chat")
    await _set(sessionmaker, tenant_id, "compute", "on", FeatureTarget("agent", "agent_chat"))
    before = await _gate(sessionmaker, auth, "compute_launch_session", runtime)
    await _set(
        sessionmaker, tenant_id, "compute", "off", FeatureTarget("channel", channel_id="chan-k")
    )
    after = await _gate(sessionmaker, auth, "compute_launch_session", runtime)
    assert before == "ran", f"On for the agent, nothing set for the channel: {before!r}"
    assert OFF in after, f"Off for the session's channel, yet it ran: {after!r}"


async def test_a_chat_thread_after_its_turn_ended_keeps_the_workspace(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A chat thread's credential outlives its turn's origin. With no agent-chat session
    recorded for its grant, it reads the workspace as before: an agent On lets nothing
    through that the workspace's default Off refuses."""
    tenant_id, account_id = await _tenant_and_account(sessionmaker)
    runtime = _runtime(sessionmaker, tenant_id, "agent_turn")
    ended = _agent_chat(account_id, tenant_id, "agent_turn", grant="turn-session")
    await _set(sessionmaker, tenant_id, "compute", "on", FeatureTarget("agent", "agent_turn"))
    refused = await _gate(sessionmaker, ended, "compute_launch_session", runtime)
    assert await caller_places(runtime, ended, None) == [FeaturePlace(tenant_id, "discord")]
    assert OFF in refused, f"the ended turn's credential gained its agent's On: {refused!r}"


async def test_an_agent_chat_session_whose_agent_cant_be_found_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the agent out would drop an agent-tier Off without a word, so the call is refused,
    as any place the gate can't read is."""
    tenant_id, account_id = await _tenant_and_account(sessionmaker)
    await _sent(sessionmaker, account_id)
    await _set(sessionmaker, tenant_id, "compute", "on")
    # Self-hosted: the agent is looked up in the records, where it has none.
    missing = _agent_chat(account_id, tenant_id, "agent_gone", anthropic_disabled=True)
    gone = await _gate(sessionmaker, missing, "compute_launch_session")

    async def unreachable(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the provider can't be reached")

    monkeypatch.setattr(
        "daimon.adapters.mcp.middleware.capability_gate.find_agent_by_derived_uuid_cached",
        unreachable,
    )
    failed = await _gate(
        sessionmaker, _agent_chat(account_id, tenant_id, "agent_x"), "list_routines"
    )
    assert gone == _UNREADABLE, f"an agent that isn't found must refuse: {gone!r}"
    assert failed == _UNREADABLE, f"a lookup that fails must refuse: {failed!r}"


_CALLERS = (
    "plain client",
    "agent key",
    "agent key not found",
    "hub",
    "chat turn",
    "chat turn naming its origin",
    "chat thread after its turn ended",
    "routine",
    "routine not found",
    "agent chat",
    "agent chat resumed by a signed-in person",
    "agent chat without a platform",
    "agent chat with no send recorded",
    "agent chat recorded before its grant was",
    "agent chat of another account",
    "agent chat whose account was erased",
)


@pytest.mark.parametrize("caller", _CALLERS)
async def test_each_caller_kind_reads_the_places_it_is_known_by(
    sessionmaker: async_sessionmaker[AsyncSession], caller: str
) -> None:
    """Only a recorded agent-chat session gains its channel and agent; every other kind keeps
    the places it had, a session no send of this release recorded too."""
    now = datetime.now(UTC)
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        other = await make_account(session, tenant=tenant)
        routine = await make_routine(
            session, tenant=tenant, agent_id="agent_routine", channel_id="chan-r"
        )
        origins = {}
        for grant, expires in (("turn-session", 5), ("ended-session", -5)):
            origins[grant] = await create_origin(
                session,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                parent_channel_id="chan-t",
                thread_id="thread-t",
                responder_ma_agent_id="agent_turn",
                responder_name="daimon",
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                expires_at=now + timedelta(minutes=expires),
                now=now,
                sender_foreign=False,
                shared_channel=False,
                chat_session_id=grant,
            )
    t = tenant.id
    recorded = {
        "agent chat": (account.id, _GRANT),
        "agent chat resumed by a signed-in person": (account.id, _GRANT),
        "agent chat without a platform": (account.id, _GRANT),
        "agent chat recorded before its grant was": (account.id, None),
        "agent chat of another account": (other.id, _GRANT),
        "agent chat whose account was erased": (account.id, _GRANT),
    }
    if caller in recorded:
        owner, grant = recorded[caller]
        resumed = caller == "agent chat resumed by a signed-in person"
        await _sent(sessionmaker, owner, grant=grant, channel="chan-k", resumed_by_person=resumed)
    if caller == "agent chat whose account was erased":
        # What the account's erasure runs (`erasure.ERASABLE`).
        async with sessionmaker.begin() as db:
            await erase_agent_chat_provenance_for_account(db, account_id=account.id)
    agents = ("agent_key", "agent_hub", "agent_turn", "agent_routine", "agent_chat")
    runtime = _runtime(sessionmaker, t, *agents)

    def derived(agent: str) -> uuid.UUID:
        return derive_agent_uuid(tenant_id=t, ma_agent_id=agent)

    def who(**fields: object) -> AuthIdentity:
        return auth_identity(account.id, t, role=Role.USER, is_admin=False, **fields)

    def turn(agent: str, **fields: object) -> AuthIdentity:
        return who(
            platform="discord", is_chat_execution=True, chat_agent_id=derived(agent), **fields
        )

    in_turn = turn("agent_turn", chat_session_id="turn-session")
    turn_place = FeaturePlace(t, "discord", "chan-t", "thread-t", agent_id="agent_turn")
    workspace = FeaturePlace(t, "discord")
    chat = turn("agent_chat", chat_session_id=_GRANT)
    chat_place = FeaturePlace(t, "discord", "chan-k", agent_id="agent_chat")
    known: dict[str, tuple[AuthIdentity, str | None, FeaturePlace]] = {
        "plain client": (who(platform="cli"), None, FeaturePlace(t, "cli")),
        "agent key": (
            who(platform="discord", agent_id=derived("agent_key"), bound_channel_id="chan-k"),
            None,
            FeaturePlace(t, "discord", "chan-k", agent_id="agent_key"),
        ),
        # The key's tier is extra: a failed or empty lookup leaves it out, never refusing.
        "agent key not found": (
            who(platform="discord", agent_id=uuid.uuid4(), anthropic_disabled=True),
            None,
            workspace,
        ),
        # As `hub._auth_for` builds it: the signed-in person with the agent they named.
        "hub": (
            who(platform="discord", agent_id=derived("agent_hub"), platform_user_id="u-1"),
            None,
            FeaturePlace(t, "discord", agent_id="agent_hub"),
        ),
        "chat turn": (in_turn, None, turn_place),
        "chat turn naming its origin": (in_turn, str(origins["turn-session"].id), turn_place),
        "chat thread after its turn ended": (
            turn("agent_turn", chat_session_id="ended-session"),
            str(origins["ended-session"].id),
            workspace,
        ),
        "routine": (
            turn("agent_routine", routine_id=routine.id, execution_id=uuid.uuid4()),
            None,
            FeaturePlace(t, "discord", "chan-r", agent_id="agent_routine"),
        ),
        "routine not found": (turn("agent_routine", routine_id=uuid.uuid4()), None, workspace),
        "agent chat": (chat, None, chat_place),
        # A session made before this release is found once a person resumes it, too.
        "agent chat resumed by a signed-in person": (chat, None, chat_place),
        # As its session's tools were chosen (`agent_chat.py`: `auth.platform or "mcp"`).
        "agent chat without a platform": (
            who(
                is_chat_execution=True, chat_agent_id=derived("agent_chat"), chat_session_id=_GRANT
            ),
            None,
            FeaturePlace(t, "mcp", "chan-k", agent_id="agent_chat"),
        ),
        # Until its next send records it, a session made before this release reads as before.
        "agent chat with no send recorded": (chat, None, workspace),
        "agent chat recorded before its grant was": (chat, None, workspace),
        "agent chat of another account": (chat, None, workspace),
        "agent chat whose account was erased": (chat, None, workspace),
    }
    assert set(known) == set(_CALLERS)
    auth, named, place = known[caller]
    assert await caller_places(runtime, auth, named) == [place]


@pytest.mark.parametrize(
    ("surface", "decided"),
    [({"no_confirmation_surface": True}, True), ({"anthropic_disabled": True}, True), ({}, False)],
)
async def test_a_provider_steps_daimon_call_is_decided_by_its_lanes_step(
    monkeypatch: pytest.MonkeyPatch, surface: dict[str, bool], decided: bool
) -> None:
    """A provider turn's credential (its row's surface) or a harness's is decided by the step
    running in its lane; a Managed Agents credential is decided in its session instead."""
    from daimon.adapters.mcp.middleware import capability_gate

    asked: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    async def no_step(_sm: object, _s: object, *, run_id: uuid.UUID, **kw: object) -> None:
        asked.append((run_id, kw["execution_id"]))  # type: ignore[arg-type]

    monkeypatch.setattr(capability_gate, "running_step_decider", no_step)
    run, grant = uuid.uuid4(), uuid.uuid4()
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER,
        workflow_run_id=run, execution_id=grant, **surface,
    )  # fmt: skip
    runtime = SimpleNamespace(session_factory=None, settings=None)
    refused = await capability_gate.step_refusal(runtime, auth, "remember", {})  # type: ignore[arg-type]
    assert (refused is not None, asked) == ((True, [(run, grant)]) if decided else (False, []))

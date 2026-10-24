"""Guard (smoke-5 P0): after "switch yourself to GPT" -> the copy offer -> "yes, make the
copy", the switch CARD is posted in that same turn; the person approves it there.

No runtime rows: only code defaults (each provider's deployment key is set). The
agent's MCP tools are called as its model would call them; everything behind them is
the production path, with fake provider HTTP edges: the built-in Daimon refuses to
switch the whole server and offers a copy; "yes" makes a NEW copy (owned by whoever
asked), and its approved switch card hands this thread to it and switches it to GPT,
in one step (a hand-off first is refused); the next message is
admitted to the copy on OpenAI. Any refusal on this path is a product bug.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import McpSettings, Settings, TurnSettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_thread_session,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import MARouter, list_response
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from sqlalchemy.ext.asyncio import async_sessionmaker

TOOLSET = [
    {
        "type": "mcp_toolset",
        "mcp_server_name": "daimon-mcp",
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        # As the deployed agent has it: a provider switch always asks first (the card).
        "configs": [
            {
                "name": "request_agent_backend_switch",
                "enabled": True,
                "permission_policy": {"type": "always_ask"},
            }
        ],
    }
]
MCP_SERVERS = [{"name": "daimon-mcp", "type": "url", "url": "https://mcp.invalid"}]


@dataclass
class FakeMA:
    """Managed Agents as one stateful fake: agents are listed, read and created."""

    tenant_id: uuid.UUID
    agents: dict[str, dict] = field(default_factory=dict)
    created: list[dict] = field(default_factory=list)
    sessions: dict[str, dict] = field(default_factory=dict)

    def add(self, agent) -> None:
        self.agents[agent.id] = agent.model_dump(mode="json")

    def router(self, environment) -> MARouter:
        router = MARouter()
        router.add(
            "GET", r"/v1/agents", lambda call, match: list_response(list(self.agents.values()))
        )
        router.add("POST", r"/v1/agents", self._create)
        router.add("GET", r"/v1/agents/([^/]+)", self._get)
        router.add(
            "GET",
            rf"/v1/environments/{re.escape(environment.id)}",
            lambda call, match: httpx.Response(200, json=environment.model_dump(mode="json")),
        )
        router.add(
            "GET",
            r"/v1/environments",
            lambda call, match: list_response([environment.model_dump(mode="json")]),
        )
        router.add(
            "GET",
            r"/v1/sessions/([^/]+)",
            lambda call, match: httpx.Response(200, json=self.sessions[match.group(1)]),
        )
        return router

    def _get(self, call, match):
        raw = self.agents.get(match.group(1))
        if raw is None:
            return httpx.Response(404, json={"type": "error", "error": {"type": "not_found_error"}})
        return httpx.Response(200, json=raw)

    def _create(self, call, match):
        """The created agent answers with what was asked, like the real API."""
        payload = json.loads(call.content)
        self.created.append(payload)
        base = next(iter(self.agents.values()))
        identity = f"agent_copy_{len(self.created)}"
        self.agents[identity] = {
            **base,
            **payload,
            "id": identity,
            "version": 1,
            "archived_at": None,
        }
        return httpx.Response(200, json=self.agents[identity])


@dataclass
class Chat:
    runtime: McpRuntime
    auth: AuthIdentity
    origin: object
    ma: FakeMA
    trace: ScriptedTransport


@pytest.fixture(params=("admin",))
async def fresh_chat(request, db_preflight, db_nullpool_engine, monkeypatch):
    """The built-in Daimon answering a person's first message in a fresh public channel."""
    from daimon.core.provider_keys import DEFAULT_KEY_ENV

    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    admin = request.param == "admin"
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, tenant=tenant, account=account, platform="discord", external_id="123"
        )
        await make_ledger_entry(
            session, tenant=tenant, delta_usd=__import__("decimal").Decimal("10")
        )
        await make_thread_session(
            session,
            tenant=tenant,
            account=account,
            thread_id="thread",
            channel_id="fresh-channel",
            ma_session_id="sess_daimon",
            ma_agent_id="agent_daimon",
        )
        # A public channel's session records its seals: none.
        from sqlalchemy import text

        await session.execute(
            text(
                "UPDATE thread_sessions SET seal_ids = '[]'::jsonb WHERE ma_session_id = 'sess_daimon'"
            )
        )
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id="fresh-channel",
            thread_id="thread",
            responder_ma_agent_id="agent_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN if admin else Role.USER,
            sender_foreign=False,
            shared_channel=False,
            now=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            chat_session_id="fixture-session-grant",
        )
    ma = FakeMA(tenant.id)
    daimon = ma_agent(
        id="agent_daimon",
        tenant_id=str(tenant.id),
        name="Daimon",
        tools=TOOLSET,
        mcp_servers=MCP_SERVERS,
    )
    ma.add(daimon)
    ma.sessions["sess_daimon"] = ma_session(
        id="sess_daimon",
        agent=daimon,
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_account": str(account.id),
            "daimon_channel": "fresh-channel",
            "daimon_thread": "thread",
        },
    ).model_dump(mode="json")
    environment = ma_environment(tenant_id=tenant.id)
    environment.metadata["daimon_name"] = "default"
    trace = ScriptedTransport(router=ma.router(environment))
    async with trace.client() as client:
        settings = Settings(
            database={"url": "postgresql+asyncpg://fixture:fixture@localhost/fixture"},
            anthropic={"api_key": "test-fixture-key"},
            turn=TurnSettings(path="mux", channel_backends=True, agent_backends=True),
            mcp=McpSettings(public_url="https://mcp.invalid"),
        )
        runtime = McpRuntime(
            sessions,
            client,
            settings,
            DeploymentDefault(agent_name="Daimon", environment_name="default"),
        )
        auth = AuthIdentity(
            tenant_id=tenant.id,
            account_id=account.id,
            role=Role.ADMIN if admin else Role.USER,
            is_admin=admin,
            is_platform_admin=admin,
            platform="discord",
            platform_user_id="123",
            chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_daimon"),
            chat_session_id="fixture-session-grant",
        )
        yield Chat(runtime, auth, origin, ma, trace)


#: Wording that makes the card wait for another chat reply after the person already
#: said yes to the copy (smoke-5: the model held the card back, "the switch is still
#: pending"). The card IS the person's approval: nothing may stand between the
#: preview and posting it.
_HOLDS_THE_CARD_BACK = (
    r"\bonly if\b",
    r"before anything else",
    r"\bwait for\b",
    r"\bask (them|the person) (again|first|to confirm)\b",
)


async def _switch_tool_description(runtime) -> str:
    from daimon.adapters.mcp.tools.agents import register_agent_tools
    from fastmcp import FastMCP

    mcp = FastMCP("guard")
    register_agent_tools(mcp, runtime)
    tool = await mcp.get_tool("request_agent_backend_switch")
    return tool.description or ""


def _assert_the_card_is_posted_in_this_turn(*guidance: str) -> None:
    for text in guidance:
        for pattern in _HOLDS_THE_CARD_BACK:
            assert not re.search(pattern, text, flags=re.IGNORECASE), (pattern, text)
        assert "preview_digest" in text, text  # says how to post the card: call again


async def test_after_yes_make_the_copy_the_switch_card_is_posted_in_that_turn(
    fresh_chat, monkeypatch
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from daimon.adapters.mcp.tools.task_continuity import (
        _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.channel_backend import AgentBackendService, load_backend_agent
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.turn import openai_deployment
    from daimon.core.turn.admission import admit
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.core.turn.runtimes import build_channel_runtime
    from daimon.core.turn_hold import RequesterOrg
    from daimon.testing.turn_fakes import RecordingLifecycle, deps_for_pinned_mux

    from packages.core.tests.turn.test_openai_full_source import FullWire
    from packages.core.tests.turn.test_openai_runtime_transport import clients_for
    from packages.core.tests.turn.test_openai_tenant_environment import (
        DEPLOYMENT,
        FakeEnvironments,
    )

    chat = fresh_chat
    # 1. The built-in agent answers every channel: it won't switch the server, it
    #    offers a copy, and nothing changes.
    with pytest.raises(ToolError) as offered:
        await _request_agent_backend_switch_impl(
            chat.runtime,
            chat.auth,
            agent_name="Daimon",
            target_backend="openai",
            origin_context_id=str(chat.origin.id),
        )
    assert "I can make a copy of me on OpenAI" in str(offered.value)
    assert "Nothing changed" in str(offered.value)
    # 2. "yes": a NEW copy, owned by whoever asked.
    copy = await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
        origin_context_id=str(chat.origin.id),
    )
    assert copy.name == "Daimon-2"
    # 3. The BAD order (smoke-6 J1): handing this thread to the copy before its switch.
    #    Refused, with the plain next step, and nothing changes: the copy would otherwise
    #    answer the next message with a switch card instead of on GPT.
    with pytest.raises(ToolError) as early:
        await _hand_off_task_impl(
            chat.runtime, chat.auth, origin_context_id=str(chat.origin.id), agent_id=copy.id
        )
    assert "post its switch card now" in str(early.value)
    assert "Nothing was changed" in str(early.value)
    # 4. The copy switches to GPT: preview, then the confirmed call the card runs.
    preview = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend="openai",
        origin_context_id=str(chat.origin.id),
    )
    # The guidance at this exact point posts the card now, in this turn: "yes, make the
    # copy" already said go; the person approves or cancels on the card.
    _assert_the_card_is_posted_in_this_turn(
        preview.summary, await _switch_tool_description(chat.runtime)
    )
    # The card the person sees before Approve names what the copy won't have there.
    from functools import partial as _partial
    from unittest.mock import AsyncMock as _AsyncMock

    from daimon.adapters.mcp.tools.agents import (
        _capture_backend_source,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.channel_backend import make_backend_service
    from daimon.core.confirmation import backend_switch_callbacks, prompt_for_tool_call
    from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall

    # The card's own service, capturing the source the way the switch tool does.
    card_service = make_backend_service(
        chat.runtime.session_factory,
        chat.runtime.settings.turn,
        capture_source=_partial(_capture_backend_source, chat.runtime, chat.auth, chat.origin),
    )
    prepare, _ = backend_switch_callbacks(
        card_service,
        sessionmaker=chat.runtime.session_factory,
        anthropic=chat.runtime.client,
        tenant_id=chat.auth.tenant_id,
        account_id=chat.auth.account_id,
        current_admin=_AsyncMock(return_value=chat.auth.is_admin),
        current_platform_admin=_AsyncMock(return_value=False),
    )
    card = await prepare(
        prompt_for_tool_call(
            ToolCall(
                tool_use_id="j1-switch",
                server_name=DAIMON_SERVER_NAME,
                tool_name="request_agent_backend_switch",
                input={
                    "agent_name": copy.name,
                    "target_backend": "openai",
                    "expected_revision": preview.expected_revision,
                    "preview_digest": preview.preview_digest,
                    "accepted_drops": list(preview.declared_drops),
                },
            ),
            requester_platform_user_id="123",
            now=datetime.now(UTC),
        )
    )
    assert card.action == f"Switch {copy.name} to OpenAI"
    assert "On OpenAI it won't have " in card.consequence
    assert "its memory" in card.consequence and "its sandbox" in card.consequence
    changed = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend="openai",
        origin_context_id=str(chat.origin.id),
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    # The approval itself handed this thread to the copy, then switched it: one step.
    assert "This conversation moves to OpenAI from the next message" in changed.summary
    # The server's default agent is unchanged, for everyone.
    service = AgentBackendService(chat.runtime.session_factory, enabled=True)
    daimon = await load_backend_agent(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        agent_name="Daimon",
    )
    current = await service.current(daimon)
    assert current is None or current.backend in (None, "anthropic")
    # 5. The next message in this thread runs on the copy, on OpenAI: the code-default
    #    row, the tenant's hosted environment created once, no runtime rows.
    wire = FullWire(str(chat.auth.tenant_id))
    clients_for(monkeypatch, wire.handle)
    monkeypatch.setattr(openai_deployment, "_deployment_id", AsyncMock(return_value=DEPLOYMENT))
    from daimon.core.turn.runtime_defaults import DEFAULT_PROJECT

    class NativeTemplate(FakeEnvironments):
        """The tenant environment the OpenAI fake serves (its hosted template)."""

        def _environment(self, tenant_id, name):
            made = super()._environment(tenant_id, name)
            ref = made.ref.model_copy(
                update={"id": "native-template", "account_scope_id": DEFAULT_PROJECT}
            )
            return made.model_copy(update={"ref": ref})

    environments = NativeTemplate()
    monkeypatch.setattr(openai_deployment, "_environments", lambda key: environments)
    sm = chat.runtime.session_factory
    deps = replace(
        deps_for_pinned_mux(chat.runtime.client, sm),
        agent_backends=True,
        deployment_default=chat.runtime.deployment_default,
        state_store=PostgresStateStore(sm),
        turn_runtimes={},
        turn_runtime_factory=build_channel_runtime,
    )
    admission = await admit(
        deps,
        tenant_id=chat.auth.tenant_id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="123",
        channel_id="fresh-channel",
        thread_id="thread",
        now=datetime.now(UTC),
    )
    assert admission.agent.id == copy.id
    assert admission.backend_revision is not None
    assert admission.backend_revision.backend == "openai"
    prepared = await bind_session_impl(
        deps,
        admission,
        tenant_id=chat.auth.tenant_id,
        platform="discord",
        external_user_id="123",
        thread_id="thread",
        session_account_id=chat.auth.account_id,
        reuse_existing=True,
    )
    assert prepared.session_ref.provider == "openai"

    async def no_recovery():
        pytest.fail("the turn changed its admitted source/session")

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=chat.auth.tenant_id,
        platform="discord",
        thread_id="thread",
        external_user_id="123",
        user_message="Which model are you on?",
        lifecycle=RecordingLifecycle(),
        cancel=__import__("asyncio").Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda _: RecordingLifecycle(),
        operation_key="j1-next",
    )
    assert outcome.state.error is None
    (session_create,) = wire.posts
    assert "spend_control" not in session_create  # a GPT agent's own turn (#1394)


async def _new_copy(chat: Chat, *, here: bool = True):
    return await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
        origin_context_id=str(chat.origin.id) if here else None,
    )


async def _switch(chat: Chat, name: str, **confirm):
    return await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=name,
        target_backend="openai",
        origin_context_id=str(chat.origin.id),
        **confirm,
    )


async def _handed_to(chat: Chat) -> str | None:
    from daimon.core.stores.thread_agent_bindings import get_binding

    async with chat.runtime.session_factory() as session:
        handed = await get_binding(
            session,
            tenant_id=chat.auth.tenant_id,
            platform="discord",
            parent_channel_id="fresh-channel",
            thread_id="thread",
        )
    return handed.responder_name if handed is not None else None


async def _provider_of(chat: Chat, name: str) -> str | None:
    from daimon.core.channel_backend import AgentBackendService, load_backend_agent

    agent = await load_backend_agent(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        agent_name=name,
    )
    current = await AgentBackendService(chat.runtime.session_factory, enabled=True).current(agent)
    return current.backend if current is not None else None


@pytest.mark.parametrize("stale", ("revision", "digest"))
async def test_a_refused_switch_card_leaves_the_thread_with_the_root(fresh_chat, stale):
    """The hand-off the card promises comes with its switch or not at all: a stale or
    changed card changes nothing, so the thread stays with the root (#1463 review 1)."""
    chat = fresh_chat
    copy = await _new_copy(chat)
    preview = await _switch(chat, copy.name)
    with pytest.raises(ToolError):
        await _switch(
            chat,
            copy.name,
            expected_revision=preview.expected_revision + (stale == "revision"),
            preview_digest=preview.preview_digest if stale == "revision" else "0" * 64,
            accepted_drops=preview.declared_drops,
        )
    assert await _handed_to(chat) is None
    assert await _provider_of(chat, copy.name) in (None, "anthropic")


async def test_a_repo_bound_thread_answers_the_unsaved_work_question_before_the_card(
    fresh_chat,
):
    """A thread with a repo bound: the uncommitted-work question comes with the preview,
    before any card, and its answer rides the confirmed call (#1463 review 2)."""
    from daimon.core.session_snapshot import SessionSnapshot
    from daimon.core.stores.thread_sessions import get_live_thread_session
    from sqlalchemy import text

    chat = fresh_chat
    snapshot = SessionSnapshot(
        ma_agent_id="agent_daimon",
        model_id="claude-sonnet-5",
        system_sha256=None,
        skills_sha256="skills",
        environment_id="env_1",
        repo_url="https://github.com/acme/data",
        repo_branch="main",
        memory_store_id=None,
        vault_id=None,
        tools_sha256="tools",
        mcp_servers_sha256="mcp",
        env_sha256=None,
        agent_version=1,
        agent_name="Daimon",
    )
    async with chat.runtime.session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE thread_sessions SET effective_config = CAST(:config AS jsonb) "
                "WHERE ma_session_id = 'sess_daimon'"
            ),
            {"config": snapshot.model_dump_json()},
        )
    copy = await _new_copy(chat)
    with pytest.raises(ToolError, match="uncommitted changes in https://github.com/acme/data"):
        await _switch(chat, copy.name)
    preview = await _switch(chat, copy.name, unsaved_work="leave")
    changed = await _switch(
        chat,
        copy.name,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
        unsaved_work="leave",
    )
    assert "This conversation moves to OpenAI from the next message" in changed.summary
    assert await _handed_to(chat) == copy.name
    async with chat.runtime.session_factory() as session:
        live = await get_live_thread_session(
            session,
            tenant_id=chat.auth.tenant_id,
            platform="discord",
            thread_id="thread",
            account_id=chat.auth.account_id,
        )
    assert live is not None and live.pending_unsaved_work == "leave"


@pytest.mark.parametrize("made", ("elsewhere", "unstamped"))
async def test_switching_an_older_copy_never_moves_this_thread(fresh_chat, made):
    """Only a copy made in this conversation for its switch is handed it: an older copy,
    made in another thread or before the stamp (a channel rule's copy has none), is an
    ordinary agent, handed off by hand_off_task as before (#1463 review 3)."""
    from daimon.adapters.mcp.tools.task_continuity import (
        _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
    )

    chat = fresh_chat
    copy = await _new_copy(chat, here=made == "elsewhere")
    if made == "elsewhere":
        stamp = chat.ma.agents[copy.id]["metadata"]
        assert stamp["daimon_forked_from"] == "agent_daimon"
        stamp["daimon_forked_in"] = stamp["daimon_forked_in"].replace("thread", "other-thread")
    else:
        assert "daimon_forked_from" not in chat.ma.agents[copy.id]["metadata"]
    preview = await _switch(chat, copy.name)
    changed = await _switch(
        chat,
        copy.name,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert "isn't the one answering here" in changed.summary
    assert await _handed_to(chat) is None
    # And hand_off_task treats it as any agent: no switch-card refusal (it's on OpenAI
    # now, so the other-provider rule speaks instead).
    with pytest.raises(ToolError) as refused:
        await _hand_off_task_impl(
            chat.runtime, chat.auth, origin_context_id=str(chat.origin.id), agent_id=copy.id
        )
    assert "post its switch card now" not in str(refused.value)

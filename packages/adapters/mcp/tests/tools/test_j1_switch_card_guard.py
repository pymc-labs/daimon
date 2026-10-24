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
            platform="discord",
            platform_user_id="123",
            chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_daimon"),
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
        # Says it outright (SMOKE 42591c55 J5: "If they want to proceed" read as "ask for
        # a chat yes"): the second call is in this turn, and no chat yes is asked for.
        flat = " ".join(text.lower().split())
        assert "same turn" in flat or "same reply" in flat, text
        assert re.search(r"(do not|never) ask the person to reply", flat), text


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
    from daimon.core.stores.thread_sessions import get_live_thread_session

    chat = fresh_chat
    await _bind_a_repo(chat)
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


async def _card(chat: Chat, name: str, preview, **extra):
    """The card the host posts for a confirmed call, prepared as the host does."""
    from functools import partial
    from unittest.mock import AsyncMock

    from daimon.adapters.mcp.tools.agents import (
        _capture_backend_source,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.channel_backend import make_backend_service
    from daimon.core.confirmation import backend_switch_callbacks, prompt_for_tool_call
    from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall

    service = make_backend_service(
        chat.runtime.session_factory,
        chat.runtime.settings.turn,
        capture_source=partial(_capture_backend_source, chat.runtime, chat.auth, chat.origin),
    )
    prepare, _ = backend_switch_callbacks(
        service,
        sessionmaker=chat.runtime.session_factory,
        anthropic=chat.runtime.client,
        tenant_id=chat.auth.tenant_id,
        account_id=chat.auth.account_id,
        current_admin=AsyncMock(return_value=chat.auth.is_admin),
    )
    return await prepare(
        prompt_for_tool_call(
            ToolCall(
                tool_use_id="j1-card",
                server_name=DAIMON_SERVER_NAME,
                tool_name="request_agent_backend_switch",
                input={
                    "agent_name": name,
                    "target_backend": "openai",
                    "expected_revision": preview.expected_revision,
                    "preview_digest": preview.preview_digest,
                    "accepted_drops": list(preview.declared_drops),
                    **extra,
                },
            ),
            requester_platform_user_id="123",
            now=datetime.now(UTC),
        )
    )


async def _bind_a_repo(chat: Chat) -> None:
    from daimon.core.session_snapshot import SessionSnapshot
    from sqlalchemy import text

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


async def test_the_unsaved_work_answer_is_bound_to_the_card(fresh_chat):
    """The answer the person gave is the one the card shows and the switch applies: an
    answer changed or invented after the preview is a different request (#1463 r2 a)."""
    chat = fresh_chat
    await _bind_a_repo(chat)
    copy = await _new_copy(chat)
    with pytest.raises(ToolError) as asked:
        await _switch(chat, copy.name)
    # The question names the tool whose retry carries the answer, never the hand-off
    # that refuses a copy awaiting its switch.
    assert "call request_agent_backend_switch again with unsaved_work" in str(asked.value)
    preview = await _switch(chat, copy.name, unsaved_work="leave")
    card = await _card(chat, copy.name, preview, unsaved_work="leave")
    assert "Uncommitted changes in the repo are left where they are" in (card.consequence or "")
    for changed in ("copy", None):
        with pytest.raises(ValueError, match="This preview changed"):
            await _card(chat, copy.name, preview, unsaved_work=changed)
        with pytest.raises(ToolError):
            await _switch(
                chat,
                copy.name,
                expected_revision=preview.expected_revision,
                preview_digest=preview.preview_digest,
                accepted_drops=preview.declared_drops,
                unsaved_work=changed,
            )
        assert await _handed_to(chat) is None
        assert await _provider_of(chat, copy.name) in (None, "anthropic")


async def test_a_hand_off_that_fails_after_the_switch_says_the_switch_landed(
    fresh_chat, monkeypatch
):
    """The switch landed but writing the hand-off failed (a database error): the reply
    says where the copy is now and where to find it (#1463 r2 b)."""
    from daimon.adapters.mcp.tools import task_continuity
    from sqlalchemy.exc import OperationalError

    chat = fresh_chat
    real = task_continuity._hand_off_task_impl  # pyright: ignore[reportPrivateUsage]

    async def failing(*args, **kwargs):
        if kwargs.get("switch") == "commit":
            raise OperationalError("INSERT", {}, Exception("connection lost"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(task_continuity, "_hand_off_task_impl", failing)
    copy = await _new_copy(chat)
    preview = await _switch(chat, copy.name)
    with pytest.raises(ToolError) as failed:
        await _switch(
            chat,
            copy.name,
            expected_revision=preview.expected_revision,
            preview_digest=preview.preview_digest,
            accepted_drops=preview.declared_drops,
        )
    assert str(failed.value) == (
        f"{copy.name} now uses OpenAI, but this conversation couldn't be handed to it: "
        f"something went wrong on our side. Ask {copy.name} in a new conversation."
    )
    assert await _provider_of(chat, copy.name) == "openai"
    assert await _handed_to(chat) is None


async def _switch_outside_chat(chat: Chat, name: str, backend: str, model: str) -> None:
    """An admin switches `name` outside chat (a setup panel's confirm): no card."""
    from dataclasses import replace
    from functools import partial

    from daimon.adapters.mcp.tools.agents import (
        _capture_backend_source,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core import channel_backend
    from daimon.core.authz import Subject

    outside = replace(chat.auth, chat_agent_id=None)
    service = channel_backend.make_backend_service(
        chat.runtime.session_factory,
        chat.runtime.settings.turn,
        capture_source=partial(_capture_backend_source, chat.runtime, outside, None),
    )
    agent = await channel_backend.load_backend_agent(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        agent_name=name,
    )
    preview = await service.preview(agent, backend, model)
    await service.set_backend(
        agent,
        backend,
        preview.setting_revision,
        channel_backend.BackendActor(
            chat.auth.tenant_id, chat.auth.account_id, Subject(is_admin=True)
        ),
        expected_preview_digest=preview.digest,
        accepted_drops=preview.declared_drops,
        model=model,
    )


@dataclass
class Wires:
    """What each provider was sent, across the conversation's messages."""

    gemini_requests: list[dict]
    gemini_catalog: object
    openai: object
    close: object


def _wires(chat: Chat, monkeypatch) -> Wires:
    """Gemini as J10 fakes it, OpenAI as the card leg above fakes it: once per test."""
    from unittest.mock import AsyncMock

    import mux.drivers.gemini as gemini_sdk
    from daimon.core.turn import openai_deployment
    from daimon.core.turn.runtime_defaults import DEFAULT_PROJECT
    from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
    from mux.testing.isolated_sdk import build_gemini_client

    from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import (
        GeminiCatalog,
        GeminiTransport,
    )
    from packages.core.tests.turn.test_openai_full_source import FullWire
    from packages.core.tests.turn.test_openai_runtime_transport import clients_for
    from packages.core.tests.turn.test_openai_tenant_environment import (
        DEPLOYMENT,
        FakeEnvironments,
    )

    class RootGemini(GeminiTransport):
        """A root conversation's interactions keep one reusable environment."""

        async def create(self, request):
            created = await super().create(request)
            created["environment_id"] = "j1-environment"
            self.saved[created["id"]]["environment_id"] = "j1-environment"
            return created

    gemini_wire = RootGemini()
    gemini_wire.counted = []
    monkeypatch.setattr(gemini_sdk, "configured_transport", lambda **kwargs: gemini_wire)
    catalog = GeminiCatalog()
    gemini_client = build_gemini_client(
        transport=httpx.MockTransport(catalog.handle),
        api_key="offline",
        base_url="https://gemini.invalid",
    )
    monkeypatch.setattr(
        gemini_sdk,
        "configured_catalog_transport",
        lambda **kwargs: SDKAgentCatalogTransport(gemini_client),
    )
    wire = FullWire(str(chat.auth.tenant_id))
    clients_for(monkeypatch, wire.handle)
    monkeypatch.setattr(openai_deployment, "_deployment_id", AsyncMock(return_value=DEPLOYMENT))

    class NativeTemplate(FakeEnvironments):
        def _environment(self, tenant_id, name):
            made = super()._environment(tenant_id, name)
            ref = made.ref.model_copy(
                update={"id": "native-template", "account_scope_id": DEFAULT_PROJECT}
            )
            return made.model_copy(update={"ref": ref})

    environments = NativeTemplate()
    monkeypatch.setattr(openai_deployment, "_environments", lambda key: environments)

    async def close() -> None:
        gemini_client.close()
        await gemini_client.aio.aclose()

    return Wires(gemini_wire.requests, catalog, wire, close)


async def _message(chat: Chat, key: str):
    """The person's next message in this thread, admitted and run end to end."""
    import asyncio
    from dataclasses import replace

    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.turn.admission import admit, backend_facts
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.core.turn.runtimes import build_channel_runtime
    from daimon.core.turn_hold import RequesterOrg
    from daimon.core.turn_origin import render_turn_origin
    from daimon.testing.turn_fakes import RecordingLifecycle, deps_for_pinned_mux

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
    controls = render_turn_origin(chat.origin, **backend_facts(admission))
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

    async def no_recovery():
        pytest.fail("the turn changed its admitted source/session")

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=chat.auth.tenant_id,
        platform="discord",
        thread_id="thread",
        external_user_id="123",
        user_message=controls + "\nWhat model are you?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda _: RecordingLifecycle(),
        operation_key=key,
    )
    assert outcome.state.error is None
    return admission, prepared, controls


def _says_and_runs(admission, prepared, controls, provider: str, model: str) -> None:
    """What it says it runs on == the card's footer == the provider it is bound to."""
    from daimon.core.turn.admission import running_on
    from mux.profiles import get_profile

    assert running_on(admission) == {"provider": provider, "model": model}
    assert admission.backend_footer_profile is not None
    assert get_profile(admission.backend_footer_profile).provider == provider
    assert f'"running_on": {{"provider": "{provider}", "model": "{model}"}}' in controls
    assert prepared.session_ref.provider == provider


TARGETS = {"gemini": "gemini-3.8-flash", "openai": "gpt-6-luna"}


@pytest.mark.parametrize("backend", sorted(TARGETS))
async def test_m03_a_switch_outside_chat_moves_this_thread_once_and_keeps_it_there(
    fresh_chat, monkeypatch, backend
):
    """M03 (P0): an agent switched outside chat (a setup panel, not the card) kept
    answering this thread on Claude while it claimed Gemini. On cheap models: Daimon on
    Haiku is switched to Gemini Flash or GPT Luna; the next message in the SAME thread
    moves there, says so once, and the second and third stay in that conversation
    (no re-move, no repeated note), each saying what it runs on == footer == binding."""
    chat = fresh_chat
    chat.ma.agents["agent_daimon"]["model"] = {"id": "claude-haiku-5-5", "speed": "standard"}
    model = TARGETS[backend]
    await _switch_outside_chat(chat, "Daimon", backend, model)
    wires = _wires(chat, monkeypatch)
    try:
        first = await _message(chat, "m03-1")
        assert first[0].agent.id == "agent_daimon"
        assert first[0].backend_follow is not None and first[0].backend_follow.kind == "moved"
        assert '"conversation_move": {"kind": "moved"' in first[2]
        _says_and_runs(*first, backend, model)
        for key in ("m03-2", "m03-3"):
            later = await _message(chat, key)
            assert later[0].backend_follow is None, "a moved conversation never moves again"
            assert "conversation_move" not in later[2], "the move is said once"
            assert later[1].session_ref == first[1].session_ref, "its history is kept"
            _says_and_runs(*later, backend, model)
    finally:
        await wires.close()  # pyright: ignore[reportCallIssue]
    if backend == "gemini":
        assert len(wires.gemini_requests) == 3
        for sent in wires.gemini_requests:
            assert sent["agent_config"]["model"] == model  # the model Gemini bills
    else:
        assert len(wires.openai.posts) == 1, "one OpenAI session, kept"  # pyright: ignore[reportAttributeAccessIssue]
        assert wires.openai.posts[0]["agent"]["model"] == model  # pyright: ignore[reportAttributeAccessIssue]


async def test_r08_the_hand_over_button_moves_this_thread_to_its_agents_provider(
    fresh_chat, monkeypatch
):
    """R08 (Astra review): the panel's hand-over button gave a Claude thread to an agent
    switched to Gemini, and the thread kept running on Claude. Now the next message is
    that agent's, on its own provider and model, and says so; the next one stays."""
    from daimon.core.native_admins import AdminCaller
    from daimon.core.thread_handoff import switch_thread_on_request

    chat = fresh_chat
    chat.ma.agents["agent_daimon"]["model"] = {"id": "claude-haiku-5-5", "speed": "standard"}
    flash = dict(chat.ma.agents["agent_daimon"])
    flash.update(
        id="agent_flash",
        name="flash-helper",
        metadata={**flash["metadata"], "daimon_name": "flash-helper"},
    )
    chat.ma.agents["agent_flash"] = flash
    await _switch_outside_chat(chat, "flash-helper", "gemini", "gemini-3.8-flash")
    switched = await switch_thread_on_request(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        platform="discord",
        parent_channel_id="fresh-channel",
        thread_id="thread",
        ma_agent_id="agent_flash",
        caller=AdminCaller(platform_user_id="123", is_server_admin=True),
        default=chat.runtime.deployment_default,
        channel="fresh-channel",
        now=datetime.now(UTC),
    )
    assert switched.switched, switched.text
    wires = _wires(chat, monkeypatch)
    try:
        first = await _message(chat, "r08-1")
        assert first[0].agent.id == "agent_flash"
        assert first[0].backend_follow is not None and first[0].backend_follow.kind == "moved"
        _says_and_runs(*first, "gemini", "gemini-3.8-flash")
        second = await _message(chat, "r08-2")
        assert second[0].backend_follow is None and second[1].session_ref == first[1].session_ref
        _says_and_runs(*second, "gemini", "gemini-3.8-flash")
    finally:
        await wires.close()  # pyright: ignore[reportCallIssue]


async def test_m03_a_refused_message_leaves_no_move_behind(fresh_chat, monkeypatch):
    """Review finding 3: a message refused for its balance after deciding to move
    leaves nothing behind; the thread is unchanged and no release is pending."""
    from daimon.core.stores import thread_provider_releases
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.testing.factories import make_ledger_entry
    from mux.contracts.ids import ChannelRef, ThreadRef
    from mux.state.lease import Slot

    chat = fresh_chat
    await _switch_outside_chat(chat, "Daimon", "gemini", "gemini-3.8-flash")
    async with chat.runtime.session_factory.begin() as session:
        from daimon.core.stores.tenants import get_tenant

        tenant = await get_tenant(session, chat.auth.tenant_id)
        await make_ledger_entry(
            session, tenant=tenant, delta_usd=__import__("decimal").Decimal("-10")
        )
    wires = _wires(chat, monkeypatch)
    try:
        with pytest.raises(AdmissionDenied, match="balance_depleted"):
            await _message(chat, "m03-broke")
    finally:
        await wires.close()  # pyright: ignore[reportCallIssue]
    slot = Slot(
        thread=ThreadRef(
            channel=ChannelRef(
                tenant_id=str(chat.auth.tenant_id), platform="discord", channel_id="fresh-channel"
            ),
            thread_id="thread",
        ),
        account_id=str(chat.auth.account_id),
    )
    async with chat.runtime.session_factory() as session:
        assert await thread_provider_releases.requested_generation(session, slot) is None
    store = PostgresStateStore(chat.runtime.session_factory)
    assert await store.get_binding(slot, include_released=True) is None, "nothing moved"


async def test_m03_a_cards_pending_move_is_never_touched_by_a_follow(fresh_chat, monkeypatch):
    """Review finding 2: a card confirmed meanwhile keeps its request; the message that
    follows never deletes it, and the card's move is what applies."""
    from daimon.core.stores import thread_provider_releases
    from daimon.core.thread_release import request_thread_release
    from mux.contracts.ids import ChannelRef, ThreadRef
    from mux.state.lease import Slot

    chat = fresh_chat
    await _switch_outside_chat(chat, "Daimon", "gemini", "gemini-3.8-flash")
    slot = Slot(
        thread=ThreadRef(
            channel=ChannelRef(
                tenant_id=str(chat.auth.tenant_id), platform="discord", channel_id="fresh-channel"
            ),
            thread_id="thread",
        ),
        account_id=str(chat.auth.account_id),
    )
    async with chat.runtime.session_factory.begin() as session:
        await request_thread_release(session, slot, generation=0, now=datetime.now(UTC))
    wires = _wires(chat, monkeypatch)
    try:
        moved = await _message(chat, "m03-card")
    finally:
        await wires.close()  # pyright: ignore[reportCallIssue]
    # The card's request selected and was consumed by its own path; no follow ran.
    assert moved[0].backend_follow is None
    _says_and_runs(*moved, "gemini", "gemini-3.8-flash")
    async with chat.runtime.session_factory() as session:
        assert await thread_provider_releases.requested_generation(session, slot) is None


async def test_m03_a_card_confirmed_mid_message_keeps_its_move_when_the_follow_fails(
    fresh_chat, monkeypatch
):
    """Review finding 2: a card is confirmed while a message is deciding to follow;
    that message's own move is then refused by the provider. The card's request is
    never deleted (0cc9f903's withdraw deleted whatever request the slot held)."""
    from dataclasses import replace

    from daimon.core.stores import thread_provider_releases
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.thread_release import request_thread_release
    from daimon.core.turn import admission as admission_module
    from daimon.core.turn.admission import admit
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.runtimes import build_channel_runtime
    from daimon.core.turn_hold import RequesterOrg
    from daimon.testing.turn_fakes import deps_for_pinned_mux
    from mux.contracts.ids import ChannelRef, ThreadRef
    from mux.state.lease import Slot

    chat = fresh_chat
    await _switch_outside_chat(chat, "Daimon", "openai", "gpt-6-luna")
    slot = Slot(
        thread=ThreadRef(
            channel=ChannelRef(
                tenant_id=str(chat.auth.tenant_id), platform="discord", channel_id="fresh-channel"
            ),
            thread_id="thread",
        ),
        account_id=str(chat.auth.account_id),
    )
    follow = admission_module._thread_follow  # pyright: ignore[reportPrivateUsage]
    confirmed: list[bool] = []

    async def card_confirms_meanwhile(*args, **kwargs):
        decided = await follow(*args, **kwargs)
        if not confirmed:
            confirmed.append(True)
            async with chat.runtime.session_factory.begin() as session:
                await request_thread_release(session, slot, generation=0, now=datetime.now(UTC))
        return decided

    monkeypatch.setattr(admission_module, "_thread_follow", card_confirms_meanwhile)
    sm = chat.runtime.session_factory
    deps = replace(
        deps_for_pinned_mux(chat.runtime.client, sm),
        agent_backends=True,
        deployment_default=chat.runtime.deployment_default,
        state_store=PostgresStateStore(sm),
        turn_runtimes={},
        turn_runtime_factory=build_channel_runtime,
    )
    # OpenAI isn't faked here: its preparation refuses this message's move.
    with pytest.raises(AdmissionDenied):
        await admit(
            deps,
            tenant_id=chat.auth.tenant_id,
            requester_org=RequesterOrg.HOST,
            platform="discord",
            external_user_id="123",
            channel_id="fresh-channel",
            thread_id="thread",
            now=datetime.now(UTC),
        )
    assert confirmed, "the card was confirmed during the message"
    async with sm() as session:
        assert await thread_provider_releases.requested_generation(session, slot) == 0, (
            "the card's move request is still pending"
        )

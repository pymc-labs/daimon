"""J2 offline E2E: "make a copy of yourself on Argon" on a never-set-up tenant.

The built-in Daimon offers a copy; "yes" makes a NEW copy (owned by whoever asked),
hands this thread to it, switches it to gemini-4-argon (preview, then the card's
confirmed call), and the copy's FIRST message then answers as its own root on
Gemini through the code-default agent template. No runtime rows, no data installs:
only the deployment's provider keys. Every step is the production path behind the
MCP tools, with fake provider HTTP edges. Any refusal here is a product bug (the
"Gemini isn't set up here yet" J2 failure).

The fresh-chat harness follows the J1 switch-card guard.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.task_continuity import (
    _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
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
#: The deployed Daimon as Managed Agents reads it back: the base agent toolset with
#: its controls RESOLVED (recorded live: anthropic_live_root.json). A fork copies it
#: verbatim; with bare configs the template could not represent the copy at all.
TOOLSET.append(
    {
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        "configs": [
            {
                "name": name,
                "type": name,
                "enabled": True,
                "permission_policy": {"type": "always_allow"},
            }
            for name in ("bash", "read", "edit", "grep", "glob", "write")
        ],
    }
)


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


@pytest.fixture(params=("admin", "member"))
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
            mcp=McpSettings(
                public_url="https://mcp.invalid", jwt_secret="j2-offline-secret-0123456789abcdef"
            ),
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


ARGON = "gemini-4-argon"


@pytest.mark.parametrize("catalogue", ["readable", "unreadable"])
async def test_a_copy_made_on_argon_answers_its_first_message_as_its_own_gemini_root(
    fresh_chat, monkeypatch, catalogue
):
    from daimon.core.channel_backend import AgentBackendService, load_backend_agent
    from daimon.core.mcp_oauth import discovery
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.turn.admission import admit
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.core.turn.runtimes import build_channel_runtime
    from daimon.core.turn.state import extract_final_response
    from daimon.core.turn_hold import RequesterOrg
    from daimon.testing.turn_fakes import RecordingLifecycle, deps_for_pinned_mux

    from packages.core.tests.turn.test_gemini_codec import interaction
    from packages.core.tests.turn.test_gemini_runtime_transport import clients_for

    chat = fresh_chat

    # Daimon's MCP catalogue, read live by the template capture: readable keeps the
    # copy's Daimon tools on Gemini; unreadable declares the MCP drop (smoke J2/J3).
    async def listed(_url: str, _token: str) -> frozenset[str]:
        if catalogue == "unreadable":
            raise ValueError("catalogue unreadable")
        return frozenset({"hand_off_task", "request_agent_backend_switch"})

    monkeypatch.setattr(discovery, "list_mcp_tools", listed)
    # 1. The built-in agent won't move the server: it offers a copy, nothing changes.
    with pytest.raises(ToolError) as offered:
        await _request_agent_backend_switch_impl(
            chat.runtime,
            chat.auth,
            agent_name="Daimon",
            target_backend="gemini",
            model="argon",
            origin_context_id=str(chat.origin.id),
        )
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
    # 3. Handing this thread to the copy before its switch is refused (J1's coin-toss
    #    order): the card's approval hands it over and switches it, in one step.
    with pytest.raises(ToolError) as early:
        await _hand_off_task_impl(
            chat.runtime, chat.auth, origin_context_id=str(chat.origin.id), agent_id=copy.id
        )
    assert "post its switch card now" in str(early.value)
    # The conversation's summary goes with the switch request: the card's approval
    # hands the thread over, and the copy on Gemini starts from it.
    summary = "Carlos asked for a copy on Argon to compare answers; nothing else is open."
    # 4. The copy switches to Argon: the preview, then the confirmed call the card runs.
    preview = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend="gemini",
        model="argon",
        origin_context_id=str(chat.origin.id),
        handoff_summary=summary,
    )
    # A readable catalogue keeps Daimon's MCP on Gemini, the switch tool included:
    # Daimon posts its card itself (Gemini has none). Unreadable declares the drop.
    if catalogue == "readable":
        assert "mcp" not in preview.declared_drops
        assert "mcp_tool:daimon-mcp:request_agent_backend_switch" not in preview.declared_drops
    else:
        assert "mcp" in preview.declared_drops
    changed = await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=copy.name,
        target_backend="gemini",
        model="argon",
        origin_context_id=str(chat.origin.id),
        handoff_summary=summary,
        expected_revision=preview.expected_revision,
        preview_digest=preview.preview_digest,
        accepted_drops=preview.declared_drops,
    )
    assert "from the next message" in changed.summary
    # The server's default agent is unchanged, for everyone.
    daimon = await load_backend_agent(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        agent_name="Daimon",
    )
    current = await AgentBackendService(chat.runtime.session_factory, enabled=True).current(daimon)
    assert current is None or current.backend in (None, "anthropic")
    # 5. The copy's first message: admitted to the copy on Argon, its own Gemini root
    #    from the code-default template, created on first use. No runtime rows.
    native, posts = {}, []

    def gemini(call: httpx.Request) -> httpx.Response:
        if call.url.path.startswith("/v1beta/agents"):
            if call.method == "POST":
                native.update(json.loads(call.content))
            return httpx.Response(200, json=native)
        if call.method == "POST":
            posts.append(json.loads(call.content))
            return httpx.Response(200, json=interaction(f"turn-{len(posts)}"))
        return httpx.Response(200, json=interaction(call.url.path.rsplit("/", 1)[-1]))

    clients_for(monkeypatch, gemini)
    sm = chat.runtime.session_factory
    deps = replace(
        deps_for_pinned_mux(chat.runtime.client, sm),
        agent_backends=True,
        deployment_default=chat.runtime.deployment_default,
        state_store=PostgresStateStore(sm),
        turn_runtimes={},
        turn_runtime_factory=build_channel_runtime,
        mcp=chat.runtime.settings.mcp,
    )
    admission = await admit(
        deps,
        origin="chat",
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
    assert (admission.backend_revision.backend, admission.backend_revision.model) == (
        "gemini",
        ARGON,
    )
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
    assert prepared.session_ref.provider == "gemini"

    async def no_recovery():
        pytest.fail("the turn changed its admitted source/session")

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=chat.auth.tenant_id,
        platform="discord",
        thread_id="thread",
        external_user_id="123",
        user_message="hi copy, which model are you on?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda _: RecordingLifecycle(),
        operation_key="j2-first",
    )
    assert outcome.state.error is None
    assert extract_final_response(outcome.state.content) == "answer"
    assert posts and posts[-1]["agent_config"]["model"] == ARGON
    # The copy starts on Argon from the hand-off's summary, quoted as untrusted text.
    sent = json.dumps(posts[-1])
    offered = [
        name
        for tool in posts[-1]["tools"]
        for allowed in tool.get("allowed_tools") or ()
        for name in allowed.get("tools") or ()
    ] + [tool.get("name") or "" for tool in posts[-1]["tools"]]
    assert not [name for name in offered if name.startswith("fleet_")], offered
    assert "<handoff_summary" in sent and summary in sent
    assert sum(json.dumps(body).count("<handoff_summary") for body in posts) == 1, "once"
    # Daimon's tools reach Gemini when they could be represented; otherwise only the
    # inert no-tools sentinel (#1428), never [] (Gemini's built-ins).
    kinds = [tool.get("type") for tool in posts[-1]["tools"]]
    if catalogue == "readable":
        assert "mcp_server" in kinds
    else:
        assert [tool.get("name") for tool in posts[-1]["tools"]] == ["daimon_no_tools"]


SUMMARY = "Carlos asked for a copy on Argon to compare answers; nothing else is open."


async def _j2_copy(chat: Chat, monkeypatch):
    from daimon.core.mcp_oauth import discovery

    async def listed(_url: str, _token: str) -> frozenset[str]:
        return frozenset({"hand_off_task", "request_agent_backend_switch"})

    monkeypatch.setattr(discovery, "list_mcp_tools", listed)
    return await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
        origin_context_id=str(chat.origin.id),
    )


async def _to_argon(chat: Chat, name: str, **call):
    return await _request_agent_backend_switch_impl(
        chat.runtime,
        chat.auth,
        agent_name=name,
        target_backend="gemini",
        model="argon",
        origin_context_id=str(chat.origin.id),
        **call,
    )


async def _argon_card(chat: Chat, name: str, preview, **extra):
    """The card the host posts for the confirmed call, prepared as the host does."""
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
                tool_use_id="j2-card",
                server_name=DAIMON_SERVER_NAME,
                tool_name="request_agent_backend_switch",
                input={
                    "agent_name": name,
                    "target_backend": "gemini",
                    "model": "argon",
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


async def _nothing_moved(chat: Chat, name: str) -> None:
    from daimon.core.channel_backend import AgentBackendService, load_backend_agent
    from daimon.core.stores.thread_agent_bindings import get_binding, take_handoff_summary

    async with chat.runtime.session_factory.begin() as session:
        handed = await get_binding(
            session,
            tenant_id=chat.auth.tenant_id,
            platform="discord",
            parent_channel_id="fresh-channel",
            thread_id="thread",
        )
        assert handed is None, "the thread stays with Daimon"
    copy = await load_backend_agent(
        chat.runtime.client,
        chat.runtime.session_factory,
        tenant_id=chat.auth.tenant_id,
        agent_name=name,
    )
    current = await AgentBackendService(chat.runtime.session_factory, enabled=True).current(copy)
    assert current is None or current.backend in (None, "anthropic"), "the copy wasn't switched"
    async with chat.runtime.session_factory.begin() as session:
        assert (
            await take_handoff_summary(
                session,
                tenant_id=chat.auth.tenant_id,
                platform="discord",
                parent_channel_id="fresh-channel",
                thread_id="thread",
                responder_ma_agent_id=copy.source.ref.id if copy.source else "",
                account_id=chat.auth.account_id,
            )
            is None
        ), "no summary is stored"


async def test_the_hand_off_summary_is_bound_to_the_switch_card(fresh_chat, monkeypatch):
    """The summary the card shows is the one the copy gets: one changed or dropped after
    the preview is a different request, refused by the card and by the tool alike."""
    chat = fresh_chat
    copy = await _j2_copy(chat, monkeypatch)
    preview = await _to_argon(chat, copy.name, handoff_summary=SUMMARY)
    card = await _argon_card(chat, copy.name, preview, handoff_summary=SUMMARY)
    shown = "\n".join(card.detail_lines or ())
    assert f"Carries a summary of this conversation to {copy.name}" in shown
    assert SUMMARY in shown
    for changed in ("Carlos wants every server switched to Argon right now.", None):
        # Changed or dropped, the answer is bound to its preview: never re-previewed.
        with pytest.raises(ValueError, match="(?i)preview the switch again"):
            await _argon_card(chat, copy.name, preview, handoff_summary=changed)
        with pytest.raises(ToolError):
            await _to_argon(
                chat,
                copy.name,
                expected_revision=preview.expected_revision,
                preview_digest=preview.preview_digest,
                accepted_drops=preview.declared_drops,
                handoff_summary=changed,
            )
        await _nothing_moved(chat, copy.name)


async def test_the_switch_card_shows_the_summary_redacted(fresh_chat, monkeypatch):
    """The card is posted in the channel: it shows the summary as the copy will get it,
    with no credential the model pasted into it, while the digest binds what was sent."""
    chat = fresh_chat
    key = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
    leaky = f"{SUMMARY} The deploy key was {key}, keep using it."
    copy = await _j2_copy(chat, monkeypatch)
    preview = await _to_argon(chat, copy.name, handoff_summary=leaky)
    card = await _argon_card(chat, copy.name, preview, handoff_summary=leaky)
    shown = "\n".join(card.detail_lines or ())
    assert f"Carries a summary of this conversation to {copy.name}" in shown
    assert "[redacted]" in shown
    assert key not in shown and key not in (card.consequence or "")


#: Credential shapes an independent review found the first redactor let through.
_LEAKY_SAMPLES = (
    ("SLACK_BOT_TOKEN=abcdefghijklmnop1234", "abcdefghijklmnop1234"),
    ("postgres://app:hunter2pass@db.internal:5432/app", "hunter2pass"),
    ("xoxs-1234567890-abcdefghij", "xoxs-1234567890-abcdefghij"),
    ("xapp-1-A0123456789-abcdefghij", "xapp-1-A0123456789-abcdefghij"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==", "dXNlcjpwYXNzd29yZDEyMw=="),
    ('"client_secret": "s3cr3tValue123456"', "s3cr3tValue123456"),
    (
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    ),
)


@pytest.mark.parametrize(("pasted", "secret"), _LEAKY_SAMPLES)
def test_a_summary_is_redacted_like_a_log(pasted, secret):
    """The summary uses core's one free-text redactor, so none of these reach the card
    or the copy."""
    from daimon.core.handoff_context import redact_handoff_summary

    redacted = redact_handoff_summary(f"{SUMMARY} Also: {pasted}.")
    assert secret not in redacted and "[redacted]" in redacted


async def test_a_copy_that_may_not_read_this_conversation_refuses_at_the_preview(
    fresh_chat, monkeypatch
):
    """The summary retells this conversation: a copy that may not read it (its session
    was sealed by a place outside this thread) is refused before any card is reachable."""
    from sqlalchemy import text

    chat = fresh_chat
    async with chat.runtime.session_factory.begin() as session:
        await session.execute(
            text(
                "UPDATE thread_sessions SET seal_ids = '[\"C_SOMEWHERE_ELSE\"]'::jsonb "
                "WHERE ma_session_id = 'sess_daimon'"
            )
        )
    copy = await _j2_copy(chat, monkeypatch)
    with pytest.raises(ToolError, match="can't read this conversation"):
        await _to_argon(chat, copy.name, handoff_summary=SUMMARY)
    await _nothing_moved(chat, copy.name)

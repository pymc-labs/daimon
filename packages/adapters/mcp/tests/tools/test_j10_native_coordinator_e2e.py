"""J10 offline E2E: a GPT or Gemini agent coordinates a fleet, like a Claude one does.

A copy switched to another backend answers through its native counterpart; the fleet
root gate refused any admission carrying a counterpart, so every GPT/Gemini root ran
with fleet off (the computed default) or failed (an explicit grant). Astra review R02
and latent-audit M07. Same harness as J2 (production path, fake provider HTTP edges);
cheap models only (Gemini Flash; the Claude copy is the control).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from daimon.adapters.mcp.tools.agents import (
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)

from packages.adapters.mcp.tests.tools.test_j2_gemini_root_e2e import (
    db_nullpool_engine as db_nullpool_engine,
)
from packages.adapters.mcp.tests.tools.test_j2_gemini_root_e2e import db_preflight as db_preflight
from packages.adapters.mcp.tests.tools.test_j2_gemini_root_e2e import fresh_chat as fresh_chat

HAIKU = "claude-haiku-4-5"
#: The cheap native models a copy switches to.
NATIVE = {"gemini": "gemini-3.8-flash", "openai": "gpt-6-luna"}


class HostedTemplates:
    """OpenAI's hosted environment templates in front of a GPT root's wire: the
    tenant's environment is created on its first GPT turn, then listed."""

    def __init__(self, wire):
        self.wire = wire
        self.templates: list[dict] = []

    def handle(self, call: httpx.Request) -> httpx.Response:
        if call.url.path.removeprefix("/v1") == "/agents/environments/templates":
            if call.method == "POST":
                body = json.loads(call.content)
                self.templates.append(
                    {"id": f"envtmpl_{len(self.templates) + 1}", "created_at": 0, **body}
                )
                return httpx.Response(200, json=self.templates[-1])
            return httpx.Response(200, json={"data": self.templates, "has_more": False})
        for template in self.templates:
            if call.url.path.endswith("/agents/environments/templates/" + template["id"]):
                return httpx.Response(200, json=template)
        return self.wire.handle(call)


#: Daimon's MCP tools the chat catalogue starts from (the fleet tools and their peers).
DAIMON_TOOLS = (
    "hand_off_task",
    "request_agent_backend_switch",
    "list_agents",
    "fleet_spawn",
    "fleet_collect",
    "fleet_status",
    "fleet_agents",
    "fleet_kill",
    "fleet_resume",
    "fleet_cross_review",
)


async def _chat_catalogue(*, native: bool) -> frozenset[str]:
    """What Daimon's MCP server lists to a turn's credential, through its real search
    transform: a GPT/Gemini provider turn has no confirmation surface."""
    from typing import Any

    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.search_transform import AgentChatAwareBM25SearchTransform
    from daimon.core.stores.domain import Role
    from fastmcp import Client, FastMCP
    from fastmcp.server.middleware import Middleware, MiddlewareContext

    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN,
        no_confirmation_surface=native,
    )

    class Identity(Middleware):
        async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
            await context.fastmcp_context.set_state("auth", auth, serializable=False)
            return await call_next(context)

    server = FastMCP("daimon")
    for name in DAIMON_TOOLS:

        def tool(name: str = name) -> str:
            return name

        server.tool(tool, name=name)
    server.add_middleware(Identity())
    server.add_transform(AgentChatAwareBM25SearchTransform(max_results=5))
    async with Client(server) as client:
        return frozenset(tool.name for tool in await client.list_tools())


async def _coordinator_turn(
    chat, monkeypatch, backend: str | None, *, explicit: bool = False, source_skills: bool = False
):
    """The copy (switched to `backend`, or left on Claude) answers one message with fleet
    on. Returns (retained fleet_root operation keys, remembered refusal)."""
    from daimon.core.mcp_oauth import discovery
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.turn.admission import admit
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.core.turn.runtime_manifest import root_refusal
    from daimon.core.turn.runtimes import build_channel_runtime
    from daimon.core.turn_hold import RequesterOrg
    from daimon.core.turn_origin import current_origin_id
    from daimon.testing.turn_fakes import RecordingLifecycle, deps_for_pinned_mux
    from sqlalchemy import text

    from packages.core.tests.turn.test_gemini_codec import interaction
    from packages.core.tests.turn.test_gemini_runtime_transport import clients_for

    catalogue = await _chat_catalogue(native=backend is not None)

    async def listed(_url: str, _token: str) -> frozenset[str]:
        return catalogue

    monkeypatch.setattr(discovery, "list_mcp_tools", listed)
    source = chat.ma.agents["agent_daimon"]
    # As Managed Agents returns it: the model is an object, so the copy's source
    # carries its Claude model config (anthropic.model_config). Cheap models only: the
    # control's copy is a Haiku agent.
    model_id = source["model"]["id"] if isinstance(source["model"], dict) else source["model"]
    source["model"] = {"id": HAIKU if backend is None else model_id, "speed": "fast"}
    if source_skills:
        # As Daimon is set up: Anthropic's built-in skills on the Claude source.
        source["skills"] = [
            {"type": "anthropic", "skill_id": name, "version": "latest"}
            for name in ("xlsx", "pptx", "docx", "pdf")
        ]
    copy = await _fork_agent_impl(
        chat.runtime,
        chat.auth,
        "Daimon",
        "Daimon",
        expected_ma_agent_id="agent_daimon",
        new_copy_if_taken=True,
        origin_context_id=str(chat.origin.id),
    )
    if backend is None:
        # The Claude copy takes this thread the way a person hands it over.
        from daimon.adapters.mcp.tools.task_continuity import (
            _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
        )

        await _hand_off_task_impl(
            chat.runtime,
            chat.auth,
            origin_context_id=str(chat.origin.id),
            agent_id=copy.id,
            keep_provider=True,
        )
    if backend in NATIVE:
        target, model = backend, NATIVE[backend]
        preview = await _request_agent_backend_switch_impl(
            chat.runtime,
            chat.auth,
            agent_name=copy.name,
            target_backend=target,
            model=model,
            origin_context_id=str(chat.origin.id),
        )
        await _request_agent_backend_switch_impl(
            chat.runtime,
            chat.auth,
            agent_name=copy.name,
            target_backend=target,
            model=model,
            origin_context_id=str(chat.origin.id),
            expected_revision=preview.expected_revision,
            preview_digest=preview.preview_digest,
            accepted_drops=preview.declared_drops,
        )
    if explicit:
        # An explicit (stored) fleet grant on the copy, saved through the same backend
        # service the request_fleet_config card confirms: a root refusal then fails
        # the turn instead of quietly turning fleet off.
        from daimon.adapters.mcp.tools.fleet_config import (
            FleetConfigInput,
            configured_fleet_grant,
        )
        from daimon.core.authz import Subject
        from daimon.core.channel_backend import (
            AgentBackendService,
            BackendActor,
            load_backend_agent,
        )

        service = AgentBackendService(chat.runtime.session_factory, enabled=True)
        agent = await load_backend_agent(
            chat.runtime.client,
            chat.runtime.session_factory,
            tenant_id=chat.auth.tenant_id,
            agent_name=copy.name,
        )
        now = datetime.now(UTC)
        value = await configured_fleet_grant(
            FleetConfigInput.model_validate(
                {"max_workers": 3, "budget_usd": "1", "max_worker_seconds": 120}
            ),
            service,
            chat.runtime.client,
            chat.runtime.session_factory,
            agent,
            now=now,
        )
        preview = await service.preview_fleet_grant(agent, value, now=now)
        await service.set_fleet_grant(
            agent,
            value,
            preview.setting_revision,
            BackendActor(chat.auth.tenant_id, chat.auth.account_id, Subject(is_admin=True)),
            expected_preview_digest=preview.digest,
            now=now,
        )
    if backend is None:
        # The control: the copy stays on Claude, so its root is an MA root. The J2 fake MA reads
        # agents only; give its session the update, send and stream a turn needs.
        from mux.testing.drivers.anthropic.ma import (
            make_fake_memory_store_handler,
            send_events_response,
            sse_response,
        )

        from packages.core.tests.turn.conftest import make_agent_message, make_status_idle

        router = chat.trace.router
        # The Claude root replaces the fixture's session; MA ids are global in the
        # test database, so this run's must not collide with an earlier test's.
        run = uuid.uuid4().hex[:8]
        own = f"sess_daimon_{run}"
        chat.ma.sessions[own] = {**chat.ma.sessions.pop("sess_daimon"), "id": own}
        async with chat.runtime.session_factory.begin() as db:
            await db.execute(
                text(
                    "UPDATE thread_sessions SET ma_session_id = :own"
                    " WHERE ma_session_id = 'sess_daimon' AND tenant_id = :tenant"
                ),
                {"own": own, "tenant": chat.auth.tenant_id},
            )

        def create_session(call, match):
            body = json.loads(call.content)
            created = {
                **chat.ma.sessions[own],
                "id": f"sess_root_{run}_{len(chat.ma.sessions)}",
                "metadata": body.get("metadata") or {},
            }
            chat.ma.sessions[created["id"]] = created
            return httpx.Response(200, json=created)

        router.add("POST", r"/v1/sessions", create_session)
        memory = make_fake_memory_store_handler()
        router.add("GET", r"/v1/memory_stores.*", lambda call, match: memory(call))
        router.add("POST", r"/v1/memory_stores.*", lambda call, match: memory(call))
        router.add(
            "POST",
            r"/v1/sessions/([^/]+)/archive",
            lambda call, match: httpx.Response(
                200,
                json={
                    **chat.ma.sessions[match.group(1)],
                    "archived_at": datetime.now(UTC).isoformat(),
                },
            ),
        )
        vault = {
            "id": "vlt_j10",
            "type": "vault",
            "display_name": "j10",
            "metadata": None,
            "archived_at": None,
            "created_at": "2026-10-24T00:00:00Z",
        }
        router.add(
            "GET",
            r"/v1/vaults$",
            lambda call, match: httpx.Response(200, json={"data": [], "next_page": None}),
        )
        router.add(
            "POST",
            r"/v1/vaults$",
            lambda call, match: httpx.Response(
                200, json={**vault, "display_name": json.loads(call.content)["display_name"]}
            ),
        )

        def credential(call, match):
            body = json.loads(call.content)
            return httpx.Response(
                200,
                json={
                    "id": f"vcrd_{len(chat.trace.requests)}",
                    "type": "vault_credential",
                    "vault_id": "vlt_j10",
                    "metadata": body.get("metadata") or {},
                    "created_at": "2026-10-24T00:00:00Z",
                    "updated_at": "2026-10-24T00:00:00Z",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": body["auth"]["mcp_server_url"],
                    },
                },
            )

        router.add("POST", r"/v1/vaults/[^/]+/credentials$", credential)
        router.add(
            "GET",
            r"/v1/vaults/[^/]+/credentials",
            lambda call, match: httpx.Response(200, json={"data": [], "next_page": None}),
        )
        router.add(
            "POST",
            r"/v1/sessions/([^/]+)",
            lambda call, match: httpx.Response(200, json=chat.ma.sessions[match.group(1)]),
        )
        router.add(
            "POST", r"/v1/sessions/([^/]+)/events", lambda call, match: send_events_response()
        )
        router.add(
            "GET",
            r"/v1/sessions/([^/]+)/events",
            lambda call, match: httpx.Response(200, json={"data": [], "has_more": False}),
        )
        router.add(
            "GET",
            r"/v1/sessions/([^/]+)/events/stream",
            lambda call, match: sse_response(
                [
                    make_agent_message(event_id="root-answer", text="answer").model_dump(
                        mode="json"
                    ),
                    make_status_idle(event_id="root-idle").model_dump(mode="json"),
                ]
            ),
        )
    native: dict = {}
    posts: list = []
    openai_root = None
    if backend == "openai":
        from unittest.mock import AsyncMock

        from daimon.core.provider_keys import DEFAULT_KEY_ENV
        from daimon.core.turn import openai_deployment

        from packages.core.tests.turn.test_openai_full_source import FullWire

        monkeypatch.setenv(DEFAULT_KEY_ENV["openai"], "offline")
        # Off GCP there is no metadata server: name the deployment as staging would.
        monkeypatch.setattr(
            openai_deployment, "_deployment_id", AsyncMock(return_value="pymc-offline")
        )
        openai_root = HostedTemplates(FullWire(str(chat.auth.tenant_id)))
    root_wire = pytest.MonkeyPatch()

    def gemini(call: httpx.Request) -> httpx.Response:
        if call.url.path.startswith("/v1beta/agents"):
            if call.method == "POST":
                native.update(json.loads(call.content))
            return httpx.Response(200, json=native)
        if call.method == "POST":
            posts.append(json.loads(call.content))
            return httpx.Response(200, json=interaction(f"turn-{len(posts)}"))
        return httpx.Response(200, json=interaction(call.url.path.rsplit("/", 1)[-1]))

    sdk_clients = contextlib.AsyncExitStack()
    if backend == "gemini":
        # Patches httpx's clients globally, which the Claude control's MA client and a
        # worker's own provider clients must keep: only for this root turn.
        clients_for(root_wire, gemini)
    if backend == "openai":
        import mux.drivers.openai as openai_sdk
        from mux.drivers.openai.transport import SDKTransport
        from mux.testing.isolated_sdk import build_async_openai

        sdk = await sdk_clients.enter_async_context(
            build_async_openai(
                transport=httpx.MockTransport(openai_root.handle),
                api_key="offline",
                base_url="https://openai.invalid/v1",
            )
        )
        root_wire.setattr(openai_sdk, "configured_transport", lambda **kwargs: SDKTransport(sdk))
    sm = chat.runtime.session_factory
    deps = replace(
        deps_for_pinned_mux(chat.runtime.client, sm),
        agent_backends=True,
        fleet_enabled=True,
        deployment_default=chat.runtime.deployment_default,
        state_store=PostgresStateStore(sm),
        turn_runtimes={},
        turn_runtime_factory=build_channel_runtime,
        mcp=chat.runtime.settings.mcp,
    )
    # The copy's turn has its own origin naming it as the responder (production makes
    # one per turn), current for the whole turn, preparation included.
    from daimon.core.stores.domain import Role
    from daimon.core.stores.turn_origins import create_origin

    async with sm.begin() as db:
        origin = await create_origin(
            db,
            tenant_id=chat.auth.tenant_id,
            account_id=chat.auth.account_id,
            platform="discord",
            parent_channel_id="fresh-channel",
            thread_id="thread",
            responder_ma_agent_id=copy.id,
            responder_name=copy.name,
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN if chat.auth.is_admin else Role.USER,
            sender_foreign=False,
            shared_channel=False,
            now=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    token = current_origin_id.set(origin.id)
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
    assert admission.agent.id == (copy.id)
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
    # The bot's own first step for a thread message: commit the turn card intent, post
    # the card and record its message id (post_initial_turn_card), never a test-built row.
    from types import SimpleNamespace

    from daimon.adapters.discord.turn_card_recovery import post_initial_turn_card

    class FirstCard:
        def __init__(self, intent_id, record):
            self.record = record

        async def post_initial(self):
            await self.record(SimpleNamespace(id=1563400000000000000 + len(chat.ma.sessions)))

    intent, _ = await post_initial_turn_card(
        sm,
        tenant_id=chat.auth.tenant_id,
        thread_id="thread",
        channel_id="fresh-channel",
        make_lifecycle=FirstCard,
    )

    async def no_recovery():
        pytest.fail("the turn changed its admitted source/session")

    try:
        await run_prepared_turn(
            deps,
            prepared,
            tenant_id=chat.auth.tenant_id,
            platform="discord",
            thread_id="thread",
            external_user_id="123",
            user_message="compare X across a fleet",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            operation_key="j10-native-root",
            turn_card_intent_id=intent.id,
        )
    finally:
        current_origin_id.reset(token)
        root_wire.undo()
        await sdk_clients.aclose()
    async with sm() as db:
        keys = list(
            await db.scalars(
                text("SELECT key FROM operation WHERE key LIKE :k"),
                {"k": f"fleet_root:{origin.id}:%"},
            )
        )
    frozen = None
    if keys:
        from daimon.core.mux_backend import resource_scope
        from daimon.core.turn.runtime_manifest import read_frozen_root

        frozen, _ = await read_frozen_root(
            deps,
            resource_scope(
                tenant_id=str(chat.auth.tenant_id),
                account_id=str(chat.auth.account_id),
                authorization_id="fleet-origin:" + str(origin.id),
            ),
            origin_id=origin.id,
            now=datetime.now(UTC),
        )
    provider_agent = (
        native if backend == "gemini" else openai_root.wire.agent if backend == "openai" else {}
    )
    return keys, root_refusal(origin.id), prepared, frozen, deps, origin, provider_agent


@pytest.mark.parametrize("grant", ["default", "explicit"])
@pytest.mark.parametrize("backend", ["gemini", "openai", None])
async def test_a_native_agent_coordinates_a_fleet(fresh_chat, monkeypatch, backend, grant):
    """The Claude (Haiku) copy is the control: it froze before the fix too."""
    keys, refusal, prepared, frozen, _, _, provider_agent = await _coordinator_turn(
        fresh_chat, monkeypatch, backend, explicit=grant == "explicit"
    )
    if backend is not None:
        assert prepared.session_ref.provider == backend
        assert prepared.admission.backend_counterpart is not None
    assert refusal is None, refusal
    assert keys, "no fleet root was retained"
    # The root's own model is the one it runs on, so a worker that names no model runs
    # there too (not on the copy's original Claude model).
    if backend is not None:
        assert (frozen.spec.model.provider, frozen.spec.model.id) == (backend, NATIVE[backend])
    else:
        assert (frozen.spec.model.provider, frozen.spec.model.id) == ("anthropic", HAIKU)


async def _fleet_from_root(chat, monkeypatch, deps, origin, root):
    """The native root dispatches one worker that names no model, through the same
    fleet tool service its turn calls, and collects it. Returns (rows, collected)."""
    import mux.drivers.gemini as gemini_sdk
    from daimon.adapters.mcp.tools.fleet import HostFleetToolService
    from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
    from daimon.core.provider_keys import DEFAULT_KEY_ENV
    from daimon.core.stores.fleet import WorkerPool
    from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
    from mux.testing.isolated_sdk import build_gemini_client

    from packages.adapters.mcp.tests.tools.test_j4_named_agents_e2e import _settled_within
    from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import (
        GeminiCatalog,
        GeminiTransport,
    )

    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")
    gemini_wire = GeminiTransport()
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
    import mux.drivers.openai as openai_sdk
    from mux.drivers.openai.transport import SDKTransport
    from mux.testing.isolated_sdk import build_async_openai

    from packages.adapters.mcp.tests.tools.test_j10_fork_e2e import AstraWire

    astra = AstraWire()
    astra.session_id = f"native-openai-worker-{uuid.uuid4().hex[:8]}"
    astra.session_bodies = []
    clients = contextlib.AsyncExitStack()
    sdk = await clients.enter_async_context(
        build_async_openai(
            transport=httpx.MockTransport(astra.handle),
            api_key="offline",
            base_url="https://openai.invalid/v1",
        )
    )
    monkeypatch.setattr(openai_sdk, "configured_transport", lambda **kwargs: SDKTransport(sdk))
    ports = HostFleetPorts(
        deps.sessionmaker,
        markup=Decimal(1),
        deps=deps,
        pool=WorkerPool(max_concurrent_workers=2, max_concurrent_workers_per_tenant=2),
    )
    bridge = HostFleetToolService(FleetOrchestrator(ports), runtime=chat.runtime, deps=deps)
    from daimon.core.stores.domain import Role

    # The root's tool calls carry the turn's own identity: the subject it admitted.
    caller = replace(
        chat.auth,
        chat_agent_id=root.agent_id,
        is_admin=root.subject.is_admin,
        role=Role.ADMIN if root.subject.is_admin else Role.USER,
    )
    try:
        reply = dict(
            await bridge.spawn(
                caller,
                run_id=None,
                origin_context_id=origin.id,
                operation_key="W1",
                role="analyst",
                backend=None,
                agent=None,
                clone=None,
                clone_revision=None,
                overrides=None,
                session=None,
                budget_usd=None,
                deadline=None,
                max_turns=None,
                result_contract=None,
                reviewer_of=None,
                task="Compare X from your point of view.",
            )
        )
        assert reply["status"] == "pending", reply
        rows = await _settled_within(ports, root, 1, 30)
        collected = dict(
            await bridge.collect(caller, run_id=root.fleet.run_id, origin_context_id=origin.id)
        )
    finally:
        await ports.close()
        gemini_client.close()
        await gemini_client.aio.aclose()
        await clients.aclose()
    return rows, collected, gemini_wire, astra


@pytest.mark.parametrize("backend", ["gemini", "openai"])
async def test_a_native_root_dispatches_a_worker_and_collects_its_result(
    fresh_chat, monkeypatch, backend
):
    """Worker dispatch and the delivered result from a GPT/Gemini root (#1494 follow-up):
    a worker that names no model runs on the root's own model and is collected."""
    keys, refusal, _, frozen, deps, origin, _ = await _coordinator_turn(
        fresh_chat, monkeypatch, backend
    )
    assert refusal is None and keys
    rows, collected, gemini_wire, astra = await _fleet_from_root(
        fresh_chat, monkeypatch, deps, origin, frozen
    )
    ((row,),) = (rows,)
    assert (row.backend, row.model, row.status) == (backend, NATIVE[backend], "succeeded")
    assert collected["status"] == "ready" and len(collected["results"]) == 1
    # It ran on that provider's own wire, as itself: the task is its instructions.
    if backend == "gemini":
        (sent,) = gemini_wire.requests
        assert "Compare X from your point of view." in str(sent["system_instruction"])
    else:
        (body,) = astra.session_bodies
        assert body["agent"]["model"] == NATIVE["openai"]


@pytest.mark.parametrize("backend", ["openai", "gemini"])
async def test_a_native_root_declares_its_claude_sources_skills_lost(
    fresh_chat, monkeypatch, backend
):
    """Smoke a1d991fb (M07): a GPT copy of a Daimon with skills refused to freeze its root
    (field=skills). The root runs on its counterpart, which uses its provider's own skills
    (or none); it declares the Claude source's skills lost instead of claiming them."""
    keys, refusal, _, frozen, *_ = await _coordinator_turn(
        fresh_chat, monkeypatch, backend, source_skills=True
    )
    assert refusal is None, refusal
    assert keys
    assert (frozen.spec.model.provider, frozen.spec.model.id) == (backend, NATIVE[backend])
    assert not frozen.spec.skills and "skills" in frozen.spec.declared_drops

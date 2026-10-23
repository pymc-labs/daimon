"""J10 end to end, offline: a member forks the built-in agent across Opus, Astra and Gemini.

No runtime rows and no stored fleet grant: only code defaults. A NON-admin member in
a fresh channel says "fork yourself across opus, astra and argon (or flash) and compare
X"; the
root agent calls the real fleet_spawn tool once per model. From there everything is
the production path: grant -> plan -> spawn -> run_worker -> native dispatch, with
fake provider transports (Anthropic, OpenAI, Gemini), and on to the fleet card.
Any refusal on this path is a product bug, not a test detail.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import discord
import httpx
import pytest
from daimon.adapters.mcp.tools.fleet import HostFleetToolService
from daimon.core.authz import Subject, Surface, build_agent_ref
from daimon.core.channel_backend import load_backend_agent
from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.neutral_resources import WorkerPermissions
from daimon.core.scope import ResolvedConfig
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
from daimon.core.session_ports_compat import create_session_record
from daimon.core.sessions import project_session_resources
from daimon.core.stores import (
    access_policy,
    agent_memory_stores,
    fleet,
    mux_state,
    tenant_ledger,
    tenants,
    turn_card_intents,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.fleet import WorkerPool
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest
from daimon.core.turn.runtime_manifest import read_frozen_root
from daimon.core.turn.runtimes import build_channel_runtime
from daimon.core.turn_origin import turn_origin
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from daimon.testing.turn_fakes import deps_for_pinned_mux
from mux.contracts.resources import ProviderBinding
from mux.testing.drivers.anthropic.ma import MARouter, send_events_response, sse_response
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from mux.testing.drivers.gemini.fake import FakeTransport
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.adapters.mcp.tests.tools.test_fleet import AUTH, runtime
from packages.core.tests.test_fleet_permissions import POLICY, parent
from packages.core.tests.turn.conftest import make_agent_message, make_status_idle
from packages.core.tests.turn.test_frozen_root import run_root_input
from packages.core.tests.turn.test_openai_conversation_worker import ConversationWire
from packages.core.tests.turn.test_openai_host import Wire

#: The Gemini worker, on each model the smoke may use (Argon primary; Flash kept).
GEMINI = {"flash": "gemini-3.8-flash", "argon": "gemini-4-argon"}


@pytest.fixture
async def j10_root(
    db_preflight,
    db_committing_engine,
):
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    async with sm.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        await make_platform_principal(
            db, tenant=tenant, account=account, platform="discord", external_id="requester"
        )
        await access_policy.set_access_policy(db, tenant_id=tenant.id, policy=POLICY)
        # A funded server, as every real one is (an unfunded one is refused, rightly).
        await tenants.set_funding_mode(db, tenant_id=tenant.id, funding_mode="prepaid")
        await tenant_ledger.insert_entry(
            db,
            tenant_id=tenant.id,
            delta_usd=Decimal("10"),
            reason="trial_credit",
            idempotency_key=f"j10-credit:{tenant.id}",
        )
    async for value in root_turn_harness(sm, tenant.id, account.id):
        yield value


async def root_turn_harness(
    sm,
    tenant_id,
    account_id,
    *,
    channel_id: str | None = None,
    thread_id: str | None = None,
    root_answer: str | None = None,
):
    """A root agent's turn in one channel of one tenant, over fake provider APIs.

    `channel_id`/`thread_id` place it (J10's fixed place by default); `root_answer`
    is what the root agent says before it goes idle (nothing by default).
    """
    memory = True
    root = parent("anthropic")
    scope = resource_scope(
        tenant_id=str(tenant_id), account_id=str(account_id), authorization_id="admitted-turn"
    )
    place = root.fleet.place
    if channel_id is not None:
        place = replace(place, channel_id=thread_id or channel_id, parent_channel_id=channel_id)
        root = replace(root, fleet=replace(root.fleet, place=place))
    thread = root.fleet.root.model_copy(
        update={
            "channel": root.fleet.root.channel.model_copy(
                update={"tenant_id": str(tenant_id)}
                | ({} if channel_id is None else {"channel_id": channel_id})
            ),
        }
        | ({} if thread_id is None else {"thread_id": thread_id})
    )
    native = ma_agent(
        id="root-source",
        name="root",
        tenant_id=str(tenant_id),
        system="Root",
        model="claude-haiku-5-5",
    )
    environment = ma_environment(tenant_id=str(tenant_id))
    session_id = "root-session-" + str(uuid4())
    snapshot = ma_session(id=session_id, agent=native, environment_id=environment.id)
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/root-source",
        lambda request, match: httpx.Response(200, json=native.model_dump(mode="json")),
    )
    router.add(
        "GET",
        rf"/v1/environments/{environment.id}",
        lambda request, match: httpx.Response(200, json=environment.model_dump(mode="json")),
    )
    # The Opus worker: its own agent and session on the same fake Managed Agents API
    # (ids unique per test: the committing database keeps every test's journal).
    opus: dict = {}
    opus_session = f"opus-worker-session-{uuid4().hex[:8]}"

    def create_worker_agent(call, match):
        payload = json.loads(call.content)
        opus["agent"] = ma_agent(
            id="opus-worker",
            tenant_id=str(tenant_id),
            name=payload["name"],
            model=payload["model"],
            system=payload.get("system"),
            tools=payload.get("tools") or (),
            skills=payload.get("skills") or (),
        ).model_copy(update={"metadata": payload.get("metadata")})
        opus["created"] = payload
        return httpx.Response(200, json=opus["agent"].model_dump(mode="json"))

    def create_session(call, match):
        payload = json.loads(call.content)
        agent = payload["agent"]
        if (agent if isinstance(agent, str) else agent.get("id")) != "opus-worker":
            return httpx.Response(200, json=snapshot.model_dump(mode="json"))
        session_agent = opus["agent"]
        if isinstance(agent, dict):
            session_agent = session_agent.model_copy(
                update={
                    key: value
                    for key, value in agent.items()
                    if key in ("skills", "tools", "mcp_servers", "system")
                }
            )
        opus["session"] = ma_session(
            id=opus_session,
            agent=session_agent,
            environment_id=payload["environment_id"],
            metadata=payload["metadata"],
            resources=payload.get("resources", []),
        )
        return httpx.Response(200, json=opus["session"].model_dump(mode="json"))

    router.add("POST", r"/v1/agents", create_worker_agent)
    router.add(
        "GET",
        r"/v1/agents/opus-worker",
        lambda call, match: httpx.Response(
            200, json=opus["agent"].model_dump(mode="json", exclude_unset=True)
        ),
    )
    router.add("POST", r"/v1/sessions", create_session)
    router.add(
        "POST",
        rf"/v1/sessions/{opus_session}/archive",
        lambda call, match: httpx.Response(200, json=opus["session"].model_dump(mode="json")),
    )
    router.add(
        "GET",
        rf"/v1/sessions/{opus_session}",
        lambda call, match: httpx.Response(200, json=opus["session"].model_dump(mode="json")),
    )
    router.add(
        "POST",
        rf"/v1/sessions/{opus_session}/events",
        lambda call, match: send_events_response(),
    )
    router.add(
        "GET",
        rf"/v1/sessions/{opus_session}/events",
        lambda call, match: httpx.Response(200, json={"data": [], "has_more": False}),
    )
    router.add(
        "GET",
        rf"/v1/sessions/{opus_session}/events/stream",
        lambda call, match: sse_response(
            [
                make_agent_message(event_id="opus-output", text="opus says X").model_dump(
                    mode="json"
                ),
                make_status_idle(event_id="opus-idle").model_dump(mode="json"),
            ]
        ),
    )
    input_sent = asyncio.Event()

    def send(request, match):
        input_sent.set()
        return send_events_response()

    class Stream(httpx.AsyncByteStream):
        def __init__(self):
            input_sent.clear()

        async def __aiter__(self):
            await input_sent.wait()
            said = (
                []
                if root_answer is None
                else [
                    make_agent_message(
                        event_id="root-answer-" + str(uuid4()), text=root_answer
                    ).model_dump(mode="json")
                ]
            )
            response = sse_response(
                [
                    *said,
                    make_status_idle(event_id="root-idle-" + str(uuid4())).model_dump(mode="json"),
                ]
            )
            yield response.content

    router.add("POST", rf"/v1/sessions/{session_id}/events", send)
    router.add(
        "GET",
        rf"/v1/sessions/{session_id}/events",
        lambda request, match: httpx.Response(200, json={"data": [], "has_more": False}),
    )
    router.add(
        "GET",
        rf"/v1/sessions/{session_id}/events/stream",
        lambda request, match: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        ),
    )
    transport = ScriptedTransport(router=router)
    transport.opus = opus  # the Opus worker's create, for assertions
    async with transport.client() as client:
        actual = await retrieve_agent(client, native.id, scope=scope)
        captured = await load_backend_agent(
            client, sm, tenant_id=tenant_id, agent_name="root", actual_agent=actual
        )
        if memory:
            async with sm.begin() as db:
                await agent_memory_stores.insert_memory_store(
                    db,
                    tenant_id=tenant_id,
                    agent_id=captured.agent_id,
                    memory_store_id="warm-root-memory",
                )
        captured = await project_session_resources(
            captured,
            agent=actual,
            environment=environment,
            session_factory=sm,
            tenant_id=tenant_id,
            agent_uuid=captured.agent_id,
            account_id=account_id,
            resource_scope=scope,
            memory_read_only=True,
        )
        acknowledged = await create_session_record(
            client,
            scope=scope,
            agent=native.id,
            environment_id=environment.id,
            metadata=None,
            resources=None,
        )
        assert acknowledged.agent.id == actual.id
        spec = captured.effective_spec.model_copy(
            update={
                "model": root.spec.model,
                "extensions": None,
                "tools": (),
                "skills": (),
                "mcp_servers": (),
                "declared_drops": (),
            }
        )
        subject = Subject(platform_user_id="requester")
        grant = AdmissionGrant(
            tenant_id,
            subject,
            Surface.CHANNEL,
            root.fleet.place,
            build_agent_ref(actual.name, actual.metadata or {}),
            root.fleet.place,
            thread.channel.channel_id,
            thread.thread_id,
            False,
            platform="discord",
        )
        admission = Admission(
            account_id,
            actual,
            environment,
            ResolvedConfig(agent_name="root", environment_name="default"),
            grant=grant,
            origin_channel_id=thread.channel.channel_id,
            origin_thread_id=thread.thread_id,
        )
        async with sm.begin() as db:
            intent = await turn_card_intents.create_turn_card_intent(
                db,
                tenant_id=tenant_id,
                platform="discord",
                channel_id=thread.channel.channel_id,
                thread_id=thread.thread_id,
                turn_token=uuid4(),
            )
        async with turn_origin(
            sm,
            tenant_id=tenant_id,
            account_id=account_id,
            platform="discord",
            parent_channel_id=thread.channel.channel_id,
            thread_id=thread.thread_id,
            responder_ma_agent_id=native.id,
            responder_name="root",
            role=Role.USER,
            sender_foreign=False,
            shared_channel=False,
        ) as origin:
            root = replace(
                root,
                fleet=replace(
                    root.fleet, scope=scope, root=thread, origin_id=origin.id, account_id=account_id
                ),
                subject=subject,
                agent=grant.agent,
                spec=spec,
                base_system=spec.system,
                agent_id=captured.agent_id,
                environment=None,
                source_agents=(captured.source.ref,),
                source_revisions=((captured.source.ref, captured.source.revision),),
                limits=root.limits.model_copy(
                    update={"deadline": datetime.now(UTC) + timedelta(seconds=120)}
                ),
            )
            # J10: NO fleet grant is stored. The agent's fleet is the code default.
            store = mux_state.PostgresStateStore(sm)
            binding = ProviderBinding(
                id=str(uuid4()),
                thread=thread,
                provider="anthropic",
                profile="anthropic.managed_agents",
                native_refs={"session": acknowledged.id, "agent": actual.id},
                generation=1,
                config_revision=0,
                legacy_account_id=str(account_id),
            )
            await store.put_binding(binding, expected_generation=0)
            grants = [root]

            async def record(*, event):
                return None

            # Deployed settings: mux, channel and agent backends on; the production
            # runtime factory picks each provider's runtime from the code defaults.
            deps = replace(
                deps_for_pinned_mux(client, sm),
                fleet_enabled=True,
                fleet_root_grants=None,
                state_store=store,
                agent_backends=True,
                turn_runtime_factory=build_channel_runtime,
                turn_runtimes={},
            )
            backend = managed_agents(
                client, scope=scope, resources=frozenset({("session", acknowledged.id)})
            )
            prepared = PreparedTurn(
                admission,
                acknowledged.id,
                None,
                None,
                False,
                account_id,
                record,
                backend=backend,
                session_ref=resource_ref(backend, "session", acknowledged.id, scope=scope),
                _scope=scope,
                _source=captured,
                _preparation=ProviderPreparationRequest(
                    deps,
                    admission,
                    scope,
                    tenant_id,
                    "discord",
                    "requester",
                    thread.thread_id,
                    account_id,
                    False,
                    DEFAULT_MA_CAPABILITIES,
                    None,
                    root.limits.deadline,
                    lambda: datetime.now(UTC),
                ),
            )
            yield deps, prepared, grants, intent, origin, scope, binding, transport


async def _settled(ports, root, count):
    async with asyncio.timeout(30):
        while True:
            _, rows = await ports.read_run(root.fleet.scope, root.fleet.run_id)
            if len(rows) == count and all(
                row.status in ("failed", "succeeded", "cancelled") for row in rows
            ):
                return rows
            await asyncio.sleep(0.05)


class AstraWire(ConversationWire):
    """OpenAI's agents API as an Astra conversation worker sees it."""

    def handle(self, call):
        path = call.url.path.removeprefix("/v1")
        if path == "/agents/sessions" and call.method == "POST":
            self.posts.append(call)
            self.requests.append(call)
            body = json.loads(call.content)
            self.session_bodies.append(body)
            internal = httpx.Request(
                "POST",
                "https://openai.invalid/v1/agents/sessions/" + self.session_id + "/events",
                headers={"OpenAI-Beta": "agents=v1", "Idempotency-Key": "fixture:send"},
                json={"events": [{"type": "agent.session.input", "input": body["input"]}]},
            )
            Wire.handle(self, internal)
            self.requests.remove(internal)
            return httpx.Response(200, json=self.session())
        return super().handle(call)

    def session(self):
        # The session answers with exactly what was asked: the admitted model, and a
        # spend_control only if one was sent.
        body = self.session_bodies[-1]
        value = {**super().session(), "agent": {**super().session()["agent"], **body["agent"]}}
        value.pop("spend_control", None)
        if "spend_control" in body:
            value["spend_control"] = body["spend_control"]
        return value


class GeminiCatalog:
    """The Gemini agent catalog: a worker's agent is created, then read back."""

    def __init__(self):
        self.agents: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []

    def handle(self, call: httpx.Request) -> httpx.Response:
        self.requests.append(call)
        if call.method == "POST" and call.url.path == "/v1beta/agents":
            agent = json.loads(call.content)
            self.agents[agent["id"]] = agent
            return httpx.Response(200, json=agent)
        name = call.url.path.removeprefix("/v1beta/agents/")
        if call.method == "GET" and name in self.agents:
            return httpx.Response(200, json=self.agents[name])
        return httpx.Response(404, json={"error": {"message": "not found"}})


class GeminiTransport(FakeTransport):
    """Gemini interactions: every create completes at once with one answer."""

    async def count_input_tokens(self, request):
        self.counted.append(json.loads(json.dumps(dict(request))))
        return 100

    async def create(self, request):
        self.requests.append(json.loads(json.dumps(dict(request))))
        response = {
            "id": f"gemini-{len(self.requests)}",
            "status": "completed",
            "created": "2026-10-23T15:00:00Z",
            "updated": "2026-10-23T15:00:05Z",
            "tools": request.get("tools", []),
            "steps": [
                {"type": "model_output", "content": [{"type": "text", "text": "gemini says X"}]}
            ],
            "usage": {"total_input_tokens": 10, "total_output_tokens": 2},
        }
        self.saved[response["id"]] = response
        return json.loads(json.dumps(response))


@dataclass
class J10:
    """What one J10 run produced, as each provider and the card saw it."""

    root: WorkerPermissions
    replies: list[dict]
    settled: tuple
    collected: dict
    gate: tuple
    run_status: str
    card: list[str]
    opus_agent: dict
    astra_sessions: list[dict]
    gemini_requests: list[dict]
    gemini_counted: list[dict]


async def run_j10(
    j10_root, monkeypatch, gemini: str, *, lifecycle=None, message: str | None = None
) -> J10:
    """The member's one message, then the root's three fleet_spawn calls and fleet_collect."""
    import mux.drivers.gemini as gemini_sdk
    import mux.drivers.openai as openai_sdk
    from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCard
    from daimon.core.provider_keys import DEFAULT_KEY_ENV
    from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
    from mux.drivers.openai.transport import SDKTransport
    from mux.testing.isolated_sdk import build_async_openai, build_gemini_client

    planned = ["opus", "astra", gemini]
    deps, prepared, _, _, seed, scope, _, transport = j10_root
    # The deployment keys each provider; there are no runtime rows, so the code
    # defaults serve every worker.
    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")
    wire = AstraWire()
    wire.session_id = f"native-openai-session-{uuid4().hex[:8]}"
    wire.session_bodies = []
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
    try:
        async with (
            build_async_openai(
                transport=httpx.MockTransport(wire.handle),
                api_key="offline",
                base_url="https://openai.invalid/v1",
            ) as sdk,
            turn_origin(
                deps.sessionmaker,
                tenant_id=seed.tenant_id,
                account_id=seed.account_id,
                platform="discord",
                parent_channel_id=seed.parent_channel_id,
                thread_id=seed.thread_id,
                responder_ma_agent_id=prepared.admission.agent.id,
                responder_name=prepared.admission.agent.name,
                role=Role.USER,
                sender_foreign=False,
                shared_channel=False,
                keep_fleet_root=True,
            ) as origin,
        ):
            monkeypatch.setattr(
                openai_sdk, "configured_transport", lambda **kwargs: SDKTransport(sdk)
            )
            # The member's message in the fresh channel: a real root turn freezes the root.
            await run_root_input(
                (*j10_root[:4], origin, *j10_root[5:]), lifecycle=lifecycle, message=message
            )
            root, policy = await read_frozen_root(
                deps, scope, origin_id=origin.id, now=datetime.now(UTC)
            )
            ports = HostFleetPorts(
                deps.sessionmaker,
                markup=Decimal(1),
                deps=deps,
                pool=WorkerPool(max_concurrent_workers=4, max_concurrent_workers_per_tenant=4),
            )
            owner = FleetOrchestrator(ports)
            bridge = HostFleetToolService(
                owner,
                runtime=replace(
                    runtime(), session_factory=deps.sessionmaker, client=deps.anthropic
                ),
                deps=deps,
            )
            # The root agent's own MCP identity: a member, never an admin.
            caller = replace(
                AUTH,
                tenant_id=origin.tenant_id,
                account_id=origin.account_id,
                role=Role.USER,
                chat_agent_id=root.agent_id,
            )
            try:
                replies = [
                    dict(
                        await bridge.spawn(
                            caller,
                            run_id=None,
                            origin_context_id=origin.id,
                            operation_key=key,
                            role=f"compare-{model}",
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
                            task="Compare X from your model's point of view.",
                            model=model,
                            planned_models=planned,
                        )
                    )
                    for key, model in zip(("W1", "W2", "W3"), planned, strict=True)
                ]
                settled = await _settled(ports, root, len(replies))
                collected = dict(
                    await bridge.collect(
                        caller, run_id=root.fleet.run_id, origin_context_id=origin.id
                    )
                )
                shown: list = []

                async def show(*args, **kwargs):
                    shown.append(kwargs["view"])
                    return None

                async def load_parent(interaction):
                    return root, policy

                card = DiscordFleetCard(
                    run_id=root.fleet.run_id,
                    service=owner,
                    load_parent=load_parent,
                    send=show,
                    edit=show,
                )
                assert await card.refresh()
                # The root's final answer follows the collect: the card ends in its
                # final state and the run settles.
                gate = await card.summary_gate(render=True, seen=True)
                final_run, _ = await fleet.run_status(
                    deps.sessionmaker, tenant_id=origin.tenant_id, run_id=root.fleet.run_id
                )
            finally:
                await ports.close()
    finally:
        gemini_client.close()
        await gemini_client.aio.aclose()
    return J10(
        root=root,
        replies=replies,
        settled=tuple(settled),
        collected=collected,
        gate=gate,
        run_status=final_run.status,
        card=[
            item.content
            for item in shown[-1].walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        ],
        opus_agent=transport_opus(j10_root),
        astra_sessions=wire.session_bodies,
        gemini_requests=gemini_wire.requests,
        gemini_counted=gemini_wire.counted,
    )


def transport_opus(j10_root) -> dict:
    return j10_root[7].opus.get("created", {})


@pytest.mark.parametrize("gemini", sorted(GEMINI))
async def test_a_member_forks_the_default_agent_across_opus_astra_and_gemini(
    j10_root, monkeypatch, gemini
):
    seen = await run_j10(j10_root, monkeypatch, gemini)
    # A member, in a fresh channel, with nothing set up: the code-default fleet.
    assert not seen.root.subject.is_admin and seen.root.subject.platform_user_id == "requester"
    # All three were admitted, dispatched, and finished.
    assert [reply["status"] for reply in seen.replies] == ["pending"] * 3
    assert [(row.backend, row.model, row.status) for row in seen.settled] == [
        ("anthropic", "claude-opus-5-5", "succeeded"),
        ("openai", "gpt-6-astra", "succeeded"),
        ("gemini", GEMINI[gemini], "succeeded"),
    ]
    assert all(row.error_code is None and row.result is not None for row in seen.settled)
    # Each provider got its worker, on the model the member named.
    opus = seen.opus_agent["model"]
    assert (opus if isinstance(opus, str) else opus["id"]) == "claude-opus-5-5"
    (astra,) = seen.astra_sessions
    assert astra["agent"]["model"] == "gpt-6-astra"
    assert "spend_control" not in astra  # Astra runs with no native cap (#1394/#1398)
    (sent,) = seen.gemini_requests
    # A worker sends a capped MODEL request (no agent config), where an empty tool list
    # is correct; the never-empty sentinel is for agent requests (#1428).
    assert sent["model"] == GEMINI[gemini] and "tools" in sent and "agent_config" not in sent
    if gemini == "argon":
        assert seen.gemini_counted  # Argon's bounded envelope was sized before the send
    # fleet_collect: three results, one per worker.
    assert seen.collected["status"] == "ready"
    # J3/J10: the results come back WITH what the run cost, for the reply to say.
    assert re.fullmatch(r"This cost (\$\d+\.\d{2}|less than \$0\.01)\.", seen.collected["cost"])
    assert sorted(item["result"] for item in seen.collected["results"]) == [
        "answer turn-1",
        "gemini says X",
        "opus says X",
    ]
    # The fleet card: every worker, its model, and succeeded.
    text = "\n".join(seen.card)
    # The root may give its answer, and the card ends collected, the run completed.
    assert seen.gate == (True, None)
    assert seen.card[0] == "**Fleet — collected**" and seen.run_status == "completed"
    assert any("Cost $" in line or "Cost less than $0.01" in line for line in seen.card), seen.card
    for model in ("claude-opus-5-5", "gpt-6-astra", GEMINI[gemini]):
        assert f"{model} — succeeded" in text
    # Nothing a worker lacks goes unsaid: each loss of the root's own setup is named
    # on its card line, and a dropped login (when there is one) in the spawn reply.
    words = {
        "mcp": "its connected tools",
        "files": "its files",
        "repository": "its repository",
        "memory": "its memory",
    }
    for row in seen.settled:
        (line,) = [line for line in text.splitlines() if f"/ {row.model} —" in line]
        for drop in row.spec.get("declared_drops") or ():
            if drop in words:
                assert words[drop] in line, (drop, line)
    without = getattr(seen.root, "workers_run_without", ())
    for reply in seen.replies:
        assert list(reply.get("workers_run_without", [])) == list(without)

"""Actual SDK requests, real admission/preparation/billing and Discord refusal."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.channel_backend import current_backend
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.mux_backend import TurnRuntime
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import identity
from daimon.core.stores.access_policy import (
    load_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.admission import AdmissionObserver, admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.gemini import (
    GeminiDeployment,
    GeminiJournal,
    GeminiTransportRuntime,
    GeminiUsageRuntime,
)
from daimon.core.turn.openai_host import OpenAIHostRuntime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest
from daimon.testing.admin_bindings import AdminBindings, provision_admin_run
from daimon.testing.admission_evidence import AdmissionEffects
from daimon.testing.channel_accounting import ControlledTurnPricing
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.new15_bindings import New15Bindings, New15Run
from daimon.testing.outcome_oracle import LogObservation, VisibleText
from daimon.testing.turn_router import build_turn_router
from mux.contracts.ids import ChannelRef, ModelRef, Provider, ResourceRef, Revision, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
from mux.drivers.gemini import NO_TOOLS_TOOL
from mux.drivers.gemini.flash import FlashAttempt
from mux.drivers.gemini.transport import SDKTransport as GeminiSDK
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport as OpenAISDK
from mux.testing.drivers.anthropic.ma import json_body
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from mux.testing.drivers.gemini.fake import MemoryStorage
from mux.testing.isolated_sdk import build_async_openai, build_gemini_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs
from structlog.typing import EventDict

pytest_plugins = ["daimon.testing.db"]
SOURCE = Path(__file__).parent / "fixtures" / "new15" / "QA-NEW15-CHANNEL-BUDGET-EXHAUSTED.yaml"
SESSION_ID = "native-openai-session"


class Stream(httpx.AsyncByteStream):
    def __init__(self, wire: Wire) -> None:
        self.wire = wire
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await self.wire.sent.wait()
        root = self.wire.turns[-1]
        for event in (
            {
                "type": "agent.session.turn.in_progress",
                "event_id": root["id"] + ":running",
                "turn": {**root, "status": "in_progress"},
            },
            {
                "type": "agent.session.turn.item.done",
                "event_id": root["id"] + ":answer",
                "turn_id": root["id"],
                "item": self.wire.items[-1],
            },
            {
                "type": "agent.session.turn.completed",
                "event_id": root["id"] + ":done",
                "turn": {**root, "status": "completed"},
            },
        ):
            event["session_id"] = SESSION_ID
            yield ("data: " + json.dumps(event) + "\n\n").encode()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.turns: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.sent = asyncio.Event()
        self.sources: list[Stream] = []
        self.fail_create = False
        self.bad_model = False
        self.cancelled = False
        self.saved_delegation = False
        self.session_delegation = False
        self.missing_usage = False
        self.expected_spend_limit = None
        self.tenant = ""

    def session(self) -> dict[str, Any]:
        return {
            "id": SESSION_ID,
            "agent": {
                "id": "native-agent",
                "model": "different" if self.bad_model else "gpt-6-luna",
                "multi_agent": {"enabled": self.session_delegation},
            },
            "environment": {"type": "openai_hosted", "id": "native-environment"},
            "metadata": {"mux_tenant": self.tenant},
            "status": "idle",
            "created_at": 0,
        }

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["OpenAI-Beta"] == "agents=v1"
        path = req.url.path.removeprefix("/v1")
        if path == "/agents/native-agent":
            return httpx.Response(
                200,
                json={
                    "id": "native-agent",
                    "metadata": {"mux_tenant": self.tenant},
                    "multi_agent": {"enabled": self.saved_delegation},
                },
            )
        if path == "/agents/sessions" and req.method == "POST":
            assert req.headers["Idempotency-Key"].startswith("openai:prepare:")
            body = json.loads(req.content)
            assert body["agent"] == {"model": "gpt-6-luna", "multi_agent": {"enabled": False}}
            assert body["environment"]["container_size"] == "small"
            if self.expected_spend_limit is None:
                assert "spend_control" not in body
            else:
                assert body["spend_control"] == {"limit": self.expected_spend_limit}
            if self.fail_create:
                raise httpx.ReadError("offline lost acknowledgment")
            return httpx.Response(200, json=self.session())
        assert path.startswith("/agents/sessions/" + SESSION_ID)
        if path.endswith("/events"):
            if req.method == "GET":
                assert req.url.params["stream"] == "true"
                assert req.headers["Accept"] == "text/event-stream"
                self.sent = asyncio.Event()
                stream = Stream(self)
                self.sources.append(stream)
                return httpx.Response(
                    200, stream=stream, headers={"Content-Type": "text/event-stream"}
                )
            assert (
                "send" in req.headers["Idempotency-Key"]
                or "cancel" in req.headers["Idempotency-Key"]
            )
            body = json.loads(req.content)
            if body["events"][0]["type"] == "agent.session.input.cancel":
                self.cancelled = True
            else:
                root = f"turn-{len(self.turns) + 1}"
                text = body["events"][0]["input"][0]["content"][0]["text"]
                self.turns.append(
                    {
                        "id": root,
                        "session_id": SESSION_ID,
                        "agent_id": "native-agent",
                        "subagent_id": None,
                        "status": "completed",
                        "created_at": 0,
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 50,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 0},
                        },
                    }
                )
                if self.missing_usage:
                    self.turns[-1].pop("usage")
                self.items.extend(
                    [
                        {
                            "id": root + ":input",
                            "turn_id": root,
                            "type": "message",
                            "role": "user",
                            "status": "completed",
                            "content": [{"type": "input_text", "text": text}],
                        },
                        {
                            "id": root + ":answer",
                            "turn_id": root,
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "TINY-OK"}],
                        },
                    ]
                )
            self.sent.set()
            return httpx.Response(202, json={})
        if path.endswith("/turns") or path.endswith("/items"):
            values = self.turns if path.endswith("/turns") else self.items
            return httpx.Response(
                200,
                json={
                    "data": values,
                    "has_more": False,
                    "last_id": values[-1]["id"] if values else None,
                },
            )
        if "/turns/" in path:
            return httpx.Response(
                200,
                json={**self.turns[-1], "status": "cancelled" if self.cancelled else "completed"},
            )
        return httpx.Response(200, json=self.session())


class FlashAccounting:
    def __init__(self) -> None:
        self.receipts: list[FlashAttempt] = []

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        pass

    async def after_attempt(self, *, receipt: FlashAttempt) -> None:
        self.receipts.append(receipt)


class GeminiWire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.missing_usage = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path.endswith(("/interactions", "/interactions/native-gemini-root"))
        assert request.method in {"POST", "GET"}
        if request.method == "POST":
            body = json_body(request)
            # An agent request: the inert no-tools sentinel, never [] (built-ins).
            assert body["tools"] == [NO_TOOLS_TOOL]
            assert body["agent_config"]["model"] == "gemini-3.8-flash"
        # Gemini workspaces expire seven days after creation: stamp it now.
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return httpx.Response(
            200,
            json={
                "id": "native-gemini-root",
                "created": stamp,
                "updated": stamp,
                "status": "completed",
                "environment_id": "project",
                "steps": [
                    {"type": "model_output", "content": [{"type": "text", "text": "TINY-OK"}]}
                ],
                "usageMetadata": None
                if self.missing_usage
                else {
                    "promptTokenCount": 100,
                    "cachedContentTokenCount": 0,
                    "candidatesTokenCount": 50,
                    "thoughtsTokenCount": 0,
                },
            },
        )


@pytest.fixture(params=["anthropic", "openai", "gemini"])
async def admin(
    request: pytest.FixtureRequest,
    db_preflight: DatabasePreflight,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AdminBindings]:
    require_owned_sessionmaker(db_session_factory)
    bound = await provision_admin_run(
        db_session_factory,
        workspace_id="100",
        backend=cast(Provider, request.param),
        channels=frozenset({"101"}),
        trace=ScriptedTransport(),
    )
    tenant_id = uuid.UUID(bound.scope.tenant_id)
    async with policy_write_transaction(db_session_factory, tenant_id=tenant_id) as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if bound.backend not in policy.enabled_backends:
            await set_access_policy(
                session,
                tenant_id=tenant_id,
                policy=type(policy).model_validate(
                    {
                        **policy.model_dump(),
                        "enabled_backends": (*policy.enabled_backends, bound.backend),
                    }
                ),
            )
    bound.default = DeploymentDefault(agent_name="test-agent", environment_name="test-env")
    try:
        yield bound
    finally:
        await bound.close()


def loop_clock() -> float:
    """The running loop's clock: every capture timestamp uses this one timeline."""
    return asyncio.get_running_loop().time()


async def setup_join(
    admin: AdminBindings,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing_usage: bool = False,
) -> tuple[New15Bindings, Any, Any, DiscordSurfaceHarness]:
    async with admin.sessions() as db, db.begin():
        principal = await identity.get_or_create_platform_principal(
            db, tenant_id=uuid.UUID(admin.scope.tenant_id), platform="discord", external_id="103"
        )
    scope = Scope(
        tenant_id=admin.scope.tenant_id,
        account_id=str(principal.account_id),
        principal_id=str(principal.id),
        authorization_id="owned-new15-member",
    )
    await admin.configure_channel("101")
    credit = await admin.execute(
        {
            "do": "admin",
            "tool": "cli",
            "args": "daimon tenants credit discord {guild_id} 10 --note offline-new15",
        },
        {},
    )
    assert credit.status == "executed"
    model = {"anthropic": "claude-haiku-4-5", "openai": "gpt-6-luna", "gemini": "gemini-3.8-flash"}[
        admin.backend
    ]
    # Ancillary Anthropic resources are independently explicit SDK HTTP fixtures,
    # not converted/native OpenAI/Gemini resource ids.
    router = build_turn_router(
        admin.scope.tenant_id,
        model_id="claude-haiku-4-5",
        agent_text="TINY-OK",
        session_id="native-anthropic-session",
        usage_event_id=None if missing_usage else "evt_parity_usage",
    )
    # Main #824 (0733181847dc775bda444d51c2467016780b8d5d) adds a
    # post-turn history GET. The shared router replays the actual emitted usage
    # span, or an empty filtered page when this fixture omits usage.

    def create(request: httpx.Request, _match: object) -> httpx.Response:
        body = json_body(request)
        saved = ma_session(
            id="native-anthropic-session",
            agent_id="ag_parity_test",
            model="claude-haiku-4-5",
            environment_id="env_parity_test",
            metadata=body["metadata"],
            resources=body.get("resources", []),
        )
        router.add_session(saved)
        return httpx.Response(200, json=saved.model_dump(mode="json"))

    def memory(request: httpx.Request, _match: object) -> httpx.Response:
        body = json_body(request)
        return httpx.Response(
            200,
            json={
                "id": "native-owned-memory",
                "type": "memory_store",
                "name": body["name"],
                "created_at": "2026-10-11T00:00:00Z",
                "updated_at": "2026-10-11T00:00:00Z",
                "description": body.get("description"),
                "metadata": body.get("metadata", {}),
                "archived_at": None,
            },
        )

    router.add("POST", r"/v1/memory_stores", memory)
    router.add("POST", r"/v1/sessions", create)
    admin.trace.router = router
    store = PostgresStateStore(admin.sessions)
    deps = TurnDeps(
        anthropic=admin.anthropic,
        sessionmaker=admin.sessions,
        deployment_default=admin.default,
        resolver_cache=new_resolver_cache(),
        defaults_root=root,
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal(1),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        turn_path="mux",
        channel_backends=True,
        state_store=store,
    )
    sdk: Any = None
    wire: Any = None
    if admin.backend == "anthropic":
        pricing = ControlledTurnPricing(
            "MOCK-100-USD-per-million-input-output",
            model,
            legacy_rates=ModelRates(100, 100, 100, 100),
        )
    else:
        price = ProviderPrice(
            provider=admin.backend,
            model=model,
            checked_on=datetime.now(UTC).date(),
            input=Decimal(100),
            output=Decimal(100),
            cache_read=Decimal(100),
        )
        pricing = ControlledTurnPricing(
            "MOCK-100-USD-per-million-and-zero-offline-infrastructure",
            model,
            provider_price=price,
            infrastructure_usd=Decimal(0),
        )
        async with admin.sessions() as db:
            revision = await current_backend(
                db, ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="101")
            )
        assert revision is not None
        if admin.backend == "openai":
            wire = Wire()
            wire.tenant = scope.tenant_id
            wire.missing_usage = missing_usage
            sdk = build_async_openai(transport=httpx.MockTransport(wire.handle))

            async def plan(request: ProviderPreparationRequest) -> SessionSpec:
                return SessionSpec(
                    agent=ResourceRef(
                        id="native-agent",
                        kind="agent",
                        provider="openai",
                        account_scope_id="project",
                        tenant_id=request.scope.tenant_id,
                        account_id=request.scope.account_id,
                    ),
                    agent_revision=Revision(local=0),
                    config_revision=revision.local,
                )

            async def infrastructure(observation: object) -> Decimal:
                return Decimal(0)

            runtime = OpenAIHostRuntime(
                lambda config, scope: OpenAISDK(sdk),
                OpenAIRecoveryJournal(),
                OpenAIUsageRevisions(),
                account_scope_id="project",
                session_plan=plan,
                authorization=lambda given, kind, id_: (
                    given.tenant_id == scope.tenant_id
                    and given.account_id == scope.account_id
                    and (kind, id_)
                    in {("agent", "native-agent"), ("session", None), ("session", SESSION_ID)}
                ),
                controls=SessionControls(
                    model="gpt-6-luna", multi_agent_enabled=False, container_size="small"
                ),
                price=price,
                infrastructure=infrastructure,
            )
        else:
            wire = GeminiWire()
            wire.missing_usage = missing_usage
            sdk = build_gemini_client(transport=httpx.MockTransport(wire.handle))
            runtime = TurnRuntime(
                lambda config, scope: GeminiTransportRuntime(GeminiSDK(sdk)),
                GeminiJournal(
                    MemoryStorage(),
                    store,
                    GeminiDeployment(
                        revision.digest,
                        revision.local,
                        "project",
                        AgentSpec(name="inline-owned", model=ModelRef(provider="gemini", id=model)),
                        EnvironmentSpec(name="inline-owned"),
                    ),
                ),
                GeminiUsageRuntime(FlashAccounting(), {model: price}, Decimal(0)),
            )
        deps = replace(deps, turn_runtimes={revision.profile: runtime})
    gateways: list[Any] = []

    def surface(prepared: PreparedTurn) -> DiscordSurfaceHarness:
        return DiscordSurfaceHarness(
            evidence_id="new15:first",
            session_id=prepared.ma_session_id,
            root_turn_id="first-offline-invocation",
            model_id=model,
            clock=loop_clock,
        )

    # Independent SDK gateway for the denied attempt: no provider root/session.
    denial_surface = DiscordSurfaceHarness(
        evidence_id="new15:second-platform-only",
        session_id="platform-only",
        root_turn_id="platform-only",
        clock=loop_clock,
    )
    gateways.append(denial_surface)
    state = denial_surface.client._connection  # pyright: ignore[reportPrivateUsage]
    item = discord.Message(
        state=state,
        channel=denial_surface.thread,
        data=cast(
            Any,
            {
                "id": "1400000000000000000",
                "channel_id": "102",
                "content": "<@999> QA budget 2: reply with only TINY-AGAIN. No tools.",
                "author": {"id": "103", "username": "caller", "discriminator": "0", "avatar": None},
                "attachments": [],
                "embeds": [],
                "mentions": [],
                "mention_roles": [],
                "timestamp": "2026-10-11T00:00:00+00:00",
                "edited_timestamp": None,
                "type": 0,
                "pinned": False,
                "mention_everyone": False,
                "tts": False,
            },
        ),
    )
    bot = cast(
        DaimonBot,
        SimpleNamespace(
            user=object(),
            _ingress_clock=None,
            runtime=SimpleNamespace(
                turn_deps=deps,
                sessionmaker=deps.sessionmaker,
                settings=SimpleNamespace(discord=None),
            ),
            _wait_for_orphan_recovery=AsyncMock(),
        ),
    )

    bot._orchestrate_observed_guarded = MethodType(DaimonBot._orchestrate_observed_guarded, bot)  # pyright: ignore[reportPrivateUsage]

    async def denial(text: str, observer: AdmissionObserver) -> None:
        assert text == "QA budget 2: reply with only TINY-AGAIN. No tools."

        async def observed(*args: Any, **kwargs: Any) -> object:
            return await admit(*args, **kwargs, observer=observer)

        monkeypatch.setattr("daimon.adapters.discord.bot.admit", observed)
        await DaimonBot._orchestrate_observed(bot, item, "100", uuid.UUID(scope.tenant_id))  # pyright: ignore[reportPrivateUsage]

    def sends() -> int:
        ancillary = sum(
            r.method == "POST" and (r.path == "/v1/sessions" or r.path.endswith("/events"))
            for r in admin.trace.requests
        )
        return ancillary + (
            sum(r.method == "POST" for r in wire.requests) if wire is not None else 0
        )

    return (
        New15Bindings(
            admin,
            deps,
            scope,
            "101",
            "102",
            "103",
            pricing,
            surface,
            denial,
            lambda: AdmissionEffects(),
            sends,
        ),
        sdk,
        wire,
        denial_surface,
    )


async def capture_run(
    admin: AdminBindings,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing_usage: bool = False,
    after_denial: Callable[[DiscordSurfaceHarness], None] | None = None,
) -> New15Run:
    join, sdk, wire, surface = await setup_join(
        admin, root, monkeypatch, missing_usage=missing_usage
    )

    def stamp(_logger: object, _method: str, entry: EventDict) -> EventDict:
        entry["captured_s"] = loop_clock()
        return entry

    # The authored 20 s observation window elapses on the event loop's own
    # clock, which is advanced in small steps with a real slice of I/O between
    # steps. Timers the system under test armed (asyncio.sleep, call_later) fire
    # in order at their due loop times, and every capture timestamp reads that
    # same clock, so the window is covered by the timeline the evidence cites,
    # not by wall time. Work that can only run in wall time (threads,
    # subprocesses) cannot be advanced, so a new one fails the run.
    loop = asyncio.get_running_loop()
    real_time = loop.time
    skew = 0.0
    before: set[asyncio.Task[Any]] = set()
    threads_before: set[threading.Thread] = set()
    denial = join.invoke_denial

    async def tracked_denial(text: str, observer: AdmissionObserver) -> object:
        # Taken as the denial starts: anything older than this is not "spawned
        # by the denial", but its timers still run on the loop clock.
        before.update(asyncio.all_tasks())
        threads_before.update(threading.enumerate())
        result = await denial(text, observer)
        if after_denial is not None:
            after_denial(surface)
        return result

    async def virtual_sleep(seconds: float) -> None:
        nonlocal skew
        advanced = 0.0
        while advanced < seconds:
            await asyncio.sleep(0.001)  # real time for ready callbacks and I/O
            step = min(0.25, seconds - advanced)
            skew += step
            advanced += step
        await asyncio.sleep(0.001)  # timers due exactly at the window's end
        me = asyncio.current_task()
        spawned = {t for t in asyncio.all_tasks() - before if t is not me}
        if spawned:
            _, pending = await asyncio.wait(spawned, timeout=2)
            if pending:
                raise AssertionError(
                    "the denial left work running past the observation window: "
                    + ", ".join(sorted(repr(t.get_coro()) for t in pending))
                )
        threads = [
            t
            for t in threading.enumerate()
            if t.is_alive() and t not in threads_before and not t.name.startswith("asyncio_")
        ]
        if threads:
            raise AssertionError(f"the denial started threads that cannot be advanced: {threads}")

    join.invoke_denial = tracked_denial
    join.clock = loop_clock
    join.sleep = virtual_sleep

    try:
        with capture_logs(processors=[stamp]) as logs:

            def effects() -> AdmissionEffects:
                return AdmissionEffects(
                    notices=tuple(
                        VisibleText(
                            evidence_id=e.evidence_id,
                            order=i,
                            observed_s=e.message.observed_s,
                            message_id=e.message.message_id,
                            text="\n".join(
                                t for t in (e.message.content, *e.message.embed_texts) if t
                            ),
                        )
                        for i, e in enumerate(surface.gateway.events)
                    ),
                    logs=tuple(
                        LogObservation(
                            evidence_id=f"new15:log:{i}",
                            order=i,
                            observed_s=e["captured_s"],
                            event=e["event"],
                            fields={
                                k: v
                                for k, v in e.items()
                                if k not in {"event", "captured_s", "log_level"}
                            },
                        )
                        for i, e in enumerate(logs)
                    ),
                    complete=True,
                )

            # Evidence effects cover only second attempt, including its logs.
            def second_effects() -> AdmissionEffects:
                captured = effects()
                return replace(
                    captured,
                    logs=tuple(
                        e for e in captured.logs if e.event == "turn.skipped.over_channel_budget"
                    ),
                )

            join.effects = second_effects
            with monkeypatch.context() as clock_patch:
                clock_patch.setattr(loop, "time", lambda: real_time() + skew)
                run = await join.run(SOURCE, evidence_id=f"new15:{admin.backend}")
        assert not admin.trace.violations and not admin.trace.replies
        # Main #824: exactly one bounded catch-up on this fresh Anthropic
        # session; foreign backends must never enter Anthropic billing recovery.
        catch_up = [
            r for r in admin.trace.requests if r.method == "GET" and r.path.endswith("/events")
        ]
        assert len(catch_up) == (1 if admin.backend == "anthropic" else 0)
        if catch_up:
            request = catch_up[0]
            assert request.path == "/v1/sessions/native-anthropic-session/events"
            assert request.query == (
                ("beta", "true"),
                ("limit", "100"),
                ("order", "desc"),
                ("types[]", "span.model_request_end"),
            )
            assert request.body == b""
            assert dict(request.protocol_headers)["anthropic-beta"] == "managed-agents-2026-04-01"
        if wire is not None:
            assert wire.requests
        # Persist only protocol headers and scripted bodies: no SDK credentials.
        native_trace = (
            []
            if wire is None
            else [
                {
                    "method": r.method,
                    "path": r.url.path,
                    "query": list(r.url.params.multi_items()),
                    "body": json_body(r),
                    "protocol_headers": {
                        k: r.headers[k]
                        for k in ("OpenAI-Beta", "Api-Revision", "Content-Type")
                        if k in r.headers
                    },
                }
                for r in wire.requests
            ]
        )
        (root / f"NEW15-{admin.backend}-sdk-trace.json").write_text(
            json.dumps(
                {
                    "anthropic": [r.to_dict() for r in admin.trace.requests],
                    "native": native_trace,
                    "metered": run.metered.metered,
                    "first_native_terminal": [
                        e.model_dump(mode="json") for e in run.first_terminal
                    ],
                    "issues": run.metered.issues,
                    "native_usage": [
                        r.model_dump(mode="json") for r in run.metered.after.native_usage
                    ],
                    "usage": [r.model_dump(mode="json") for r in run.metered.after.usage],
                    "debits": [r.model_dump(mode="json") for r in run.readback.debits],
                    "budget": None
                    if run.readback.budget is None
                    else {
                        "limit_usd": str(run.readback.budget.budget.limit_usd),
                        "spent_usd": str(run.readback.budget.spent_usd),
                        "window": run.readback.budget.budget.window,
                        "exceeded": run.readback.budget.is_exceeded,
                    },
                },
                indent=2,
                default=lambda value: value.hex() if isinstance(value, bytes) else str(value),
            )
        )
        return run
    finally:
        await surface.client._connection.close()  # pyright: ignore[reportPrivateUsage]
        if sdk is not None:
            if admin.backend == "gemini":
                await sdk.aio.aclose()
                sdk.close()
            else:
                await sdk.close()


async def test_frozen_new15_real_metered_then_budget_denial(
    admin: AdminBindings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Main #824's history rereads the streamed span; the debit must remain once-only.
    run = await capture_run(admin, tmp_path, monkeypatch)
    assert run.metered.metered, run.metered.issues
    assert run.metered.after.net_debit_usd == Decimal("0.015000")
    assert run.admission is not None and not run.admission.pending
    assert run.admission.native_sends == 0
    assert (
        run.admission.decision is not None
        and run.admission.decision.reason == "channel_budget_exceeded"
    )
    assert any(e.event == "turn.skipped.over_channel_budget" for e in run.admission.effects.logs)
    assert run.outcome.status == "PASS", run.outcome
    assert len(run.first_terminal) == 1
    assert replace(run, first_terminal=()).outcome.status == "PENDING"
    with pytest.raises(ValueError, match="captured native terminal"):
        _ = replace(
            run, first_surface=run.first_surface.model_copy(update={"root_turn_id": "invented"})
        ).outcome
    # Forgery negatives mutate actual captured facts, never parallel outcome copies.
    with pytest.raises(ValueError, match="independently pinned"):
        _ = replace(run, source_sha256="0" * 64).outcome
    assert replace(run, admission=replace(run.admission, native_sends=1)).outcome.status == "FAIL"
    assert replace(run, readback=replace(run.readback, entries=())).outcome.status == "FAIL"
    assert (
        replace(run, metered=replace(run.metered, issues=("MISSING_ACTUAL_USAGE",))).outcome.status
        == "PENDING"
    )
    assert (
        replace(
            run, first_surface=run.first_surface.model_copy(update={"text_capture_complete": False})
        ).outcome.status
        == "PENDING"
    )
    assert (
        replace(
            run, admission=replace(run.admission, pending=("ADAPTER_EFFECT_COVERAGE_MISSING",))
        ).outcome.status
        == "PENDING"
    )
    # Decode-time integrity: leave pending=() while altering each actual fact.
    admission = run.admission
    assert (
        replace(
            run, admission=replace(admission, effects=replace(admission.effects, complete=False))
        ).outcome.status
        == "PENDING"
    )
    with pytest.raises(ValueError, match="current capture window"):
        _ = replace(
            run, admission=replace(admission, observed_s=admission.started_s + 0.001)
        ).outcome
    # A shortened window containing every actual effect still lacks the authored wait.
    last_effect = max(e.observed_s for e in (*admission.effects.notices, *admission.effects.logs))
    assert last_effect - admission.started_s < 20
    assert (
        replace(
            run,
            admission=replace(admission, observed_s=max(last_effect, admission.decision_s or 0)),
        ).outcome.status
        == "PENDING"
    )
    assert admission.decision is not None
    for mutation in (
        replace(admission, thread_id="unrelated-thread"),
        replace(admission, external_user_id="unrelated-caller"),
        replace(
            admission,
            thread_id="unrelated-thread",
            decision=replace(admission.decision, thread_id="unrelated-thread"),
        ),
        replace(
            admission,
            external_user_id="unrelated-caller",
            decision=replace(admission.decision, external_user_id="unrelated-caller"),
        ),
        replace(admission, decision=replace(admission.decision, account_id=uuid.UUID(int=99))),
        replace(admission, decision=replace(admission.decision, channel_id="unrelated-channel")),
        replace(admission, decision_s=admission.started_s - 1),
    ):
        with pytest.raises(ValueError):
            _ = replace(run, admission=mutation).outcome
    for time_s in (admission.started_s - 1, admission.observed_s + 1, float("nan")):
        notices = tuple(
            n.model_copy(update={"observed_s": time_s}) for n in admission.effects.notices
        )
        with pytest.raises(ValueError, match="current capture window"):
            _ = replace(
                run,
                admission=replace(admission, effects=replace(admission.effects, notices=notices)),
            ).outcome
    # A real capture-window notice after the authored 15-second deadline is late.
    late = tuple(
        n.model_copy(update={"observed_s": admission.started_s + 16})
        for n in admission.effects.notices
    )
    assert (
        replace(
            run, admission=replace(admission, effects=replace(admission.effects, notices=late))
        ).outcome.status
        == "FAIL"
    )
    # The full 20-second window also catches an answer appearing after the notice deadline.
    answer = admission.effects.notices[0].model_copy(
        update={"text": "TINY-AGAIN", "observed_s": admission.started_s + 16}
    )
    assert (
        replace(
            run,
            admission=replace(
                admission,
                effects=replace(admission.effects, notices=(*admission.effects.notices, answer)),
            ),
        ).outcome.status
        == "FAIL"
    )
    assert replace(run, admission=replace(admission, native_sends=None)).outcome.status == "PENDING"
    with pytest.raises(ValueError):
        _ = replace(
            run, first_surface=run.first_surface.model_copy(update={"post_capture_complete": False})
        ).outcome
    with pytest.raises(ValueError, match="capture window is invalid"):
        _ = replace(run, admission=replace(admission, observed_s=float("nan"))).outcome
    silent = replace(run.admission, effects=replace(run.admission.effects, notices=()))
    assert replace(run, admission=silent).outcome.status == "FAIL"
    with pytest.raises(ValueError, match="owned channel/session"):
        _ = replace(run, readback=replace(run.readback, tenant_id=uuid.UUID(int=99))).outcome
    with pytest.raises(ValueError, match="owned channel/session"):
        _ = replace(
            run, first_surface=run.first_surface.model_copy(update={"session_id": "foreign"})
        ).outcome
    # Real query after teardown: retain the genuine debit; remove only the budget.
    assert run.cleanup.debits == run.readback.debits and run.cleanup.budget is None
    (tmp_path / f"NEW15-{admin.backend}.json").write_text(
        json.dumps(
            {
                "outcome": run.outcome.model_dump(mode="json"),
                "debit_usd": str(run.readback.net_debit_usd),
                "native_sends_second": run.admission.native_sends,
                "notices": [n.text for n in run.admission.effects.notices],
                "pricing": run.metered.pricing.evidence_id,
                "source_sha256": run.source_sha256,
            },
            indent=2,
        )
    )


@pytest.mark.parametrize("deferral", ["sleeping_task", "call_later"])
async def test_a_second_answer_deferred_inside_the_window_is_observed(
    admin: AdminBindings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deferral: str,
) -> None:
    """A system that answers 10 s after refusing must not pass the 20 s window.

    The post comes from a timer the denial armed: an `asyncio.sleep` in a spawned
    task, or a bare `loop.call_later`. Both fire on the loop clock that the
    window advances, so the late `TINY-AGAIN` is captured and `text_absent` fails.
    """
    posted: list[asyncio.Task[object]] = []

    def defer(surface: DiscordSurfaceHarness) -> None:
        loop = asyncio.get_running_loop()

        async def answer() -> None:
            await surface.thread.send("TINY-AGAIN")

        if deferral == "call_later":
            loop.call_later(10, lambda: posted.append(loop.create_task(answer())))
        else:

            async def later() -> None:
                await asyncio.sleep(10)
                await answer()

            posted.append(loop.create_task(later()))

    run = await capture_run(admin, tmp_path, monkeypatch, after_denial=defer)
    assert run.admission is not None
    late = [n for n in run.admission.effects.notices if "TINY-AGAIN" in n.text]
    assert len(late) == 1 and 9 < late[0].observed_s - run.admission.started_s < 15
    assert run.admission.observed_s - run.admission.started_s >= 20
    assert run.outcome.status == "FAIL"
    assert any(
        c.kind == "text_absent" and c.turn == 2 and c.status == "FAIL" for c in run.outcome.checks
    )


async def test_work_still_running_after_the_window_fails_the_capture(
    admin: AdminBindings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-dispatch due after the 20 s window surfaces instead of vanishing."""
    stragglers: list[asyncio.Task[None]] = []

    def straggle(surface: DiscordSurfaceHarness) -> None:
        async def later() -> None:
            await asyncio.sleep(30)
            await surface.thread.send("TINY-AGAIN")

        stragglers.append(asyncio.get_running_loop().create_task(later()))

    try:
        with pytest.raises(AssertionError, match="past the observation window"):
            await capture_run(admin, tmp_path, monkeypatch, after_denial=straggle)
    finally:
        for task in stragglers:
            task.cancel()
        await asyncio.gather(*stragglers, return_exceptions=True)


async def test_actual_sdk_missing_usage_never_invents_exhaustion(
    admin: AdminBindings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Main #824's empty model-request history must not fabricate usage or exhaustion.
    run = await capture_run(admin, tmp_path, monkeypatch, missing_usage=True)
    assert not run.metered.metered
    assert run.readback.debits == ()
    assert run.admission is None
    assert run.outcome.status == "PENDING"
    assert all(c.status == "PENDING" for c in run.outcome.checks if c.kind != "cleanup")
    assert run.cleanup.budget is None

"""Explicit host preparation, scoped persistent binding and real neutral billing."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from daimon.core.channel_backend import BackendUnsupported, check_backend
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.mux_backend import TurnRuntime
from daimon.core.pricing import ProviderPrice
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.admission import Admission
from daimon.core.turn.binding import prepared_persistence
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import (
    PROFILE,
    GeminiDeployment,
    GeminiJournal,
    GeminiTransportRuntime,
    GeminiUsageRuntime,
)
from daimon.core.turn.prepare import PreparedTurn, bind_session_impl
from daimon.core.turn.run import run_prepared_turn_impl
from daimon.core.usage_billing import ObservationRecorder
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.events import AgentMessagePayload, TextPart
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec
from mux.drivers.gemini.flash import FLASH_PRIMARY, FlashAttempt
from mux.drivers.gemini.transport import Object
from mux.errors import ContinuityLost, ScopeViolation
from mux.state.lease import Slot
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from mux.testing.drivers.gemini.fake import MemoryStorage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_gemini_codec import TrackedTransport, interaction

TENANT, ACCOUNT = uuid.UUID(int=12001), uuid.UUID(int=12002)
CHANNEL = ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="explicit-channel")
CONFIG = ConfigRevision.create(
    CHANNEL,
    1,
    resolve_default(
        BackendConfig(backend="gemini", profile=PROFILE, model=FLASH_PRIMARY),
    ),
)


class Accounting:
    def __init__(self) -> None:
        self.holds: list[ModelRef] = []
        self.receipts: list[FlashAttempt] = []
        self.released: list[str] = []

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        self.holds.append(model)

    async def after_attempt(self, *, receipt: FlashAttempt) -> None:
        self.receipts.append(receipt)

    async def release_unbilled(self, *, interaction_id: str) -> bool:
        self.released.append(interaction_id)
        return True


@dataclass
class Host:
    deps: TurnDeps
    admission: Admission
    transport: TrackedTransport
    journal: GeminiJournal
    ancillary: ScriptedTransport


@pytest.fixture
async def host(db_session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Host]:
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, id=TENANT)
    store = PostgresStateStore(db_session_factory)
    deployment = GeminiDeployment(
        config_digest=CONFIG.digest,
        config_revision=CONFIG.local,
        account_scope_id="project",
        agent=AgentSpec(name="explicit-agent", model=ModelRef(provider="gemini", id=FLASH_PRIMARY)),
        environment=EnvironmentSpec(name="inline"),
    )
    journal = GeminiJournal(MemoryStorage(), store, deployment)
    transport, ancillary = TrackedTransport(), ScriptedTransport()
    price = ProviderPrice(
        provider="gemini",
        model=FLASH_PRIMARY,
        checked_on=date(2026, 10, 10),
        input=Decimal("0.75"),
        output=Decimal("3.75"),
        cache_read=Decimal("0.075"),
    )
    runtime = TurnRuntime(
        transport_factory=lambda config, scope: GeminiTransportRuntime(transport),
        journal=journal,
        usage_store=GeminiUsageRuntime(Accounting(), {FLASH_PRIMARY: price}, Decimal(0)),
    )
    async with ancillary.client() as client:
        deps = TurnDeps(
            anthropic=client,
            sessionmaker=db_session_factory,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            defaults_root=Path("/unused"),
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
            turn_runtimes={PROFILE: runtime},
            state_store=store,
            tool_safety=OPEN_TOOL_SAFETY,
        )
        admission = Admission(
            account_id=ACCOUNT,
            agent=ma_agent(),
            environment=ma_environment(),
            config=ResolvedConfig(agent_name="configured", environment_name="configured"),
            backend_revision=CONFIG,
            backend_revision_origin="channel",
            backend=check_backend(CONFIG),
            origin_channel_id=CHANNEL.channel_id,
        )
        yield Host(deps, admission, transport, journal, ancillary)


async def prepare(
    host: Host, admission: Admission | None = None, *, reuse_existing: bool = True
) -> PreparedTurn:
    return await bind_session_impl(
        host.deps,
        admission or host.admission,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="user",
        thread_id="thread",
        session_account_id=ACCOUNT,
        reuse_existing=reuse_existing,
    )


async def test_explicit_registered_preparation_reuses_authorized_persisted_binding(
    host: Host,
) -> None:
    first = await prepare(host)
    second = await prepare(host)
    assert not first.reused and second.reused and first.session_ref == second.session_ref
    assert first.backend is not None and first.backend.capabilities().profile_id == PROFILE
    binding = await host.journal.state_store.get_binding(
        Slot(
            thread=ThreadRef(channel=CHANNEL, thread_id="thread"),
            account_id=str(ACCOUNT),
        )
    )
    assert binding is not None and binding.native_refs["session"] == first.ma_session_id
    assert binding.profile == PROFILE and binding.legacy_account_id == str(ACCOUNT)
    assert not host.transport.requests and not host.ancillary.requests


async def test_fresh_preparation_creates_empty_slot_but_cannot_replace_binding(host: Host) -> None:
    first = await prepare(host, reuse_existing=False)
    assert not first.reused
    assert first.backend is not None and first.session_ref is not None
    slot = Slot(thread=ThreadRef(channel=CHANNEL, thread_id="thread"), account_id=str(ACCOUNT))
    retained = await host.journal.state_store.get_binding(slot)
    assert retained is not None

    def forbidden(config: ConfigRevision, scope: Scope) -> object:
        raise AssertionError("fresh request for existing binding allocated a transport")

    runtime = host.deps.turn_runtimes[PROFILE]
    host.deps = replace(
        host.deps, turn_runtimes={PROFILE: replace(runtime, transport_factory=forbidden)}
    )
    with pytest.raises(ContinuityLost):
        await prepare(host, reuse_existing=False)
    assert await host.journal.state_store.get_binding(slot) == retained
    assert not host.transport.requests and not host.ancillary.requests


def _causes(error: BaseException) -> list[tuple[str, ...]]:
    """`missing` of every UnsupportedCapability in the cause chain."""
    found: list[tuple[str, ...]] = []
    while error is not None:
        missing = getattr(error, "missing", None)
        if isinstance(missing, tuple):
            found.append(missing)
        error = error.__cause__ or error.__context__  # type: ignore[assignment]
    return found


@pytest.mark.parametrize("failure", ["runtime", "deployment", "tenant", "sealed", "revision"])
async def test_missing_or_foreign_dependencies_refuse_before_provider_factory(
    host: Host,
    failure: str,
) -> None:
    opened: list[Scope] = []
    runtime = host.deps.turn_runtimes[PROFILE]

    def factory(config: ConfigRevision, scope: Scope) -> object:
        opened.append(scope)
        raise AssertionError("invalid preparation accessed a provider")

    runtime = replace(runtime, transport_factory=factory)
    admission = host.admission
    if failure == "runtime":
        host.deps = replace(host.deps, turn_runtimes={})
    elif failure == "deployment":
        host.deps = replace(
            host.deps,
            turn_runtimes={
                PROFILE: replace(
                    runtime,
                    journal=replace(host.journal, deployment=None),
                )
            },
        )
    elif failure == "tenant":
        admission = replace(
            admission,
            backend_revision=CONFIG.model_copy(
                update={
                    "channel": CHANNEL.model_copy(update={"tenant_id": "foreign"}),
                }
            ),
        )
    elif failure == "sealed":
        admission = replace(admission, origin_seal_ids=frozenset({"sealed"}))
    else:
        admission = replace(
            admission,
            backend_revision=ConfigRevision.create(
                CHANNEL,
                2,
                resolve_default(
                    BackendConfig(backend="gemini", profile=PROFILE, model=FLASH_PRIMARY)
                ),
            ),
        )
    if failure in {"tenant", "sealed", "revision"}:
        host.deps = replace(host.deps, turn_runtimes={PROFILE: runtime})
    with pytest.raises((ScopeViolation, AdmissionDenied)):
        await prepare(host, admission)
    assert not opened and not host.transport.requests and not host.ancillary.requests


async def test_two_host_turns_bill_actual_interactions_and_journal_under_fence(host: Host) -> None:
    host.transport.responses.extend(
        [interaction("one", text="first"), interaction("two", text="second")]
    )
    prepared = await prepare(host)
    for index in range(2):
        prepared = await prepare(host)
        assert prepared.session_ref is not None

        async def reseed() -> str:
            raise AssertionError("Gemini host turn must never use Anthropic recovery")

        outcome = await run_prepared_turn_impl(
            host.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="user",
            user_message="question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            operation_key=f"offline-host:{index}",
        )
        assert not outcome.recovered and outcome.ma_session_id == prepared.ma_session_id
        state = outcome.state
        assert state.error is None and state.usage_totals.output_tokens == 11
    assert host.transport.requests[1]["previous_interaction_id"] == "one"
    assert len(host.transport.requests) == 2 and not host.ancillary.requests
    async with host.deps.sessionmaker() as session:
        spent = await session.scalar(
            text("SELECT -sum(delta_usd) FROM tenant_ledger WHERE tenant_id=:tenant"),
            {"tenant": TENANT},
        )
        assert spent == Decimal("0.000156")
        assert (
            await session.scalar(
                text("SELECT count(*) FROM accounting_outbox WHERE applied_at IS NOT NULL")
            )
            == 2
        )
    assert prepared.session_ref is not None
    journal = await host.journal.state_store.read_events(prepared.session_ref.id)
    assert len({e.turn_id for e in journal if e.type == "session.turn_ended"}) == 2
    assert len([e for e in journal if e.type == "user.message"]) == 2
    assert len([e for e in journal if e.type == "usage.observed"]) == 2
    messages = [e.typed_payload() for e in journal if e.type == "agent.message"]
    assert len(messages) == 2
    assert [
        part.text
        for payload in messages
        if isinstance(payload, AgentMessagePayload)
        for part in payload.content
        if isinstance(part, TextPart)
    ] == ["first", "second"]


@pytest.mark.parametrize("failure", ["runtime", "model", "shared", "publishing", "daimon_rules"])
async def test_unsupported_host_configuration_is_visible_refusal_before_factory(
    host: Host, failure: str
) -> None:
    opened: list[Scope] = []

    def forbidden(config: ConfigRevision, scope: Scope) -> object:
        opened.append(scope)
        raise AssertionError("unsupported configuration reached the transport factory")

    runtime = replace(host.deps.turn_runtimes[PROFILE], transport_factory=forbidden)
    host.deps = replace(host.deps, turn_runtimes={PROFILE: runtime})
    admission = host.admission
    if failure == "runtime":
        host.deps = replace(host.deps, turn_runtimes={})
    elif failure in ("model", "shared"):
        revision = ConfigRevision.create(
            CHANNEL,
            1,
            resolve_default(
                BackendConfig(
                    backend="gemini",
                    profile=PROFILE,
                    model="gemini-3.5-pro" if failure == "model" else FLASH_PRIMARY,
                    thread_mode="shared" if failure == "shared" else "per_caller",
                )
            ),
        )
        with pytest.raises(BackendUnsupported):
            check_backend(revision)
        # Independently prove the preparation boundary also refuses a stale or
        # manually supplied admission, rather than relying on the first gate.
        admission = replace(admission, backend_revision=revision)
    elif failure == "daimon_rules":
        # A rule that makes daimon's own tools ask is a gate this backend cannot hold.
        admission = replace(
            admission,
            daimon_rule_tools=frozenset({"get_session"}),
            daimon_rules_need_driver=True,
        )
    else:
        admission = replace(admission, asks_before_publishing=True)
    with pytest.raises(AdmissionDenied) as refusal:
        await prepare(host, admission)
    assert refusal.value.reason == "backend_unsupported"
    if failure == "daimon_rules":
        assert ("daimon_tool_rules",) in _causes(refusal.value), "the log names the rule gap"
    assert not opened and not host.transport.requests and not host.ancillary.requests


async def test_fallback_alias_usage_is_preserved_as_pending_after_restart(host: Host) -> None:
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart
    from mux.errors import ProviderError

    first = await prepare(host)
    assert first.backend is not None and first.session_ref is not None
    host.transport.responses.extend(
        [
            ProviderError("overloaded", retryable=True, native_code="503"),
            interaction("alias"),
        ]
    )
    scope = Scope(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        principal_id="daimon",
        authorization_id="admitted-turn",
    )
    await first.backend.events.send(
        scope,
        first.session_ref,
        (UserMessage(content=(TextPart(text="hi"),)),),
        key="alias-attempt",
    )
    restarted = await prepare(host)
    assert restarted.backend is not None and restarted.session_ref == first.session_ref
    observations = await restarted.backend.usage.reconcile(scope, first.session_ref)
    assert observations and observations[-1].model == ModelRef(
        provider="gemini", id="gemini-flash-latest"
    )
    callback = cast(ObservationRecorder, restarted._record)  # pyright: ignore[reportPrivateUsage]
    assert await callback(observation=observations[-1]) is False
    assert observations[-1].input_tokens == 64 and observations[-1].output_tokens == 11
    async with host.deps.sessionmaker() as session:
        assert await session.scalar(text("SELECT count(*) FROM tenant_ledger")) == 0
        assert (
            await session.scalar(
                text("SELECT count(*) FROM accounting_outbox WHERE applied_at IS NULL")
            )
            == 1
        )
    assert len(host.transport.requests) == 2 and not host.ancillary.requests


async def test_each_tool_round_has_its_own_hold_and_settles_at_requires_action(
    host: Host, tmp_path: Path
) -> None:
    import json
    import sqlite3

    from daimon.core.turn.gemini import GeminiTurnIO
    from daimon.core.turn.gemini_state import SQLiteFlashAccounting

    path = tmp_path / "holds.sqlite3"
    accounting = SQLiteFlashAccounting(
        path, namespace="n", hold_usd=Decimal("0.05"), cap_usd=Decimal("0.15")
    )
    runtime = host.deps.turn_runtimes[PROFILE]
    assert isinstance(runtime.usage_store, GeminiUsageRuntime)
    usage = replace(runtime.usage_store, accounting=accounting)
    host.deps = replace(host.deps, turn_runtimes={PROFILE: replace(runtime, usage_store=usage)})
    prepared = await prepare(host)
    assert prepared.backend is not None and prepared.session_ref is not None
    scope = Scope(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        principal_id="daimon",
        authorization_id="admitted-turn",
    )
    persistence = await prepared_persistence(
        host.deps,
        prepared.admission,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        session_id=prepared.ma_session_id,
        mapping_id=None,
        session_ref=prepared.session_ref,
        operation_key="tool-rounds",
    )
    rounds = []
    for name in ("round-1", "round-2"):
        reply = interaction(name, status="requires_action")
        reply["steps"] = [
            {"type": "function_call", "id": f"{name}-call", "name": "weather", "arguments": {}}
        ]
        rounds.append(reply)
    host.transport.responses.extend([*rounds, interaction("answer")])
    io = GeminiTurnIO(prepared.backend, scope, prepared.session_ref, persistence=persistence)

    async def drive() -> None:
        await io.send([{"type": "user.message", "content": [{"type": "text", "text": "weather"}]}])
        for _ in range(2):
            frames = await io.replay_turn_events()
            tool = [
                f.native
                for f in frames
                if f.native is not None and f.native.type == "agent.custom_tool_use"
            ][-1]
            await io.send(
                [
                    {
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": tool.id,
                        "content": [{"type": "text", "text": "cold"}],
                    }
                ]
            )
        await io.replay_turn_events()

    await persistence.run(drive)

    def ledger() -> list[dict]:
        with sqlite3.connect(path) as connection:
            (body,) = connection.execute("SELECT body FROM payloads").fetchone()
        return sorted(json.loads(body).values(), key=lambda row: row["interaction_id"])

    # Every round, continuation included, reserved its own hold on its own interaction.
    assert [(row["interaction_id"], row["held_usd"]) for row in ledger()] == [
        ("answer", "0.05"),
        ("round-1", "0.05"),
        ("round-2", "0.05"),
    ]
    observations = await prepared.backend.usage.reconcile(scope, prepared.session_ref)
    # A requires_action interaction is final: its tool result continued as a new one.
    assert sorted((o.id, o.final) for o in observations) == [
        ("gemini:answer:usage", True),
        ("gemini:round-1:usage", True),
        ("gemini:round-2:usage", True),
    ]
    record = cast(ObservationRecorder, prepared._record)  # pyright: ignore[reportPrivateUsage]
    for observation in observations:
        await record(observation=observation)
    assert [(row["interaction_id"], row["held_usd"], row["settled_usd"]) for row in ledger()] == [
        ("answer", "0", "0.05"),
        ("round-1", "0", "0.05"),
        ("round-2", "0", "0.05"),
    ]


async def test_fenced_function_result_has_its_own_claim_and_preserves_root(host: Host) -> None:
    from daimon.core.turn.gemini import GeminiTurnIO

    prepared = await prepare(host)
    assert prepared.backend is not None and prepared.session_ref is not None
    scope = Scope(
        tenant_id=str(TENANT),
        account_id=str(ACCOUNT),
        principal_id="daimon",
        authorization_id="admitted-turn",
    )
    persistence = await prepared_persistence(
        host.deps,
        prepared.admission,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        session_id=prepared.ma_session_id,
        mapping_id=None,
        session_ref=prepared.session_ref,
        operation_key="fenced-action",
    )
    reply = interaction("required", status="requires_action")
    reply["steps"] = [
        {"type": "function_call", "id": "native-action", "name": "weather", "arguments": {}}
    ]
    next_reply = interaction("required-next", status="requires_action")
    next_reply["steps"] = [
        {"type": "function_call", "id": "next-action", "name": "weather", "arguments": {}}
    ]
    host.transport.responses.extend([reply, next_reply, interaction("result")])
    io = GeminiTurnIO(prepared.backend, scope, prepared.session_ref, persistence=persistence)

    async def drive() -> None:
        await io.send([{"type": "user.message", "content": [{"type": "text", "text": "weather"}]}])
        root = io.root
        for _ in range(2):
            frames = await io.replay_turn_events()
            tool = [
                f.native
                for f in frames
                if f.native is not None and f.native.type == "agent.custom_tool_use"
            ][-1]
            await io.send(
                [
                    {
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": tool.id,
                        "content": [{"type": "text", "text": "cold"}],
                    }
                ]
            )
            assert io.root == root
        await io.replay_turn_events()

    await persistence.run(drive)
    assert len(host.transport.requests) == 3
    assert host.transport.requests[1]["input"] == [
        {"type": "function_result", "name": "weather", "call_id": "native-action", "result": "cold"}
    ]
    initial = await host.journal.state_store.get_operation(
        scope, f"{persistence.operation_key}:send:0"
    )
    result = await host.journal.state_store.get_operation(
        scope, f"{persistence.operation_key}:results:native-action:0"
    )
    assert initial is not None and result is not None
    assert initial.operation.status == result.operation.status == "accepted"
    second_result = await host.journal.state_store.get_operation(
        scope, f"{persistence.operation_key}:results:next-action:0"
    )
    assert second_result is not None and second_result.operation.status == "accepted"
    journal = await host.journal.state_store.read_events(prepared.session_ref.id)
    actions = [e for e in journal if e.type == "session.requires_action"]
    assert len(actions) == 2
    assert {e.turn_id for e in actions} == {io.root}
    assert host.transport.requests[2]["input"] == [
        {"type": "function_result", "name": "weather", "call_id": "next-action", "result": "cold"}
    ]


@pytest.mark.parametrize(
    ("status", "replay"),
    [
        ("failed", False),
        ("failed", True),
        ("cancelled", True),
        ("cancelled", False),
        ("incomplete", False),
        ("budget_exceeded", False),
    ],
)
async def test_final_host_outcome_preserves_failed_or_cancelled_root(
    host: Host, monkeypatch: pytest.MonkeyPatch, status: str, replay: bool
) -> None:
    from daimon.core.turn.gemini import GeminiTurnIO
    from daimon.core.turn.io import TurnConnectionLost, TurnEvent, TurnStream
    from daimon.core.turn.termination import TerminationReason

    prepared = await prepare(host)
    host.transport.responses.append(interaction("terminal-root", status=status))
    if replay:
        openings = 0

        class DroppedStream:
            def __aiter__(self) -> "DroppedStream":
                return self

            async def __anext__(self) -> TurnEvent:
                if openings == 1:
                    raise TurnConnectionLost("lost connection before saved terminal delivery")
                raise StopAsyncIteration

            async def close(self) -> None:
                pass

        async def dropped(self: GeminiTurnIO, *, read_timeout_s: float) -> TurnStream:
            nonlocal openings
            openings += 1
            return DroppedStream()

        monkeypatch.setattr(GeminiTurnIO, "open_stream", dropped)

    async def reseed() -> str:
        raise AssertionError("foreign failure must not recover through Anthropic")

    lifecycle = RecordingLifecycle()
    outcome = await run_prepared_turn_impl(
        host.deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="user",
        user_message="question",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        operation_key="terminal-probe",
    )
    assert not outcome.recovered and len(host.transport.requests) == 1
    assert not host.ancillary.requests
    assert outcome.termination != TerminationReason.COMPLETED
    if status != "cancelled":
        assert outcome.termination == (
            TerminationReason.SESSION_TERMINATED
            if status in {"incomplete", "budget_exceeded"}
            else TerminationReason.UPSTREAM
        )
        assert outcome.state.error is not None and outcome.state.error.kind == "upstream"
        assert len(lifecycle.terminal_failures) == 1 and not lifecycle.terminal_success
    elif replay:
        assert outcome.termination == TerminationReason.INTERRUPTED
        assert outcome.state.error is not None and outcome.state.error.kind == "interrupted"
        assert len(lifecycle.terminal_failures) == 1 and not lifecycle.terminal_success
    else:
        # Live externally cancelled roots use the existing interrupt convention;
        # SDK-only replay needs the explicit interruption signal to avoid success.
        assert outcome.termination == TerminationReason.INTERRUPTED
        assert outcome.state.error is None
        assert len(lifecycle.terminal_success) == 1 and not lifecycle.terminal_failures
    journal = await host.journal.state_store.read_events(prepared.ma_session_id)
    terminal = [e for e in journal if e.type == "session.turn_ended"]
    assert len(terminal) == 1
    assert (
        terminal[0].payload["outcome"]
        == {
            "failed": "errored",
            "cancelled": "interrupted",
            "incomplete": "terminated",
            "budget_exceeded": "terminated",
        }[status]
    )
    async with host.deps.sessionmaker() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM accounting_outbox WHERE applied_at IS NOT NULL")
            )
            == 1
        )


async def test_a_completed_turns_recorded_usage_settles_its_real_hold(
    host: Host, tmp_path: Path
) -> None:
    import json
    import sqlite3

    from daimon.core.turn.gemini_state import SQLiteFlashAccounting

    path = tmp_path / "holds.sqlite3"
    accounting = SQLiteFlashAccounting(
        path, namespace="n", hold_usd=Decimal("0.05"), cap_usd=Decimal("0.05")
    )
    runtime = host.deps.turn_runtimes[PROFILE]
    assert isinstance(runtime.usage_store, GeminiUsageRuntime)
    usage = replace(runtime.usage_store, accounting=accounting)
    host.deps = replace(host.deps, turn_runtimes={PROFILE: replace(runtime, usage_store=usage)})

    async def reseed() -> str:
        raise AssertionError("Gemini host turn must never use Anthropic recovery")

    # The cap equals one hold: the second turn runs only if the first one's settled.
    for index, name in enumerate(("one", "two")):
        host.transport.responses.append(interaction(name))
        prepared = await prepare(host)
        outcome = await run_prepared_turn_impl(
            host.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="user",
            user_message="question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            operation_key=f"settle:{index}",
        )
        assert outcome.state.error is None
    with sqlite3.connect(path) as connection:
        (body,) = connection.execute("SELECT body FROM payloads").fetchone()
    rows = sorted(json.loads(body).values(), key=lambda row: row["interaction_id"])
    # 48 uncached at $0.75 + 16 cached at $0.075 + 11 output at $3.75, per million.
    assert [
        (row["interaction_id"], row["held_usd"], row["settled_usd"], row["actual_usd"])
        for row in rows
    ] == [("one", "0", "0.05", "0.00007845"), ("two", "0", "0.05", "0.00007845")]


async def test_host_requested_cancel_remains_interrupted_and_journals_observed_stop(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from daimon.core.turn.gemini_state import SQLiteFlashAccounting
    from daimon.core.turn.termination import TerminationReason

    settled: list[str] = []

    class Spy(SQLiteFlashAccounting):
        async def settle(self, *, interaction_id: str, model: str, actual_usd: Decimal) -> bool:
            settled.append(interaction_id)
            return await super().settle(
                interaction_id=interaction_id, model=model, actual_usd=actual_usd
            )

    runtime = host.deps.turn_runtimes[PROFILE]
    assert isinstance(runtime.usage_store, GeminiUsageRuntime)
    spy = Spy(tmp_path / "holds.sqlite3", namespace="n", hold_usd=Decimal(1), cap_usd=Decimal(1))
    usage = replace(runtime.usage_store, accounting=spy)
    host.deps = replace(host.deps, turn_runtimes={PROFILE: replace(runtime, usage_store=usage)})
    prepared = await prepare(host)
    host.transport.responses.append(interaction("cancel-root", status="in_progress"))
    host.transport.block = True
    cancel = asyncio.Event()
    native_cancel = host.transport.cancel

    async def cancel_provider(interaction_id: str) -> Object:
        host.transport.saved[interaction_id] = interaction(interaction_id, status="cancelled")
        return await native_cancel(interaction_id)

    monkeypatch.setattr(host.transport, "cancel", cancel_provider)

    async def stop_when_attached() -> None:
        await host.transport.read_started.wait()
        cancel.set()

    async def reseed() -> str:
        raise AssertionError("cancel must not recreate a provider session")

    stop = asyncio.create_task(stop_when_attached())
    try:
        outcome = await run_prepared_turn_impl(
            host.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="user",
            user_message="question",
            lifecycle=RecordingLifecycle(),
            cancel=cancel,
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            operation_key="cancel-probe",
        )
    finally:
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)
    assert outcome.termination == TerminationReason.INTERRUPTED and outcome.state.error is None
    assert not outcome.recovered and len(host.transport.requests) == 1
    assert not host.ancillary.requests and host.transport.close_count == 1
    assert host.transport.cancelled == ["cancel-root"]
    journal = await host.journal.state_store.read_events(prepared.ma_session_id)
    assert any(
        e.type == "session.turn_ended" and e.payload["outcome"] == "interrupted" for e in journal
    )
    async with host.deps.sessionmaker() as session:
        applied = (
            await session.execute(
                text(
                    "SELECT observation_id, revision, prior_applied_revision FROM accounting_outbox "
                    "WHERE applied_at IS NOT NULL ORDER BY revision"
                )
            )
        ).all()
    # The running snapshot, then the cancelled (final) reading of the same meter: a
    # correction of revision 1, never a second charge.
    assert [(row.revision, row.prior_applied_revision) for row in applied] == [(1, None), (2, 1)]
    assert len({row.observation_id for row in applied}) == 1
    # Both readings were recorded; only the final (cancelled) one settled the hold.
    assert settled == ["cancel-root"]


async def test_a_released_gemini_slot_is_adopted_with_a_fresh_session_not_a_replay(
    host: Host,
) -> None:
    from mux.state.store import ERA_REF, released_successor

    first = await prepare(host)
    slot = Slot(thread=ThreadRef(channel=CHANNEL, thread_id="thread"), account_id=str(ACCOUNT))
    store = host.journal.state_store
    bound = await store.get_binding(slot)
    assert bound is not None
    await store.put_binding(released_successor(bound), expected_generation=bound.generation)
    assert await store.get_binding(slot) is None
    second = await prepare(host)
    assert not second.reused and second.session_ref != first.session_ref
    adopted = await store.get_binding(slot)
    assert adopted is not None
    assert (adopted.id, adopted.generation) == (bound.id, bound.generation + 2)
    assert adopted.native_refs[ERA_REF] == f":g{bound.generation + 1}"
    third = await prepare(host)
    assert third.reused and third.session_ref == second.session_ref

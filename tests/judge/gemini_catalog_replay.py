"""Consume N3 native fixtures through G2, with exact additional host reads."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from daimon.core.channel_backend import check_backend
from daimon.core.mux_backend import TurnRuntime
from daimon.core.turn.gemini import (
    GeminiDeployment,
    GeminiJournal,
    GeminiTransportRuntime,
    GeminiUsageRuntime,
    gemini_backend,
)
from daimon.testing.admin_bindings import AdminBindings
from daimon.testing.drivers.gemini.fake import MemoryStorage
from daimon.testing.ma_transport import ScriptedTransport
from daimon.testing.outcome_oracle import TurnEvidence
from daimon.testing.provider_fixtures import (
    BackendFixture,
    FixtureGap,
    NativeTurn,
    ScenarioFixture,
    turn_tape,
)
from daimon.testing.provider_replay import ProviderTape, WireReply, provider_replay
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ProviderBinding, SessionSpec
from mux.drivers.gemini.flash import FlashAttempt
from mux.drivers.gemini.transport import SDKTransport
from mux.state.memory import MemoryStateStore

if __package__:
    from .catalog_product_context import ProductContext
    from .catalog_runner import Invocation, ScenarioPlan, expand_text
    from .catalog_steps import ExecutionGap, StepReceipt, run_steps, validate_native_coverage
    from .gemini_catalog_host import GeminiTurnCapture, run_gemini_turn
else:
    from catalog_product_context import ProductContext
    from catalog_runner import Invocation, ScenarioPlan, expand_text
    from catalog_steps import ExecutionGap, StepReceipt, run_steps, validate_native_coverage
    from gemini_catalog_host import GeminiTurnCapture, run_gemini_turn


# Refuse SDK routing/discovery options by name before construction; no values
# or key files are read. Interactions replay is Developer API over MockTransport.
OFFLINE_SDK_ENV = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GENAI_USE_ENTERPRISE",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GEMINI_BASE_URL",
    "GOOGLE_VERTEX_BASE_URL",
)


class OfflineFlashAccounting:
    """Local dispatch observations only; no live budget or zero-cost certificate."""

    def __init__(self) -> None:
        self.before: list[ModelRef] = []
        self.after: list[FlashAttempt] = []

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None:
        if model != ModelRef(provider="gemini", id="gemini-3.8-flash") or attempt != 1:
            raise ValueError("offline Gemini recipe has no fallback attempt")
        self.before.append(model)

    async def after_attempt(self, *, receipt: FlashAttempt) -> None:
        self.after.append(receipt)


@dataclass(frozen=True)
class GeminiCapture:
    turns: tuple[GeminiTurnCapture, ...]
    request_count: int
    step_receipts: tuple[StepReceipt, ...] = ()
    step_gaps: tuple[ExecutionGap, ...] = ()


def host_tape(
    scenario: ScenarioFixture,
    fixture: BackendFixture,
    turn: NativeTurn,
    *,
    tenant: str,
    prior: tuple[NativeTurn, ...],
) -> ProviderTape:
    """Each reply consumed once; no router, retries or guessed recovery success."""
    tape = turn_tape(scenario, fixture, turn, key=f"host-turn-{turn.turn}:send:0")
    # Cold turns consume N3's exact POST, SSE and saved GET. Followups also
    # retrieve the previous interaction at stream attachment and before send.
    headers = (("api-revision", "2026-05-20"),)
    baseline: list[WireReply] = []
    if prior:
        previous = prior[-1]
        for label in ("attach-previous", "send-previous"):
            baseline.append(
                WireReply(
                    id=label,
                    method="GET",
                    path=f"/v1beta/interactions/{previous.root_id}",
                    headers=headers,
                    response_json=previous.snapshot,
                    after=(baseline[-1].id,) if baseline else (),
                )
            )
    replies = [
        *baseline,
        tape.replies[0].model_copy(
            update={
                "after": (baseline[-1].id,) if baseline else (),
            }
        ),
        tape.replies[1],
    ]
    # The native completion frame triggers reconciliation of every interaction.
    for old in prior:
        replies.append(
            WireReply(
                id=f"saved-prior-{old.turn}",
                method="GET",
                path=f"/v1beta/interactions/{old.root_id}",
                headers=headers,
                response_json=old.snapshot,
                after=(replies[-1].id,),
            )
        )
    replies.append(tape.replies[2].model_copy(update={"after": (replies[-1].id,)}))
    return ProviderTape.model_validate({**tape.model_dump(), "replies": replies})


async def execute_gemini_turns(
    plan: ScenarioPlan,
    scenario: ScenarioFixture,
    fixture: BackendFixture,
    *,
    source_root: Path,
    repository_root: Path,
    admin: AdminBindings | None = None,
    admin_values: Mapping[str, str] | None = None,
    product_context: ProductContext | None = None,
) -> GeminiCapture:
    """Validate all inputs, execute the owned prefix, retain later barriers."""
    if (
        plan.backend != "gemini"
        or fixture.backend != "gemini"
        or fixture not in scenario.providers
        or scenario.scenario_id != plan.scenario.id
        or scenario.source.sha256 != plan.scenario.sha256
        or scenario.source.path != f"scenarios/{plan.scenario.filename}"
        or fixture.profile != "gemini.inline_reuse"
        or fixture.model != "gemini-3.8-flash"
    ):
        raise ValueError("Gemini fixture is not a complete pinned headless recipe")
    present_names = frozenset(os.environ)
    if any(name in present_names for name in OFFLINE_SDK_ENV):
        raise ValueError("offline Gemini replay refuses ambient SDK options")
    fixture = BackendFixture.model_validate_json(fixture.model_dump_json())
    fixture.codec_source.resolve(repository_root)
    scenario.projection.verify(source_root)
    invocations = [i for i in plan.invocations if i.operation == "host_turn"]
    validate_native_coverage(plan, fixture)
    by_turn = {t.turn: t for t in fixture.turns}
    slots: dict[tuple[str, str], str] = {}
    for invocation in invocations:
        if invocation.turn is None or invocation.text is None:
            raise ValueError("Gemini invocation lacks its original turn/text")
        native = by_turn.get(invocation.turn)
        if native is None:
            continue
        if (
            native.location != invocation.location
            or native.channel != invocation.channel
            or native.thread != invocation.thread
            or native.request_text != expand_text(invocation.text, scenario.values)
        ):
            raise ValueError("Gemini fixture differs from the planned invocation")
        slot = (native.channel, native.thread)
        if slot in slots and slots[slot] != native.session_id:
            raise ValueError("Gemini followup changes its selected session")
        if slot not in slots and native.session_id in slots.values():
            raise ValueError("Gemini distinct host slots alias the same session")
        slots[slot] = native.session_id
    if product_context is not None:

        async def configure_product(invocation: Invocation) -> None:
            await product_context.configure(invocation)

        async def invoke_product(invocation: Invocation, _timeout_s: float) -> TurnEvidence:
            await product_context.native_pending(invocation)
            raise ValueError("unbound provider product preparation returned")

        gate_gaps = tuple(
            FixtureGap(
                code="RUNNER_HOOK",
                location=t.location,
                reason="Completion gate needs an owned concurrency hook.",
            )
            for t in fixture.turns
            if t.completion_gate
        )
        steps = await run_steps(
            plan,
            invoke=invoke_product,
            prepare_channel=configure_product,
            native_gaps=(*fixture.gaps, *gate_gaps),
            native_fixture=fixture,
            admin=admin,
            admin_values=admin_values,
            product_context=product_context,
        )
        return GeminiCapture((), 0, steps.receipts, steps.gaps)
    store = MemoryStateStore()
    bindings: dict[tuple[str, str], ProviderBinding] = {}
    storage = MemoryStorage()
    history: dict[str, list[NativeTurn]] = {}
    captures: list[GeminiTurnCapture] = []
    requests = 0
    sentinel_wire = ScriptedTransport()

    async def prepare_channel(invocation: Invocation) -> None:
        if product_context is not None:
            await product_context.configure(invocation)
            return
        channel = next(b for b in plan.bindings if b.ref == invocation.channel)
        check_backend(channel.revision)
        await store.put_config_revision(channel.revision)

    async with sentinel_wire.client() as sentinel:

        async def invoke(invocation: Invocation, timeout_s: float) -> TurnEvidence:
            nonlocal requests
            if product_context is not None:
                await product_context.native_pending(invocation)
            if invocation.turn is None:
                raise ValueError("Gemini invocation lacks a turn")
            native = by_turn[invocation.turn]
            channel = next(b for b in plan.bindings if b.ref == invocation.channel)
            scope = Scope(
                tenant_id=channel.revision.channel.tenant_id,
                account_id="qa-caller",
                principal_id="offline-catalog-host",
                authorization_id="scripted-fixture",
            )
            slot = (native.channel, native.thread)
            accounting = OfflineFlashAccounting()
            deployment = GeminiDeployment(
                config_digest=channel.revision.digest,
                config_revision=channel.revision.local,
                account_scope_id="qa-offline-project",
                agent=AgentSpec(
                    name="offline-catalog", model=ModelRef(provider="gemini", id=fixture.model)
                ),
                environment=EnvironmentSpec(name="inline"),
            )
            prior = history.setdefault(native.session_id, [])
            tape = host_tape(scenario, fixture, native, tenant=scope.tenant_id, prior=tuple(prior))
            async with provider_replay(
                tape,
                backend="gemini",
                profile=fixture.profile,
                model=fixture.model,
                source_root=source_root,
            ) as replay:
                if not isinstance(replay.transport, SDKTransport):
                    raise ValueError("Gemini fixture factory returned a foreign transport")
                transport = replay.transport
                runtime = TurnRuntime(
                    transport_factory=lambda _config, _scope, transport=transport: (
                        GeminiTransportRuntime(transport)
                    ),
                    journal=GeminiJournal(storage, store, deployment),
                    usage_store=GeminiUsageRuntime(accounting, {}),
                )
                if slot not in bindings:
                    backend = gemini_backend(channel.revision, scope, runtime, "qa-offline-project")
                    agent = await backend.agents.create(
                        scope, deployment.agent, key=f"{native.session_id}:agent"
                    )
                    environment = await backend.environments.create(
                        scope, deployment.environment, key=f"{native.session_id}:env"
                    )
                    session = await backend.sessions.create(
                        scope,
                        SessionSpec(
                            agent=agent.ref,
                            agent_revision=agent.revision,
                            environment=environment.ref,
                            config_revision=channel.revision.local,
                            extensions={
                                "gemini.session": ExtensionConfig(
                                    namespace="gemini.session",
                                    version=1,
                                    value={
                                        "binding_id": f"binding-{len(bindings)}",
                                        "thread": ThreadRef(
                                            channel=channel.revision.channel,
                                            thread_id=native.thread,
                                        ).model_dump(mode="json"),
                                    },
                                )
                            },
                        ),
                        key=f"{native.session_id}:session",
                    )
                    # N3's session_id is a caller-owned logical label, never a
                    # provider wire id. Pin the virgin local record to that label;
                    # no native events, roots, usage or terminal evidence are seeded.
                    async with storage.transaction() as records:
                        record = records.sessions.pop(session.ref.id)
                        if (
                            record.current
                            or record.events
                            or record.observations
                            or native.session_id in records.sessions
                        ):
                            raise ValueError("Gemini fixture bootstrap is not a virgin session")
                        binding = session.binding.model_copy(
                            update={
                                "native_refs": {
                                    **session.binding.native_refs,
                                    "session": native.session_id,
                                },
                                "legacy_account_id": scope.account_id,
                            }
                        )
                        record.session = session.model_copy(
                            update={
                                "ref": session.ref.model_copy(update={"id": native.session_id}),
                                "binding": binding,
                            }
                        )
                        records.sessions[native.session_id] = record
                        for creation, (principal, digest, id_) in tuple(records.creations.items()):
                            if id_ == session.ref.id:
                                records.creations[creation] = principal, digest, native.session_id
                    await store.put_binding(binding, expected_generation=0)
                    bindings[slot] = binding
                captures.append(
                    await run_gemini_turn(
                        plan,
                        invocation,
                        source_sha256=scenario.source.sha256,
                        request_text=native.request_text,
                        values=scenario.values,
                        expected_root=native.root_id,
                        project="qa-offline-project",
                        runtime=runtime,
                        sentinel=sentinel,
                        store=store,
                        binding=bindings[slot],
                        scope=scope,
                        timeout_s=timeout_s,
                    )
                )
                if (
                    len(accounting.before) != 1
                    or len(accounting.after) != 1
                    or (
                        accounting.after[0].outcome != "accepted"
                        or accounting.after[0].interaction_id != native.root_id
                    )
                ):
                    raise ValueError("Gemini replay lacks one accepted primary Flash attempt")
                replay.wire.assert_consumed()
                requests += len(replay.wire.requests)
            prior.append(native)
            return captures[-1].evidence

        gate_gaps = tuple(
            FixtureGap(
                code="RUNNER_HOOK",
                location=t.location,
                reason="Completion gate needs an owned concurrency hook.",
            )
            for t in fixture.turns
            if t.completion_gate
        )
        steps = await run_steps(
            plan,
            invoke=invoke,
            prepare_channel=prepare_channel,
            native_gaps=(*fixture.gaps, *gate_gaps),
            native_fixture=fixture,
            admin=admin,
            admin_values=admin_values,
            product_context=product_context,
        )
    if sentinel_wire.requests or sentinel_wire.violations:
        raise ValueError("Gemini host attempted an Anthropic fallback")
    return GeminiCapture(tuple(captures), requests, steps.receipts, steps.gaps)

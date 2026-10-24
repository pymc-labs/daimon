"""Consume N3 native fixtures through G1, with exact additional host reads."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from daimon.core.channel_backend import check_backend
from daimon.testing.admin_bindings import AdminBindings
from daimon.testing.outcome_oracle import TurnEvidence
from daimon.testing.provider_fixtures import (
    BackendFixture,
    FixtureGap,
    NativeTurn,
    ScenarioFixture,
    turn_tape,
)
from daimon.testing.provider_replay import Object, ProviderTape, WireReply, provider_replay
from mux.contracts.ids import Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.drivers.openai.transport import SDKTransport
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

if __package__:
    from .catalog_product_context import ProductContext
    from .catalog_runner import Invocation, ScenarioPlan, expand_text
    from .catalog_steps import ExecutionGap, StepReceipt, run_steps, validate_native_coverage
    from .openai_catalog_host import OpenAITurnCapture, run_openai_turn
else:
    from catalog_product_context import ProductContext
    from catalog_runner import Invocation, ScenarioPlan, expand_text
    from catalog_steps import ExecutionGap, StepReceipt, run_steps, validate_native_coverage
    from openai_catalog_host import OpenAITurnCapture, run_openai_turn


# SDK 2.54.0 inherits these unless the factory pins them explicitly. Check
# names only; never fetch, sanitize, log or temporarily replace their values.
OFFLINE_SDK_ENV = (
    "OPENAI_ADMIN_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_CUSTOM_HEADERS",
)


@dataclass(frozen=True)
class OpenAICapture:
    turns: tuple[OpenAITurnCapture, ...]
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
    path = f"/v1/agents/sessions/{turn.session_id}"
    session: Object = {
        "id": turn.session_id,
        "agent": {"id": "qa-agent", "model": fixture.model, "multi_agent": {"enabled": False}},
        "created_at": 0,
        "environment": {"type": "openai_hosted", "id": "qa-environment"},
        "status": "idle",
        "required_actions": [],
        "metadata": {"mux_tenant": tenant},
    }
    headers = (("openai-beta", "agents=v1"),)
    turns: Object = {"data": [t.snapshot for t in prior], "has_more": False}
    items: Object = {
        "data": [frame["item"] for t in prior for frame in t.frames if "item" in frame],
        "has_more": False,
    }
    replies = (
        WireReply(
            id="baseline-turns",
            method="GET",
            path=path + "/turns",
            query=(("order", "asc"), ("limit", "100")),
            headers=headers,
            response_json=turns,
        ),
        WireReply(
            id="baseline-items",
            method="GET",
            path=path + "/items",
            query=(("order", "asc"), ("limit", "100")),
            headers=headers,
            response_json=items,
            after=("baseline-turns",),
        ),
        WireReply(id="session", method="GET", path=path, headers=headers, response_json=session),
        WireReply(
            id="send-session",
            method="GET",
            path=path,
            headers=headers,
            response_json=session,
            after=("session",),
        ),
        tape.replies[1].model_copy(update={"after": ("send-session", "baseline-items")}),
        tape.replies[2],
    )
    return ProviderTape.model_validate({**tape.model_dump(), "replies": replies})


async def execute_openai_turns(
    plan: ScenarioPlan,
    scenario: ScenarioFixture,
    fixture: BackendFixture,
    *,
    source_root: Path,
    repository_root: Path,
    admin: AdminBindings | None = None,
    admin_values: Mapping[str, str] | None = None,
    product_context: ProductContext | None = None,
) -> OpenAICapture:
    """Validate all inputs, execute the owned prefix, retain later barriers."""
    if (
        plan.backend != "openai"
        or fixture.backend != "openai"
        or fixture not in scenario.providers
        or scenario.scenario_id != plan.scenario.id
        or scenario.source.sha256 != plan.scenario.sha256
        or scenario.source.path != f"scenarios/{plan.scenario.filename}"
        or fixture.profile != "openai.persistent_workspace"
        or fixture.model != "gpt-6-luna"
    ):
        raise ValueError("OpenAI fixture is not a complete pinned headless recipe")
    present_names = frozenset(os.environ)
    if any(name in present_names for name in OFFLINE_SDK_ENV):
        raise ValueError("offline OpenAI replay refuses ambient SDK options")
    fixture = BackendFixture.model_validate_json(fixture.model_dump_json())
    fixture.codec_source.resolve(repository_root)
    scenario.projection.verify(source_root)
    invocations = [i for i in plan.invocations if i.operation == "host_turn"]
    validate_native_coverage(plan, fixture)
    by_turn = {t.turn: t for t in fixture.turns}
    slots: dict[tuple[str, str], str] = {}
    for invocation in invocations:
        if invocation.turn is None or invocation.text is None:
            raise ValueError("OpenAI invocation lacks its original turn/text")
        native = by_turn.get(invocation.turn)
        if native is None:
            continue
        if (
            native.location != invocation.location
            or native.channel != invocation.channel
            or native.thread != invocation.thread
            or native.request_text != expand_text(invocation.text, scenario.values)
        ):
            raise ValueError("OpenAI fixture differs from the planned invocation")
        slot = (native.channel, native.thread)
        if slot in slots and slots[slot] != native.session_id:
            raise ValueError("OpenAI followup changes its selected session")
        if slot not in slots and native.session_id in slots.values():
            raise ValueError("OpenAI distinct host slots alias the same session")
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
        return OpenAICapture((), 0, steps.receipts, steps.gaps)
    store = MemoryStateStore()
    bindings: dict[tuple[str, str], ProviderBinding] = {}
    history: dict[str, list[NativeTurn]] = {}
    captures: list[OpenAITurnCapture] = []
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
                raise ValueError("OpenAI invocation lacks a turn")
            native = by_turn[invocation.turn]
            channel = next(b for b in plan.bindings if b.ref == invocation.channel)
            scope = Scope(
                tenant_id=channel.revision.channel.tenant_id,
                account_id="qa-caller",
                principal_id="offline-catalog-host",
                authorization_id="scripted-fixture",
            )
            slot = (native.channel, native.thread)
            if slot not in bindings:
                binding = ProviderBinding(
                    id=f"binding-{len(bindings)}",
                    thread=ThreadRef(channel=channel.revision.channel, thread_id=native.thread),
                    provider="openai",
                    profile=channel.revision.profile,
                    native_refs={
                        "session": native.session_id,
                        "agent": "qa-agent",
                        "environment": "qa-environment",
                        "model": fixture.model,
                    },
                    generation=1,
                    config_revision=channel.revision.local,
                    legacy_account_id=scope.account_id,
                )
                await store.put_binding(binding, expected_generation=0)
                bindings[slot] = binding
            prior = history.setdefault(native.session_id, [])
            tape = host_tape(scenario, fixture, native, tenant=scope.tenant_id, prior=tuple(prior))
            async with provider_replay(
                tape,
                backend="openai",
                profile=fixture.profile,
                model=fixture.model,
                source_root=source_root,
            ) as replay:
                if not isinstance(replay.transport, SDKTransport):
                    raise ValueError("OpenAI fixture factory returned a foreign transport")
                captures.append(
                    await run_openai_turn(
                        plan,
                        invocation,
                        source_sha256=scenario.source.sha256,
                        request_text=native.request_text,
                        values=scenario.values,
                        expected_root=native.root_id,
                        project="qa-offline-project",
                        transport=replay.transport,
                        sentinel=sentinel,
                        store=store,
                        binding=bindings[slot],
                        scope=scope,
                        timeout_s=timeout_s,
                    )
                )
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
        raise ValueError("OpenAI host attempted an Anthropic fallback")
    return OpenAICapture(tuple(captures), requests, steps.receipts, steps.gaps)

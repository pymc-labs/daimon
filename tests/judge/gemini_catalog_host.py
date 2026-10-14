"""Real G2 host turns over an explicitly supplied, source-pinned offline transport.

Fixture loading and strict wire consumption belong to the N3 replay consumer.
No transport, key, SDK client, session creation or provider fallback is discovered.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from daimon.core.channel_backend import check_backend
from daimon.core.errors import TurnError
from daimon.core.mux_backend import TurnBackendRequest, TurnRuntime
from daimon.core.turn.driver import run_turn
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.testing.discord_surface import DiscordSurfaceCapture
from daimon.testing.host_clock import current_host_clock
from daimon.testing.outcome_oracle import SessionUse, TerminalEvidence, TurnEvidence, VisibleText
from daimon.testing.platform_surface import PlatformSurfaceCapture
from daimon.testing.turn_clock import ClockedLifecycle, NativeRoot, await_root_completion
from mux.contracts.events import TurnEndedPayload
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import ProviderBinding
from mux.state.memory import MemoryStateStore
from mux.state.store import binding_slot

if __package__:
    from .catalog_runner import Invocation, ScenarioPlan, expand_text
    from .discord_catalog_surface import bind_discord_text
    from .headless_output import HeadlessOutput, capture_headless_return
    from .platform_catalog_surface import CatalogLifecycle, bind_platform_text
else:
    from catalog_runner import Invocation, ScenarioPlan, expand_text
    from discord_catalog_surface import bind_discord_text
    from headless_output import HeadlessOutput, capture_headless_return
    from platform_catalog_surface import CatalogLifecycle, bind_platform_text


@dataclass(frozen=True)
class GeminiTurnCapture:
    evidence: TurnEvidence
    output: HeadlessOutput | None
    surface: DiscordSurfaceCapture | None
    platform_surface: PlatformSurfaceCapture | None = None


async def run_gemini_turn(
    plan: ScenarioPlan,
    invocation: Invocation,
    *,
    source_sha256: str,
    request_text: str,
    values: dict[str, str],
    expected_root: str,
    project: str,
    runtime: TurnRuntime,
    sentinel: AsyncAnthropic,
    store: MemoryStateStore,
    binding: ProviderBinding,
    scope: Scope,
    timeout_s: float = 10.0,
) -> GeminiTurnCapture:
    if (
        plan.backend != "gemini"
        or binding.provider != "gemini"
        or binding.profile != "gemini.inline_reuse"
        or source_sha256 != plan.scenario.sha256
        or not expected_root
        or not project
        or invocation.turn is None
        or invocation.text is None
        or expand_text(invocation.text, values) != request_text
    ):
        raise ValueError("Gemini replay differs from its pinned invocation")
    channel = next(b for b in plan.bindings if b.ref == invocation.channel)
    check_backend(channel.revision)
    if channel.agent_model != "gemini-3.8-flash" or channel.revision.model != "gemini-3.8-flash":
        raise ValueError("Gemini replay differs from the selected model")
    if await store.latest_config_revision(channel.revision.channel) != channel.revision:
        raise ValueError("Gemini persisted revision differs from selected backend")
    if await store.get_binding(binding_slot(binding)) != binding:
        raise ValueError("Gemini selected persisted binding changed")
    session = ResourceRef(
        id=binding.native_refs["session"],
        kind="session",
        provider="gemini",
        account_scope_id=project,
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    key = f"host-turn-{invocation.turn}"
    persistence = TurnPersistence(store, binding, scope, operation_key=key)

    request = TurnBackendRequest(
        profile=channel.revision.profile,
        client=sentinel,
        scope=scope,
        session_id=session.id,
        session=session,
        config=channel.revision,
        runtime=runtime,
        read_timeout_s=5.0,
    )
    before = await store.read_events(session.id)
    prior_roots = {e.turn_id for e in before if e.turn_id is not None}
    if expected_root in prior_roots:
        raise ValueError("Gemini expected root already belongs to a prior invocation")
    cursor = before[-1].sequence if before else -1
    clock = current_host_clock()
    started = clock()
    output_id = f"{plan.scenario.id}:gemini:{session.id}:{expected_root}:headless_return"
    surface_id = output_id.replace(
        ":headless_return", f":{plan.scenario.scenario.get('surface')}_adapter_posts"
    )
    lifecycle = CatalogLifecycle(
        surface=str(plan.scenario.scenario.get("surface")),
        evidence_id=surface_id,
        session_id=session.id,
        root_turn_id=expected_root,
        model_id=channel.agent_model,
    )
    states: list[TurnState] = []
    clocked = ClockedLifecycle(lifecycle, clock=clock, started_s=started)
    output: HeadlessOutput | None = None

    async def native_root() -> NativeRoot:
        operation = await store.get_operation(scope, key + ":send:0")
        if (
            operation is None
            or operation.operation.status not in ("accepted", "processed")
            or not operation.result
        ):
            raise ValueError("Gemini input lacks a persisted accepted send claim")
        receipt = SendReceipt.model_validate(operation.result)
        if receipt.operation_id != "driver:" + key + ":send:0" or receipt.status not in (
            "queued",
            "processed",
        ):
            raise ValueError("Gemini input differs from its accepted owned operation")
        if receipt.turn_id != expected_root or receipt.input_ids != (expected_root,):
            raise ValueError("Gemini input differs from its acknowledged native root")
        journal = await store.read_events(session.id, after=cursor)
        if any(
            e.session_id != session.id
            or e.native.provider != "gemini"
            or (e.turn_id is not None and e.turn_id not in prior_roots | {expected_root})
            for e in journal
        ):
            raise ValueError("Gemini journal contains an unowned fresh root")
        if await store.get_binding(binding_slot(binding)) != binding:
            raise ValueError("native completion binding changed")
        return NativeRoot(session, expected_root, journal)

    with persistence.activate():

        async def invoke() -> str:
            state = await await_root_completion(
                run_turn(
                    anthropic=sentinel,
                    scope=scope,
                    session_id=session.id,
                    session_ref=session,
                    profile=channel.revision.profile,
                    backend_request=request,
                    path="mux",
                    user_message=request_text,
                    lifecycle=clocked,
                    cancel=asyncio.Event(),
                    billing=BillingExempt(reason="headless-unrecorded"),
                    stream_read_timeout_s=5.0,
                    render_interval_s=3600,
                ),
                lifecycle=clocked,
                timeout_s=timeout_s,
                native_root=native_root,
            )
            states.append(state)
            if state.error is not None:
                raise state.error
            return extract_final_response(state.content)

        async def captured() -> None:
            nonlocal output
            output = await capture_headless_return(invoke, evidence_id=output_id, clock=clock)

        try:
            await lifecycle.run(captured)
        except TurnError as error:
            if not states or states[-1].error is not error:
                raise
    observed = clock()
    if len(states) != 1 or len(lifecycle.terminal_success) + len(lifecycle.terminal_failures) != 1:
        raise ValueError("Gemini host invocation did not close exactly once")
    state = states[0]
    if state.error is None and output is None:
        raise ValueError("successful Gemini host has no returned output")
    if output is not None and (
        output.evidence_id != output_id
        or output.started_s < started
        or output.returned_s > observed
    ):
        raise ValueError("Gemini output differs from its owned root window")
    operation = await store.get_operation(scope, key + ":send:0")
    if (
        operation is None
        or operation.operation.status not in ("accepted", "processed")
        or not operation.result
    ):
        raise ValueError("Gemini input lacks a persisted accepted send claim")
    receipt = SendReceipt.model_validate(operation.result)
    if receipt.operation_id != "driver:" + key + ":send:0" or receipt.status not in (
        "queued",
        "processed",
    ):
        raise ValueError("Gemini input differs from its accepted owned operation")
    if receipt.turn_id != expected_root or receipt.input_ids != (expected_root,):
        raise ValueError("Gemini input differs from its acknowledged native root")
    journal = await store.read_events(session.id, after=cursor)
    if any(
        e.session_id != session.id
        or e.native.provider != "gemini"
        or (e.turn_id is not None and e.turn_id not in prior_roots | {expected_root})
        for e in journal
    ):
        raise ValueError("Gemini journal contains an unowned fresh root")
    roots = [e for e in journal if e.turn_id == expected_root and e.thread_id is None]
    completed = clocked.completion_time(
        session=session, root_turn_id=expected_root, journal=journal
    )
    terminals: list[TerminalEvidence] = []
    for event in roots:
        if event.type == "session.turn_ended":
            payload = TurnEndedPayload.model_validate(event.payload)
            if payload.root_turn_id != expected_root:
                raise ValueError("Gemini journal terminal disagrees with its root")
            terminals.append(
                TerminalEvidence(
                    evidence_id=f"{session.id}:{event.id}",
                    session_id=session.id,
                    root_turn_id=expected_root,
                    authority=event.authority,
                    outcome=payload.outcome,
                    observed_s=completed,
                )
            )
    if not roots or not any(t.authority in ("record", "reconciled") for t in terminals):
        raise ValueError("Gemini invocation has no authoritative fresh root terminal")
    if await store.get_binding(binding_slot(binding)) != binding:
        raise ValueError("Gemini persisted binding changed during host turn")
    if (state.error is not None or lifecycle.terminal_failures) and all(
        t.outcome == "completed" for t in terminals
    ):
        raise ValueError("Gemini host failed despite completed root evidence")
    evidence = TurnEvidence(
        turn=invocation.turn,
        slot_id=binding_slot(binding).model_dump_json(),
        session_id=session.id,
        root_turn_id=expected_root,
        started_s=started,
        terminals=tuple(terminals),
        sessions=(SessionUse(evidence_id=f"{key}:selected", session_id=session.id),),
        terminal_capture_complete=not any(e.authority == "gap" for e in journal),
        session_capture_complete=True,
        texts=(
            VisibleText(
                evidence_id=output.evidence_id,
                message_id=output.evidence_id,
                order=0,
                observed_s=output.returned_s,
                text=output.text,
            ),
        )
        if output is not None
        and output.has_visible_text
        and plan.scenario.scenario.get("surface") == "headless"
        else (),
        text_capture_complete=(
            output is not None and plan.scenario.scenario.get("surface") == "headless"
        ),
    )
    evidence = bind_discord_text(
        evidence,
        lifecycle.capture,
        expected_id=surface_id,
        observed_s=observed,
        accounting_required=any(
            a.get("kind") in {"footer_cost_matches_ledger", "ledger_matches_usage"}
            for a in plan.assertions
        ),
    )
    evidence = bind_platform_text(
        evidence,
        lifecycle.platform_capture,
        expected_id=surface_id,
        expected_platform=str(plan.scenario.scenario.get("surface")),
        observed_s=observed,
        accounting_required=any(
            a.get("kind") in {"footer_cost_matches_ledger", "ledger_matches_usage"}
            for a in plan.assertions
        ),
    )
    return GeminiTurnCapture(evidence, output, lifecycle.capture, lifecycle.platform_capture)

"""Offline catalog executor over the real mux turn driver and N3 outcome oracle.

Authored SDK tapes test host behavior. They do not certify model behavior, remote
files/tool execution, platform delivery, or production admission/billing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from daimon.core.channel_backend import check_backend
from daimon.core.mux_backend import TurnBackendRequest, turn_backend
from daimon.core.turn.driver import run_turn
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.posture import BillingExempt
from daimon.testing.ma import send_events_response
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from daimon.testing.outcome_oracle import (
    OutcomeReport,
    RunEvidence,
    SessionUse,
    Status,
    TerminalEvidence,
    TurnEvidence,
    evaluate,
)
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.events import TurnEndedPayload
from mux.contracts.ids import ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.drivers.anthropic.resources.session_tools import SessionTools
from mux.state.memory import MemoryStateStore
from mux.state.store import binding_slot
from pydantic import Field, JsonValue, computed_field, model_validator

if __package__:
    from .catalog_runner import (
        FROZEN_TARGET_SHA256,
        PROVIDERS,
        CatalogMatrix,
        Invocation,
        Record,
        ScenarioPlan,
        build_matrix,
        expand_text,
        sha,
    )
else:
    from catalog_runner import (
        FROZEN_TARGET_SHA256,
        PROVIDERS,
        CatalogMatrix,
        Invocation,
        Record,
        ScenarioPlan,
        build_matrix,
        expand_text,
        sha,
    )


class ReplayTurn(Record):
    turn: int = Field(ge=1)
    request_text: str
    events: tuple[dict[str, JsonValue], ...]

    @model_validator(mode="after")
    def native_identity(self) -> ReplayTurn:
        ids = [event.get("id") for event in self.events]
        if not ids or any(not isinstance(id_, str) or not id_ for id_ in ids):
            raise ValueError("replay needs nonempty native record IDs")
        if len(set(cast(list[str], ids))) != len(ids):
            raise ValueError("replay native record IDs must be unique")
        first = self.events[0]
        if first.get("type") != "user.message" or first.get("content") != [
            {"type": "text", "text": self.request_text}
        ]:
            raise ValueError("replay must begin with the submitted user message")
        return self


class ScenarioReplay(Record):
    scenario_id: str
    scenario_sha256: str
    backend: Literal["anthropic"] = "anthropic"
    profile: Literal["anthropic.managed_agents"] = "anthropic.managed_agents"
    agent_model: Literal["claude-haiku-5-5"] = "claude-haiku-5-5"
    # Explicit fixture values only. No environment/credential lookup occurs.
    values: dict[str, str] = {}
    turns: tuple[ReplayTurn, ...]

    @model_validator(mode="after")
    def unique_turns(self) -> ScenarioReplay:
        if len({turn.turn for turn in self.turns}) != len(self.turns):
            raise ValueError("repeated replay turn")
        ids = [event["id"] for turn in self.turns for event in turn.events]
        if len(set(cast(list[str], ids))) != len(ids):
            raise ValueError("replay native record IDs must be unique across turns")
        return self


class ExecutionGap(Record):
    code: str
    location: str


class CellResult(Record):
    scenario_id: str
    backend: Literal["anthropic", "openai", "gemini"]
    scored: bool
    source_sha256: str
    mode: Literal["offline_scripted"] = "offline_scripted"
    gaps: tuple[ExecutionGap, ...]
    evidence: RunEvidence
    catalog_outcome: OutcomeReport
    host_outcome: OutcomeReport
    request_count: int = 0
    # These are actual host lifecycle outputs, retained for the next oracle slice.
    rendered_text: tuple[str, ...] = ()

    @computed_field
    @property
    def status(self) -> Status:
        if self.catalog_outcome.status == "FAIL" or self.host_outcome.status == "FAIL":
            return "FAIL"
        if self.gaps:
            return "PENDING"
        return self.catalog_outcome.status


class ExecutionReport(Record):
    version: Literal[1] = 1
    integration_sha: str
    target_sha256: str
    target_ids: tuple[str, ...]
    scored_denominator: Literal[159] = 159
    cells: tuple[CellResult, ...]

    @model_validator(mode="after")
    def frozen_cells(self) -> ExecutionReport:
        canonical = sha(("\n".join(self.target_ids) + "\n").encode())
        if self.target_sha256 != FROZEN_TARGET_SHA256 or canonical != self.target_sha256:
            raise ValueError("execution report differs from frozen TARGET-53")
        pairs = {(cell.scenario_id, cell.backend) for cell in self.cells}
        expected = {(id_, backend) for id_ in self.target_ids for backend in PROVIDERS}
        if (
            len(self.target_ids) != 53
            or len(self.cells) != 159
            or pairs != expected
            or any(not cell.scored for cell in self.cells)
        ):
            raise ValueError("execution report must retain all 159 frozen scored cells")
        return self

    @computed_field
    @property
    def scored_counts(self) -> dict[str, int]:
        return {
            status: sum(c.scored and c.status == status for c in self.cells)
            for status in ("PASS", "FAIL", "PENDING")
        }


def _assertions(plan: ScenarioPlan, values: dict[str, str]) -> list[dict[str, JsonValue]]:
    # Round-trip into the oracle's JSON contract, retaining every original check.
    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return expand_text(value, values)
        if isinstance(value, list):
            return [expand(item) for item in cast(list[Any], value)]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in cast(dict[str, Any], value).items()}
        return value

    return cast(list[dict[str, JsonValue]], expand([*plan.assertions, *plan.global_assertions]))


def _host_assertions(evidence: RunEvidence) -> list[dict[str, JsonValue]]:
    checks: list[dict[str, JsonValue]] = []
    previous: dict[str, int] = {}
    for turn in evidence.turns:
        checks.extend(
            [
                {"kind": "turn_completed", "turn": turn.turn},
                {"kind": "no_session_replacement", "turn": turn.turn},
            ]
        )
        if turn.slot_id in previous:
            checks.append(
                {"kind": "same_session", "turn": turn.turn, "previous_turn": previous[turn.slot_id]}
            )
        previous[turn.slot_id] = turn.turn
    return checks


def _unbound_steps(plan: ScenarioPlan) -> tuple[ExecutionGap, ...]:
    gaps: list[ExecutionGap] = []
    for invocation in plan.invocations:
        if invocation.operation not in {"new_channel", "host_turn", "wait_done"}:
            gaps.append(ExecutionGap(code="STEP_BINDING_UNAVAILABLE", location=invocation.location))
        if invocation.fixture is not None:
            gaps.append(
                ExecutionGap(code="ATTACHMENT_BINDING_UNAVAILABLE", location=invocation.location)
            )
        if invocation.operation == "host_turn" and (
            invocation.source.get("do") not in {"mention", "thread_reply"}
            or invocation.source.get("reply_to") is not None
            or invocation.source.get("as", "user") != "user"
        ):
            gaps.append(
                ExecutionGap(code="ROUTING_BINDING_UNAVAILABLE", location=invocation.location)
            )
    if not any(i.operation == "host_turn" for i in plan.invocations):
        gaps.append(ExecutionGap(code="NO_SCRIPTABLE_HOST_TURN", location="scenario"))
    return tuple(gaps)


async def _run_turn(
    plan: ScenarioPlan,
    invocation: Invocation,
    tape: ReplayTurn,
    store: MemoryStateStore,
    binding: ProviderBinding,
    scope: Scope,
) -> tuple[TurnEvidence, int, str]:
    """Capture journal authority and all selections in this owned host window."""
    if invocation.turn is None:
        raise ValueError("host invocation has no catalog turn")
    channel = next(b for b in plan.bindings if b.ref == invocation.channel)
    if await store.latest_config_revision(channel.revision.channel) != channel.revision:
        raise ValueError("persisted channel revision differs from selected backend")
    check_backend(channel.revision)
    selected = await store.get_binding(binding_slot(binding))
    if selected != binding:
        raise ValueError("selected persisted binding changed")
    session = ResourceRef(
        id=binding.native_refs["session"],
        kind="session",
        provider="anthropic",
        account_scope_id="offline-scripted",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    key = f"host-turn-{invocation.turn}"
    persistence = TurnPersistence(store, binding, scope, operation_key=key)
    transport = ScriptedTransport()
    # Verify the prepared session's frozen agent model through real SDK parsing.
    native_session = ma_session(id=session.id, model=channel.agent_model)
    transport.queue(
        ScriptedReply(
            "GET",
            f"/v1/sessions/{session.id}",
            httpx.Response(200, json=native_session.model_dump(mode="json")),
        ),
        ScriptedReply.stream(
            f"/v1/sessions/{session.id}/events/stream",
            [cast(dict[str, object], event) for event in tape.events],
        ),
        ScriptedReply(
            "POST",
            f"/v1/sessions/{session.id}/events",
            send_events_response([tape.events[0]]),
            request_json={
                "events": [
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": tape.request_text}],
                    }
                ]
            },
            check_json=True,
        ),
    )
    lifecycle = RecordingLifecycle()
    started = time.monotonic()
    journal_before = len(await store.read_events(session.id))
    async with transport.client() as client:
        request = TurnBackendRequest(
            profile=channel.revision.profile,
            client=client,
            scope=scope,
            session_id=session.id,
            session=session,
            config=channel.revision,
            read_timeout_s=5.0,
        )
        backend = turn_backend(request)
        snapshot = await backend.backend.extension(
            SessionTools, namespace="anthropic.session_tools", version=1
        ).retrieve(scope, session)
        # The SDK session builder/parser validates this fixture; still check the exact pin.
        agent = snapshot.native.get("agent")
        model = agent.get("model") if isinstance(agent, dict) else None
        model_id = model.get("id") if isinstance(model, dict) else None
        if snapshot.native.get("id") != session.id or model_id != channel.agent_model:
            raise ValueError("prepared session differs from selected model/session")
        with persistence.activate():
            state = await asyncio.wait_for(
                run_turn(
                    anthropic=client,
                    session_id=session.id,
                    user_message=tape.request_text,
                    lifecycle=lifecycle,
                    cancel=asyncio.Event(),
                    billing=BillingExempt(reason="headless-unrecorded"),
                    path="mux",
                    scope=scope,
                    session_ref=session,
                    profile=channel.revision.profile,
                    backend_request=request,
                    stream_read_timeout_s=5.0,
                    render_interval_s=3600,
                ),
                timeout=10.0,
            )
    observed = time.monotonic()
    # assert_consumed uses bare asserts; explicit guards must survive Python -O.
    if transport.violations or transport.replies:
        raise ValueError("scripted HTTP capture did not close cleanly")
    if [event.model_dump(mode="json").get("id") for event in lifecycle.sse_events] != [
        event["id"] for event in tape.events
    ]:
        raise ValueError("scripted SSE capture did not consume every record")
    operation = await store.get_operation(scope, f"{key}:send:0")
    root = cast(str, tape.events[0]["id"])
    if operation is None or not operation.result or (operation.result.get("input_ids") != [root]):
        raise ValueError("root is not the acknowledged submitted input")
    journal = (await store.read_events(session.id))[journal_before:]
    if any(event.turn_id is not None and event.turn_id != root for event in journal):
        raise ValueError("journal record belongs to another root")
    terminals: list[TerminalEvidence] = []
    for event in journal:
        if event.type == "session.turn_ended":
            payload = TurnEndedPayload.model_validate(event.payload)
            if event.turn_id != root or payload.root_turn_id != root:
                raise ValueError("journal terminal differs from the acknowledged root")
            terminals.append(
                TerminalEvidence(
                    evidence_id=f"{session.id}:{event.id}",
                    session_id=event.session_id,
                    root_turn_id=payload.root_turn_id,
                    authority=event.authority,
                    outcome=payload.outcome,
                    observed_s=observed,
                )
            )
    # Lifecycle closure is a second host observation; it cannot substitute for a journal terminal.
    if len(lifecycle.terminal_success) + len(lifecycle.terminal_failures) != 1:
        raise ValueError("host terminal lifecycle did not close once")
    if await store.get_binding(binding_slot(binding)) != binding:
        raise ValueError("persisted binding changed during the turn")
    evidence = TurnEvidence(
        turn=invocation.turn,
        slot_id=binding_slot(binding).model_dump_json(),
        session_id=session.id,
        root_turn_id=root,
        started_s=started,
        terminals=tuple(terminals),
        sessions=(SessionUse(evidence_id=f"{key}:selected", session_id=session.id),),
        terminal_capture_complete=not any(event.authority == "gap" for event in journal),
        # Only this executor writes bindings; it never calls recovery or replacement hooks.
        session_capture_complete=True,
    )
    # Preserve recorded native failures, but never let a host failure PASS.
    if (state.error is not None or lifecycle.terminal_failures) and (
        not terminals or all(item.outcome == "completed" for item in terminals)
    ):
        raise ValueError("host failed despite a completed or missing journal terminal")
    return (
        evidence,
        len(transport.requests),
        "\n".join(block.text for block in state.content if block.kind == "text"),
    )


async def execute_cell(plan: ScenarioPlan, replay: ScenarioReplay | None) -> CellResult:
    gaps: list[ExecutionGap] = []
    evidence = RunEvidence(scenario_id=plan.scenario.id, backend=plan.backend, turns=())
    values = replay.values if replay is not None else {}
    try:
        assertions = _assertions(plan, values)
    except ValueError:
        # Preserve unresolved predicates. They cannot confer a PASS.
        assertions = cast(list[dict[str, JsonValue]], [*plan.assertions, *plan.global_assertions])
        gaps.append(ExecutionGap(code="PLACEHOLDER_UNBOUND", location="assertions"))
    if plan.backend != "anthropic":
        gaps.append(ExecutionGap(code="HOST_BACKEND_PENDING_G1_G2", location="channel"))
    else:
        gaps.extend(_unbound_steps(plan))
        if replay is None:
            gaps.append(ExecutionGap(code="REPLAY_FIXTURE_UNAVAILABLE", location="scenario"))
    if replay is not None and (
        replay.scenario_id != plan.scenario.id
        or replay.scenario_sha256 != plan.scenario.sha256
        or replay.backend != plan.backend
    ):
        raise ValueError("replay differs from the pinned catalog scenario/provider")
    host_invocations = [i for i in plan.invocations if i.operation == "host_turn"]
    tapes: dict[int, ReplayTurn] = {}
    if replay is not None:
        tapes = {t.turn: t for t in replay.turns}
        if set(tapes) != {i.turn for i in host_invocations}:
            raise ValueError("replay must cover exactly the planned host turns")
        for invocation in host_invocations:
            if invocation.turn is None or invocation.text is None:
                raise ValueError("host invocation lacks turn/text")
            if expand_text(invocation.text, replay.values) != tapes[invocation.turn].request_text:
                raise ValueError("replay request differs from the planned text")
    turns: list[TurnEvidence] = []
    rendered: list[str] = []
    requests = 0
    if not gaps and replay is not None:
        store = MemoryStateStore()
        bindings: dict[tuple[str, str], ProviderBinding] = {}
        for channel in plan.bindings:
            check_backend(channel.revision)
            await store.put_config_revision(channel.revision)
        for invocation in host_invocations:
            channel = next(b for b in plan.bindings if b.ref == invocation.channel)
            scope = Scope(
                tenant_id=channel.revision.channel.tenant_id,
                account_id="qa-caller",
                principal_id="offline-catalog-host",
                authorization_id="scripted-fixture",
            )
            slot = (channel.ref, invocation.thread or "")
            if slot not in bindings:
                binding = ProviderBinding(
                    id=f"binding-{len(bindings)}",
                    thread=ThreadRef(
                        channel=channel.revision.channel, thread_id=invocation.thread or ""
                    ),
                    provider="anthropic",
                    profile=channel.revision.profile,
                    native_refs={"session": f"session-{len(bindings)}"},
                    generation=1,
                    config_revision=channel.revision.local,
                    legacy_account_id=scope.account_id,
                )
                await store.put_binding(binding, expected_generation=0)
                bindings[slot] = binding
            turn, count, text = await _run_turn(
                plan, invocation, tapes[cast(int, invocation.turn)], store, bindings[slot], scope
            )
            turns.append(turn)
            requests += count
            rendered.append(text)
        evidence = RunEvidence(
            scenario_id=plan.scenario.id, backend=plan.backend, turns=tuple(turns)
        )
    gaps.extend(
        ExecutionGap(code="CATALOG_CAPABILITY_PENDING", location=gap.location) for gap in plan.gaps
    )
    return CellResult(
        scenario_id=plan.scenario.id,
        backend=plan.backend,
        scored=plan.scored,
        source_sha256=plan.scenario.sha256,
        gaps=tuple(gaps),
        evidence=evidence,
        catalog_outcome=evaluate(evidence, assertions),
        host_outcome=evaluate(evidence, _host_assertions(evidence)),
        request_count=requests,
        rendered_text=tuple(rendered),
    )


async def execute_catalog(
    root: Path,
    *,
    integration_sha: str,
    run_id: str,
    replays: tuple[ScenarioReplay, ...],
) -> ExecutionReport:
    # Always regenerate from pinned source files, never execute an unvalidated saved plan.
    matrix: CatalogMatrix = build_matrix(root, integration_sha=integration_sha, run_id=run_id)
    by_id = {replay.scenario_id: replay for replay in replays}
    if len(by_id) != len(replays) or set(by_id) - set(matrix.target_ids):
        raise ValueError("replays must uniquely identify frozen target scenarios")
    cells: list[CellResult] = []
    for plan in matrix.plans:
        if plan.scored:
            cells.append(
                await execute_cell(
                    plan, by_id.get(plan.scenario.id) if plan.backend == "anthropic" else None
                )
            )
    return ExecutionReport(
        integration_sha=matrix.integration_sha,
        target_sha256=matrix.target_sha256,
        target_ids=matrix.target_ids,
        cells=tuple(cells),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--replays", type=Path, required=True)
    parser.add_argument("--integration-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw: list[object] = json.loads(args.replays.read_text())
    replays = tuple(ScenarioReplay.model_validate(item) for item in raw)
    result = asyncio.run(
        execute_catalog(
            args.catalog.resolve(),
            integration_sha=args.integration_sha,
            run_id=args.run_id,
            replays=replays,
        )
    )
    args.output.write_text(result.model_dump_json(indent=2) + "\n")
    print(f"Offline scored cells: {result.scored_counts}; no live calls.")


if __name__ == "__main__":
    main()

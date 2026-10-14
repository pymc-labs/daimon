"""Ordered offline host steps; unsupported operations are execution barriers.

Only local channel revisions, real host turns, measured delays and owned root
completion waits are bound. Native fixture observations never prove remote
effects, admin mutations, ingress routing or scheduler/clock behavior.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, cast

from daimon.testing.admin_bindings import AdminBindings, AdminReceipt, parse_admin
from daimon.testing.host_clock import ClockSample, HostClock, current_host_clock
from daimon.testing.http_observation import HTTP_SOURCE_ASSERTIONS, HTTP_SOURCE_PINS
from daimon.testing.outcome_oracle import TurnEvidence
from daimon.testing.provider_fixtures import BackendFixture, FixtureGap
from daimon.testing.turn_clock import await_clocked_step
from pydantic import model_validator

if __package__:
    from .catalog_product_context import ProductContext, ProductContextPending
    from .catalog_runner import STEP_PARAMS, Invocation, Record, ScenarioPlan
else:
    from catalog_product_context import ProductContext, ProductContextPending
    from catalog_runner import STEP_PARAMS, Invocation, Record, ScenarioPlan

MAX_REAL_DELAY_S = 30.0
OFFLINE_TURN_WATCHDOG_S = 10.0
REMOTE_EFFECT_GAP = "Remote tool/workspace effects remain uncaptured."
OPERATION_GAP = "Runner must bind the preserved operation or asset."


class ExecutionGap(Record):
    code: str
    location: str


class StepReceipt(Record):
    location: str
    kind: str
    status: Literal["executed", "pending"]
    code: str
    started_s: float | None = None
    finished_s: float | None = None
    turn: int | None = None
    root_turn_id: str | None = None
    timeout_s: float | None = None
    delay_s: float | None = None
    admin: AdminReceipt | None = None
    started_clock: ClockSample | None = None
    finished_clock: ClockSample | None = None
    completion_clock: ClockSample | None = None
    admin: AdminReceipt | None = None

    @model_validator(mode="after")
    def bound_measurement(self) -> StepReceipt:
        if self.admin is not None and self.kind != "admin":
            raise ValueError("admin evidence belongs only to its source operation")
        if self.status == "pending":
            if any(
                v is not None
                for v in (
                    self.started_s,
                    self.finished_s,
                    self.root_turn_id,
                    self.timeout_s,
                    self.delay_s,
                    self.started_clock,
                    self.finished_clock,
                    self.completion_clock,
                )
            ):
                raise ValueError("pending step cannot claim a measured execution")
            if self.admin is not None and self.admin.status == "executed":
                raise ValueError("pending step cannot claim an executed admin operation")
            return self
        if self.kind == "admin" and (
            self.admin is None or self.admin.status != "executed" or not self.admin.observations
        ):
            raise ValueError("step has no owned execution hook")
        if (
            self.kind
            not in {"new_channel", "mention", "thread_reply", "wait", "wait_done", "admin"}
            or self.code != "BOUND"
        ):
            raise ValueError("step has no owned execution hook")
        if self.started_clock is not None or self.finished_clock is not None:
            a, b = self.started_clock, self.finished_clock
            if a is None or b is None or a.clock_id != b.clock_id or a.order >= b.order:
                raise ValueError("step clock samples do not share ordered host observations")
            if a.observed_s != self.started_s or b.observed_s != self.finished_s:
                raise ValueError("step clock samples differ from measured execution")
            c = self.completion_clock
            if c is not None and (c.clock_id != a.clock_id or not a.order < c.order < b.order):
                raise ValueError("native completion is outside the step clock window")
        elif self.completion_clock is not None:
            raise ValueError("completion clock has no step window")
        start, end = number(self.started_s), number(self.finished_s)
        if start is None or end is None or end < start:
            raise ValueError("step has no valid measured execution window")
        if self.kind in {"mention", "thread_reply", "wait_done"} and (
            not self.root_turn_id or self.turn is None or self.turn < 1
        ):
            raise ValueError("step has no acknowledged root identity")
        if self.timeout_s is not None and number(self.timeout_s, positive=True) is None:
            raise ValueError("step has an invalid completion budget")
        if self.delay_s is not None and number(self.delay_s) is None:
            raise ValueError("step has an invalid delay measurement")
        return self


@dataclass(frozen=True)
class StepRun:
    receipts: tuple[StepReceipt, ...]
    gaps: tuple[ExecutionGap, ...]


@dataclass(frozen=True)
class AdminStepBinding:
    binding: AdminBindings
    values: Mapping[str, str]


def number(value: object, *, positive: bool = False) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    n = float(value)
    return n if math.isfinite(n) and (n > 0 if positive else n >= 0) else None


def http_only_source(plan: ScenarioPlan) -> bool:
    """Narrow owner-bound, administrative source: never invent a host turn."""
    raw = plan.scenario.scenario
    expected = HTTP_SOURCE_ASSERTIONS.get(plan.scenario.id)
    return (
        expected is not None
        and HTTP_SOURCE_PINS.get(plan.scenario.id) == plan.scenario.sha256
        and raw.get("id") == plan.scenario.id
        and raw.get("set") == "A"
        and raw.get("surface") == "headless"
        and type(raw.get("est_turns")) is int
        and raw["est_turns"] == 0
        and not raw.get("human")
        and all(raw.get(section) == [] for section in ("setup", "steps", "teardown"))
        and not plan.invocations
        and not plan.bindings
        and not plan.global_assertions
        and list(plan.assertions) == raw.get("assert")
        and list(plan.assertions) == [e.model_dump(exclude_none=True) for e in expected]
        and all(
            g.reason == "http_check: no headless binding implemented"
            and g.location in {f"assert[{n}]" for n in range(len(expected))}
            for g in plan.gaps
        )
    )


def step_gaps(
    plan: ScenarioPlan, *, admin: AdminBindings | None = None
) -> tuple[ExecutionGap, ...]:
    gaps: list[ExecutionGap] = []
    for i in plan.invocations:
        kind = i.source.get("do")
        if kind in STEP_PARAMS and set(i.source) - STEP_PARAMS[kind]:
            gaps.append(ExecutionGap(code="STEP_PARAMETERS_UNBOUND", location=i.location))
        if i.operation not in {"new_channel", "host_turn", "wait", "wait_done"} and not (
            i.operation == "unbound" and kind == "admin" and admin is not None
        ):
            gaps.append(ExecutionGap(code="STEP_BINDING_UNAVAILABLE", location=i.location))
        if i.operation == "wait":
            delay = number(i.source.get("s"))
            if delay is None or delay > MAX_REAL_DELAY_S:
                gaps.append(ExecutionGap(code="STEP_BINDING_UNAVAILABLE", location=i.location))
        if (
            i.operation == "wait_done"
            and "timeout_s" in i.source
            and number(i.source["timeout_s"], positive=True) is None
        ):
            gaps.append(ExecutionGap(code="WAIT_DONE_TIMEOUT_UNBOUND", location=i.location))
        if i.fixture is not None:
            gaps.append(ExecutionGap(code="ATTACHMENT_BINDING_UNAVAILABLE", location=i.location))
        if i.operation == "host_turn" and (
            kind not in {"mention", "thread_reply"}
            or i.source.get("reply_to") is not None
            or i.source.get("as", "user") != "user"
        ):
            gaps.append(ExecutionGap(code="ROUTING_BINDING_UNAVAILABLE", location=i.location))
    if not any(i.operation == "host_turn" for i in plan.invocations):
        gaps.append(ExecutionGap(code="NO_SCRIPTABLE_HOST_TURN", location="scenario"))
    return tuple(gaps)


def validate_native_coverage(plan: ScenarioPlan, fixture: BackendFixture) -> None:
    """Missing turns require a pinned, explicit native prerequisite gap."""
    planned = {i.turn: i for i in plan.invocations if i.operation == "host_turn"}
    provided = {t.turn for t in fixture.turns}
    if len(planned) != sum(i.operation == "host_turn" for i in plan.invocations):
        raise ValueError("planned host turns are not unique")
    missing_locations = {
        g.location
        for g in fixture.gaps
        if g.code
        in {
            "MANUAL_TRIGGER",
            "NATIVE_TOOL_SCHEMA_UNBOUND",
            "ARTIFACT_PROTOCOL_UNBOUND",
            "HOST_GATE",
        }
    }
    if provided - set(planned) or any(
        i.location not in missing_locations for n, i in planned.items() if n not in provided
    ):
        raise ValueError("native fixture silently omits or invents a host turn")


def validate_step_sources(plan: ScenarioPlan) -> None:
    expected_locations: list[str] = []
    for section_name in ("setup", "steps", "teardown"):
        for n, step in enumerate(plan.scenario.scenario.get(section_name, [])):
            base = f"{section_name}[{n}]"
            texts = step.get("texts")
            if (
                step.get("do") == "burst"
                and isinstance(texts, list)
                and texts
                and all(isinstance(t, str) for t in cast(list[object], texts))
            ):
                expected_locations.extend(
                    f"{base}.texts[{k}]" for k in range(len(cast(list[object], texts)))
                )
            else:
                expected_locations.append(base)
    if (
        len({i.location for i in plan.invocations}) != len(plan.invocations)
        or [i.location for i in plan.invocations] != expected_locations
    ):
        raise ValueError("mapped steps omit or duplicate an original operation")
    turn = 0
    for i in plan.invocations:
        match = re.fullmatch(r"(setup|steps|teardown)\[(\d+)\](?:\.texts\[(\d+)\])?", i.location)
        if match is None:
            raise ValueError("step differs from pinned source operation")
        section = plan.scenario.scenario.get(match[1], [])
        index = int(match[2])
        if index >= len(section) or i.source != section[index]:
            raise ValueError("step differs from pinned source operation")
        kind = i.source.get("do")
        expected = (
            "new_channel"
            if kind == "new_channel"
            else "host_turn"
            if kind in {"mention", "burst", "dm"}
            else "context"
            if kind == "channel_message"
            or kind == "thread_reply"
            and i.source.get("mention") is not True
            else "host_turn"
            if kind == "thread_reply"
            else kind
            if kind in {"wait", "wait_done"}
            else "unbound"
        )
        if i.operation != expected and i.operation != "unbound":
            raise ValueError("step differs from pinned source operation")
        if i.operation in {"host_turn", "context"}:
            turn += 1
            if i.turn != turn:
                raise ValueError("step changes original turn numbering")
        elif i.operation in {"wait", "wait_done"} and i.turn != (turn or None):
            raise ValueError("wait changes its original trigger association")


def _deadline(plan: ScenarioPlan, trigger: Invocation) -> float:
    """The native turn is awaited inside the original catalog completion budget.

    The independent offline watchdog may refuse sooner, never later. A later
    unbound operation cannot be skipped to acquire a completion deadline.
    """
    following = False
    for i in plan.invocations:
        if i == trigger:
            following = True
            continue
        if not following:
            continue
        if i.operation not in {"wait", "wait_done"}:
            break
        if i.operation == "wait_done":
            if i.turn != trigger.turn:
                raise ValueError("catalog wait does not belong to its preceding host turn")
            value = number(i.source.get("timeout_s", OFFLINE_TURN_WATCHDOG_S), positive=True)
            if value is None:
                raise ValueError("catalog wait has an invalid completion budget")
            return min(value, OFFLINE_TURN_WATCHDOG_S)
    return OFFLINE_TURN_WATCHDOG_S


async def run_steps(
    plan: ScenarioPlan,
    *,
    invoke: Callable[[Invocation, float], Awaitable[TurnEvidence]],
    prepare_channel: Callable[[Invocation], Awaitable[None]],
    native_gaps: tuple[FixtureGap, ...] = (),
    native_fixture: BackendFixture | None = None,
    admin: AdminBindings | None = None,
    admin_values: Mapping[str, str] | None = None,
    product_context: ProductContext | None = None,
) -> StepRun:
    """Execute in source order and retain receipts for every skipped dependency.

    Admin calls use only N6's public binding and retain its readback receipts.
    A mutation only permits a subsequent opt-in host turn when the real product
    context records its own completed execution; absent preparation stays pending.
    An unbound admin/context/asset/routing/schema barrier stops subsequent work.
    Earlier independent turns remain usable partial evidence, never a full
    scenario PASS. Exact remote-effect gaps are observational and remain PENDING
    after native turns run. Unknown hook reasons remain execution barriers.
    """
    supplied_clock = current_host_clock()
    clock = supplied_clock if isinstance(supplied_clock, HostClock) else HostClock(supplied_clock)
    validate_step_sources(plan)
    if admin is not None and admin.backend != plan.backend:
        raise ValueError("admin binding differs from selected scenario provider")
    if product_context is not None and (
        admin is not product_context.admin or product_context.admin.backend != plan.backend
    ):
        raise ValueError("product context differs from owned admin binding")
    values = dict(admin_values or {})
    static = step_gaps(plan, admin=admin)
    gaps = list(static)
    blockers = {g.location for g in static}
    # A malformed completion budget cannot authorize its preceding dispatch.
    for i in plan.invocations:
        if i.operation == "host_turn":
            try:
                _deadline(plan, i)
            except ValueError:
                blockers.add(i.location)
    observations: list[FixtureGap] = []
    hooks: list[FixtureGap] = []
    locations = {i.location for i in plan.invocations}
    scoped: dict[str, set[str]] = {}
    if native_fixture is not None:
        native_fixture = BackendFixture.model_validate_json(native_fixture.model_dump_json())
    for gap in native_gaps:
        if gap.location in locations | {"human", "scenario", "steps"}:
            continue
        if (
            native_fixture is None
            or gap not in native_fixture.gaps
            or native_fixture.backend != plan.backend
            or native_fixture.mcp_binding is None
            or (gap.location, gap.code)
            not in {
                ("mcp", "RUNNER_HOOK"),
                ("mcp", "MCP_AUTH_UNBOUND"),
                ("mcp", "MCP_SERVER_LIMIT"),
                ("deepwiki", "EXTERNAL_TOOL_SCHEMA_UNBOUND"),
            }
        ):
            raise ValueError("native prerequisite gap has no original source operation")
        affected: set[str] = set()
        for turn in native_fixture.turns:
            if not any(
                gap.location == "mcp" or call.server == gap.location for call in turn.mcp_calls
            ):
                continue
            invocation = next((i for i in plan.invocations if i.location == turn.location), None)
            if (
                invocation is None
                or invocation.operation != "host_turn"
                or invocation.turn != turn.turn
                or invocation.channel != turn.channel
                or invocation.thread != turn.thread
            ):
                raise ValueError("native MCP prerequisite has a foreign source invocation")
            affected.add(invocation.location)
        if not affected:
            raise ValueError("native MCP prerequisite has no affected source invocation")
        if gap.code in {"MCP_AUTH_UNBOUND", "MCP_SERVER_LIMIT"}:
            # These are session configuration prerequisites, including turns
            # that do not happen to emit a tool call in their authored output.
            affected.update(i.location for i in plan.invocations if i.operation == "host_turn")
        scoped[gap.location] = affected | scoped.get(gap.location, set())
    for gap in native_gaps:
        if gap.location in scoped:
            blockers.update(scoped[gap.location])
            gaps.append(ExecutionGap(code="PROVIDER_" + gap.code, location=gap.location))
        elif gap.code == "RUNNER_HOOK" and gap.reason == REMOTE_EFFECT_GAP:
            observations.append(gap)
        elif gap.code == "RUNNER_HOOK" and gap.reason == OPERATION_GAP:
            hooks.append(gap)
        else:
            blockers.add(gap.location)
            gaps.append(ExecutionGap(code="PROVIDER_" + gap.code, location=gap.location))
    for hook in hooks:
        i = next((i for i in plan.invocations if i.location == hook.location), None)
        if i is None:
            blockers.add(hook.location)
            continue
        if (
            i.operation not in {"new_channel", "wait", "wait_done"}
            and not (i.operation == "unbound" and i.source.get("do") == "admin" and admin)
        ) or i.fixture is not None:
            blockers.add(i.location)
    receipts: list[StepReceipt] = []
    completed: dict[int, TurnEvidence] = {}
    admin_changed = False
    stopped = any(g.location in {"human", "scenario", "steps"} for g in native_gaps)
    for i in plan.invocations:
        kind = str(i.source.get("do", "missing_do"))
        if not stopped and admin_changed and i.operation == "host_turn" and product_context is None:
            gaps.append(ExecutionGap(code="ADMIN_HOST_CONTEXT_UNBOUND", location=i.location))
            blockers.add(i.location)
        if stopped or i.location in blockers:
            receipts.append(
                StepReceipt(
                    location=i.location,
                    kind=kind,
                    status="pending",
                    code="DEPENDENCY_BLOCKED" if stopped else "OPERATION_UNBOUND",
                    turn=i.turn,
                )
            )
            stopped = True
            continue
        start_sample = clock.sample()
        started = start_sample.observed_s
        completion_clock: ClockSample | None = None
        root: str | None = None
        timeout: float | None = None
        delay: float | None = None
        admin_receipt: AdminReceipt | None = None
        if i.operation == "new_channel":
            try:
                await prepare_channel(i)
            except ProductContextPending:
                if product_context is None:
                    raise
                receipts.append(
                    StepReceipt(
                        location=i.location,
                        kind=kind,
                        status="pending",
                        code="ADMIN_HOST_CONTEXT_UNBOUND",
                    )
                )
                gaps.append(ExecutionGap(code="ADMIN_HOST_CONTEXT_UNBOUND", location=i.location))
                stopped = True
                continue
        elif i.operation == "host_turn":
            timeout = _deadline(plan, i)
            try:
                measured = await await_clocked_step(
                    invoke(i, timeout), timeout_s=timeout, clock=clock
                )
                evidence = measured.value
                if measured.completion is not None:
                    proof = measured.completion
                    if (
                        proof.session_id != evidence.session_id
                        or proof.root_turn_id != evidence.root_turn_id
                    ):
                        raise ValueError(
                            "host evidence differs from independently verified completion"
                        )
                    completion_clock = proof.clock_sample
            except ProductContextPending:
                if product_context is None:
                    raise
                receipts.append(
                    StepReceipt(
                        location=i.location,
                        kind=kind,
                        status="pending",
                        code="ADMIN_HOST_CONTEXT_UNBOUND",
                        turn=i.turn,
                    )
                )
                gaps.append(ExecutionGap(code="ADMIN_HOST_CONTEXT_UNBOUND", location=i.location))
                stopped = True
                continue
            if product_context is not None:
                product_context.verify_execution(i, evidence)
            if evidence.turn != i.turn or not evidence.root_turn_id:
                raise ValueError("host step returned a foreign or missing turn identity")
            completed[evidence.turn] = evidence
            root = evidence.root_turn_id
        elif i.operation == "wait":
            delay = number(i.source.get("s"))
            if delay is None or delay > MAX_REAL_DELAY_S:
                raise ValueError("unbound delay reached execution")
            await asyncio.sleep(delay)
        elif i.operation == "wait_done":
            evidence = completed.get(i.turn) if i.turn is not None else None
            if (
                evidence is None
                or not evidence.terminal_capture_complete
                or not evidence.terminals
                or any(
                    t.authority not in {"record", "reconciled"}
                    or t.root_turn_id != evidence.root_turn_id
                    or t.session_id != evidence.session_id
                    for t in evidence.terminals
                )
            ):
                receipts.append(
                    StepReceipt(
                        location=i.location,
                        kind=kind,
                        status="pending",
                        code="WAIT_DONE_TERMINAL_UNVERIFIED",
                        turn=i.turn,
                    )
                )
                gaps.append(ExecutionGap(code="WAIT_DONE_TERMINAL_UNVERIFIED", location=i.location))
                stopped = True
                continue
            root = evidence.root_turn_id
            if "timeout_s" in i.source:
                timeout = number(i.source["timeout_s"], positive=True)
                if timeout is None or any(
                    t.observed_s - evidence.started_s > timeout for t in evidence.terminals
                ):
                    raise TimeoutError("catalog completion deadline exceeded")
        elif i.operation == "unbound" and kind == "admin" and admin is not None:
            # The public binding handles closed syntax, every DB bind, tenant,
            # account, destination authorization and real service readback.
            admin_receipt = await admin.execute(i.source, values)
            admin_receipt = AdminReceipt.model_validate_json(admin_receipt.model_dump_json())
            if admin_receipt.backend != plan.backend:
                raise ValueError("admin receipt differs from selected scenario provider")
            if admin_receipt.status != "executed":
                code = admin_receipt.code or "ADMIN_OPERATION_REFUSED"
                receipts.append(
                    StepReceipt(
                        location=i.location,
                        kind=kind,
                        status="pending",
                        code=code,
                        admin=admin_receipt,
                    )
                )
                gaps.append(ExecutionGap(code=code, location=i.location))
                stopped = True
                continue
            command = parse_admin(
                i.source, {"platform": admin.platform, "guild_id": admin.workspace_id, **values}
            )
            if admin_receipt.operation != command.operation or admin_receipt.code is not None:
                raise ValueError("admin receipt differs from original source operation")
            admin_changed = True
            values.update(admin_receipt.values)
        else:
            raise ValueError("unbound source operation reached execution")
        finish_sample = clock.sample()
        receipts.append(
            StepReceipt(
                location=i.location,
                kind=kind,
                status="executed",
                code="BOUND",
                started_s=started,
                finished_s=finish_sample.observed_s,
                started_clock=start_sample,
                finished_clock=finish_sample,
                completion_clock=completion_clock,
                turn=i.turn,
                root_turn_id=root,
                timeout_s=timeout,
                delay_s=delay,
                admin=admin_receipt,
            )
        )
    executed = {r.location for r in receipts if r.status == "executed"}
    gaps.extend(
        ExecutionGap(code="PROVIDER_RUNNER_HOOK", location=g.location)
        for g in (*observations, *hooks)
        if g in observations or g.location not in executed
    )
    # Unexecuted tail operations must keep the whole cell pending even when the
    # first blocker is only a native prerequisite, not a catalog portability gap.
    gaps.extend(
        ExecutionGap(code="STEP_DEPENDENCY_PENDING", location=r.location)
        for r in receipts
        if r.status == "pending"
    )
    return StepRun(tuple(receipts), tuple(gaps))

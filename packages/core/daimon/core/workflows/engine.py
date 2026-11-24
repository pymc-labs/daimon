"""Pure run transitions: how steps settle, which edges they take, joins, loops and limits.

The shell (`daimon.core.workflows.runs`) loads a run's steps, calls one function here and
writes back the `Transition` (changed steps, ledger events, the run's end) in the same
transaction. Branch, Transform and Finish run inline here; every other kind becomes a
`ready` step the steps loop executes.

Joins: a node with several forward incoming edges collects arrivals, taken or not, on one
`pending` step. It resolves once no active step can still reach it by forward edges (back
edges excluded, so the forward graph has no cycles): ready if any arrival was taken
(`join: all`), else skipped. `join: first` runs on the first taken arrival and cancels
the branches still leading into it. A back edge always starts a new visit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final, Literal

from daimon.core.workflows.expressions import ExpressionError, evaluate, expressions_in, render
from daimon.core.workflows.graph import Edge, ancestors, back_edges, edges_of
from daimon.core.workflows.spec import (
    MAX_TITLE,
    ActionNode,
    AgentNode,
    AnyNode,
    ApprovalNode,
    BranchNode,
    FinishNode,
    Spec,
    TransformNode,
    WaitNode,
)
from pydantic import JsonValue

StepStatus = Literal[
    "pending",
    "ready",
    "running",
    "waiting",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
    "unknown",
]
ErrorClass = Literal["retryable", "gate", "contract", "agent", "unknown", "fatal"]
RunStatus = Literal["queued", "running", "waiting", "succeeded", "failed", "cancelled", "timed_out"]
WaitingReason = Literal["approval", "question", "timer", "event", "blocked", "poll", "unknown"]

ACTIVE: Final = frozenset({"pending", "ready", "running", "waiting", "unknown"})
DEFAULT_MAX_VISITS: Final = 3
DEFAULT_MAX_STEPS: Final = 100
INLINE: Final = (BranchNode, TransformNode, FinishNode)
_FLOW_KINDS: Final = frozenset({"next", "route", "case", "else", "otherwise"})
_DEFAULT_ATTEMPTS: Final = {"agent": 2, "code": 2, "routine": 3}
_BACKOFF_S: Final = 20


def step_key(node_id: str, visit: int) -> str:
    """`node` for the first visit, `node#2` for later ones, as the run page shows them."""
    return node_id if visit == 1 else f"{node_id}#{visit}"


def item_key(node_id: str, visit: int, index: int) -> str:
    """`each[3]`: the fourth item of a For each visit."""
    return f"{step_key(node_id, visit)}[{index}]"


def edge_key(edge: Edge) -> str:
    return f"{edge.source}:{edge.slot}"


@dataclass(frozen=True)
class StepView:
    """One visit of a node as the engine sees it. `route` is a route label, or a Branch's slot."""

    node_id: str
    visit: int
    status: StepStatus
    route: str | None = None
    output: JsonValue = None
    summary: str | None = None
    via: tuple[str, ...] = ()
    error_class: ErrorClass | None = None
    error_message: str | None = None
    reused: Literal["fork", "pinned"] | None = None
    files: tuple[dict[str, JsonValue], ...] = ()

    @property
    def key(self) -> str:
        return step_key(self.node_id, self.visit)


@dataclass(frozen=True)
class Event:
    """A ledger event to append; the shell adds seq, time, actor and hashes."""

    type: str
    step: str | None
    data: dict[str, JsonValue]


@dataclass(frozen=True)
class RunEnd:
    status: Literal["succeeded", "failed", "cancelled", "timed_out"]
    outcome: Literal["completed", "nothing_to_do"] | None = None
    reason: str | None = None
    message: str | None = None
    step: str | None = None
    #: What the Finish asks to merge into `workflow.state` (a null value deletes the key).
    state: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True)
class Transition:
    steps: tuple[StepView, ...]
    events: tuple[Event, ...]
    end: RunEnd | None


@dataclass(frozen=True)
class Succeeded:
    output: JsonValue = None
    route: str | None = None
    summary: str | None = None
    #: Files API refs of the files the step made.
    files: tuple[dict[str, JsonValue], ...] = ()
    #: `finish_early`: the run ends here as succeeded with outcome `nothing_to_do`.
    early: bool = False
    #: A Finish's rendered `state`.
    state: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True)
class Failed:
    error_class: ErrorClass
    message: str
    #: The run's failure reason when this fails the run; `step_failed` when None.
    reason: str | None = None


@dataclass(frozen=True)
class Unknown:
    """The attempt died after a write may have happened; the owner decides."""

    message: str


Outcome = Succeeded | Failed | Unknown


@dataclass(frozen=True)
class RunFacts:
    """What templates read besides `steps`, and the run's limits."""

    inputs: Mapping[str, JsonValue]
    run: Mapping[str, JsonValue]
    workflow: Mapping[str, JsonValue]
    trigger: Mapping[str, JsonValue]
    now: datetime
    max_steps: int = DEFAULT_MAX_STEPS
    #: Test runs: outputs reused for these nodes instead of running them.
    pinned: Mapping[str, StepView] = field(default_factory=dict[str, StepView])


def project_status(steps: Sequence[StepView]) -> tuple[RunStatus, WaitingReason | None] | None:
    """Running while any step is ready or runs, else waiting while any waits; None when idle.

    A waiting step's reason is not on `StepView`; the shell passes `unknown` steps here and
    reads other reasons from the step rows, so this returns `unknown` or None as the reason.
    """
    statuses = {s.status for s in steps}
    if statuses & {"ready", "running"}:
        return "running", None
    if "unknown" in statuses:
        return "waiting", "unknown"
    if "waiting" in statuses:
        return "waiting", None
    return None


def retry_delay_s(node: AnyNode, attempt: int, outcome: Outcome) -> int | None:
    """Seconds until the next attempt, or None when this outcome is final.

    Only `retryable` errors retry, up to `retry.attempts` (agent and code steps default to
    2, routine steps 3, others 1); the wait doubles from 20 seconds.
    """
    if not isinstance(outcome, Failed) or outcome.error_class != "retryable":
        return None
    retry = getattr(node, "retry", None)
    attempts = retry.attempts if retry is not None else _DEFAULT_ATTEMPTS.get(node.kind, 1)
    return _BACKOFF_S * 2 ** (attempt - 1) if attempt < attempts else None


def resolve_inputs(
    spec: Spec, given: Mapping[str, JsonValue]
) -> tuple[dict[str, JsonValue], list[str]]:
    """The run's inputs with defaults filled, and what is wrong with them."""
    schema = spec.inputs
    if schema is None:
        return dict(given), []
    out: dict[str, JsonValue] = {}
    problems = [f"Unknown input: {k}" for k in given if k not in schema.properties]
    types: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "number": (int, float),
        "integer": (int,),
        "boolean": (bool,),
        "object": (dict,),
        "array": (list,),
    }
    for name, prop in schema.properties.items():
        value = given.get(name, prop.default)
        if value is None:
            if name in (schema.required or []):
                problems.append(f"Missing input: {name}")
            continue
        wanted = types.get(prop.type or "", (object,))
        if not isinstance(value, wanted) or (prop.type != "boolean" and isinstance(value, bool)):
            problems.append(f"Input {name} must be a {prop.type}")
        elif prop.enum and value not in prop.enum:
            problems.append(f"Input {name} must be one of {', '.join(prop.enum)}")
        out[name] = value
    return out, problems


def expression_context(
    spec: Spec, steps: Sequence[StepView], node_id: str, facts: RunFacts
) -> dict[str, JsonValue]:
    """What `node_id`'s templates read. `steps.<id>` is that node's latest settled visit.

    A skipped visit counts only when no visit ran: a visit refused by `max_visits` must not
    hide the output of the one before it, which the `on_max_visits` target reads. A step that
    has not run is absent, so `has()` guards hold, except a loop-carried one (upstream and
    downstream of `node_id` at once), which reads as null on the first pass.
    """
    latest: dict[str, StepView] = {}
    counts: dict[str, int] = {}

    def rank(s: StepView) -> tuple[bool, int]:
        return s.status != "skipped", s.visit

    for s in steps:
        if s.status not in ("pending", "skipped"):
            counts[s.node_id] = counts.get(s.node_id, 0) + 1
        if s.status not in ACTIVE and (
            s.node_id not in latest or rank(s) > rank(latest[s.node_id])
        ):
            latest[s.node_id] = s
    view: dict[str, JsonValue] = {}
    for nid, s in latest.items():
        view[nid] = {
            "output": s.output,
            "summary": s.summary,
            "route": s.route,
            "status": s.status,
            "files": list(s.files),
            "visits": counts.get(nid, 0),
        }
    up = ancestors(spec, node_id)
    for nid in up - set(view):
        if node_id in ancestors(spec, nid):
            view[nid] = {
                "output": None,
                "summary": None,
                "route": None,
                "status": None,
                "files": [],
                "visits": 0,
            }
    return {
        "inputs": dict(facts.inputs),
        "steps": view,
        "run": dict(facts.run),
        "workflow": dict(facts.workflow),
        "trigger": dict(facts.trigger),
    }


def _state_patch(
    node: FinishNode, context: Mapping[str, JsonValue], now: datetime
) -> dict[str, JsonValue] | None:
    """The Finish's `state` with its keys and values rendered."""
    if not node.state:
        return None
    patch: dict[str, JsonValue] = {}
    for key, value in node.state.items():
        name = render(key, context, now=now)
        if not isinstance(name, str) or not name or len(name) > MAX_TITLE:
            raise ExpressionError(f"A state key must be 1 to {MAX_TITLE} characters of text")
        patch[name] = render(value, context, now=now)
    return patch


class _Walk:
    """One transition's working copy of the run's steps."""

    def __init__(self, spec: Spec, steps: Sequence[StepView], facts: RunFacts) -> None:
        self.spec = spec
        self.facts = facts
        self.steps = list(steps)
        self.changed: dict[tuple[str, int], StepView] = {}
        self.events: list[Event] = []
        self.end: RunEnd | None = None
        back = back_edges(spec)
        self.back = {(e.source, e.slot) for e in back}
        forward: dict[str, set[str]] = {n: set() for n in spec.nodes}
        incoming: dict[str, int] = dict.fromkeys(spec.nodes, 0)
        for nid, node in spec.nodes.items():
            for e in edges_of(nid, node):
                if e.target in spec.nodes and (e.source, e.slot) not in self.back:
                    forward[nid].add(e.target)
                    incoming[e.target] += 1
        self.joins = {n for n, count in incoming.items() if count > 1}
        self.reach = {n: _closure(forward, n) for n in spec.nodes}

    # --- bookkeeping ---

    def put(self, step: StepView) -> StepView:
        for i, s in enumerate(self.steps):
            if s.node_id == step.node_id and s.visit == step.visit:
                self.steps[i] = step
                break
        else:
            self.steps.append(step)
        self.changed[(step.node_id, step.visit)] = step
        return step

    def emit(self, type: str, step: StepView | None, **data: JsonValue) -> None:
        self.events.append(Event(type, step.key if step else None, data))

    def latest(self, node_id: str) -> StepView | None:
        mine = [s for s in self.steps if s.node_id == node_id]
        return max(mine, key=lambda s: s.visit) if mine else None

    def next_visit(self, node_id: str) -> int:
        last = self.latest(node_id)
        return 1 if last is None else last.visit + 1

    def visits(self, node_id: str) -> int:
        return sum(
            1 for s in self.steps if s.node_id == node_id and s.status not in ("pending", "skipped")
        )

    def find(self, node_id: str, visit: int) -> StepView:
        for s in self.steps:
            if s.node_id == node_id and s.visit == visit:
                return s
        raise LookupError(f"No step {step_key(node_id, visit)}")

    # --- moves ---

    def enter(self, node_id: str, via: tuple[str, ...], visit: int | None = None) -> None:
        """A visit of `node_id` begins: limits, pinned outputs, then inline kinds run."""
        if self.end is not None:
            return
        node = self.spec.nodes[node_id]
        visit = visit or self.next_visit(node_id)
        taken = sum(1 for s in self.steps if s.status not in ("pending", "skipped"))
        if taken >= self.facts.max_steps:
            self.fail_run("max_steps", f"The run took {self.facts.max_steps} steps", None)
            return
        if not isinstance(node, FinishNode):
            limit = node.max_visits or DEFAULT_MAX_VISITS
            if self.visits(node_id) >= limit:
                skipped = self.put(
                    StepView(
                        node_id,
                        visit,
                        "skipped",
                        via=via,
                        error_message=f"Visited {limit} times",
                    )
                )
                self.emit("step.skipped", skipped, node=node_id, visit=visit, reason="max_visits")
                over = [e for e in edges_of(node_id, node) if e.kind == "max"]
                if over:
                    self.leave(skipped, over)
                else:
                    self.fail_run(
                        "max_visits", f"{_title(node_id, node)} ran {limit} times", skipped.key
                    )
                return
        step = self.put(StepView(node_id, visit, "ready", via=via))
        self.emit("step.ready", step, node=node_id, visit=visit, via=list(via))
        pinned = self.facts.pinned.get(node_id)
        if pinned is not None and not isinstance(node, FinishNode):
            self.emit("step.started", step, attempt=1)
            self.complete(step, Succeeded(pinned.output, pinned.route, pinned.summary), "pinned")
        elif isinstance(node, INLINE):
            self.run_inline(step, node)

    def run_inline(self, step: StepView, node: BranchNode | TransformNode | FinishNode) -> None:
        self.emit("step.started", step, attempt=1)
        context = expression_context(self.spec, self.steps, step.node_id, self.facts)
        now = self.facts.now
        outcome: Outcome
        try:
            match node:
                case BranchNode():
                    outcome = Succeeded(route=_branch_slot(node, context, now))
                case TransformNode():
                    outcome = Succeeded(output=render(dict(node.set), context, now=now))
                case FinishNode():
                    output = render(node.output, context, now=now)
                    if node.deliver is not None:
                        # A delivery whose templates fail fails the Finish, before any post.
                        render(node.deliver.model_dump(), context, now=now)
                    outcome = Succeeded(output=output, state=_state_patch(node, context, now))
        except ExpressionError as err:
            outcome = Failed("fatal", str(err))
        self.complete(step, outcome)

    def complete(
        self, step: StepView, outcome: Outcome, reused: Literal["fork", "pinned"] | None = None
    ) -> None:
        node = self.spec.nodes[step.node_id]
        if isinstance(outcome, Succeeded):
            outcome = _check_route(step.node_id, node, outcome)
        if isinstance(outcome, Unknown):
            unknown = self.put(
                replace(
                    step, status="unknown", error_class="unknown", error_message=outcome.message
                )
            )
            self.emit("step.unknown", unknown, message=outcome.message)
            return
        if isinstance(outcome, Failed):
            failed = self.put(
                replace(
                    step,
                    status="failed",
                    error_class=outcome.error_class,
                    error_message=outcome.message,
                )
            )
            self.emit(
                "step.failed", failed, error_class=outcome.error_class, message=outcome.message
            )
            handler = [e for e in edges_of(step.node_id, node) if e.kind == "error"]
            if handler:
                self.leave(failed, handler)
            else:
                self.fail_run(outcome.reason or "step_failed", outcome.message, failed.key)
            return
        done = self.put(
            replace(
                step,
                status="succeeded",
                output=outcome.output,
                route=outcome.route,
                summary=outcome.summary,
                files=outcome.files,
                reused=reused,
                error_class=None,
                error_message=None,
            )
        )
        taken = self.taken(done)
        if outcome.route is not None:
            self.emit(
                "step.route_chosen",
                done,
                route=outcome.route,
                slot=taken[0].slot if taken else None,
            )
        self.emit("step.succeeded", done, **({"reused": reused} if reused else {}))
        if outcome.early:
            self.end_run(RunEnd("succeeded", outcome="nothing_to_do", step=done.key))
            return
        if isinstance(node, FinishNode):
            if node.outcome == "failed":
                source = step.via[0].split(":", 1)[0] if step.via else None
                rejected = source is not None and isinstance(
                    self.spec.nodes.get(source), ApprovalNode
                )
                reason = "approval_rejected" if rejected else "step_failed"
                self.fail_run(reason, f"Ended at {_title(step.node_id, node)}", done.key)
            else:
                # A Finish with nothing to deliver or return, after no action ran, is a quiet
                # end, as a week with nothing to report.
                acted = any(
                    s.status == "succeeded"
                    and isinstance(self.spec.nodes.get(s.node_id), ActionNode)
                    for s in self.steps
                )
                quiet = node.output is None and node.deliver is None and not acted
                outcome_of = "nothing_to_do" if quiet else "completed"
                self.end_run(
                    RunEnd("succeeded", outcome=outcome_of, step=done.key, state=outcome.state)
                )
            return
        self.leave(done, taken)

    def taken(self, step: StepView) -> list[Edge]:
        edges = edges_of(step.node_id, self.spec.nodes[step.node_id])
        if isinstance(self.spec.nodes[step.node_id], BranchNode):
            return [e for e in edges if e.slot == step.route]
        # An approval's `otherwise` edge is labelled with its route name, `otherwise`.
        routed = [e for e in edges if e.kind in ("route", "otherwise") and e.label == step.route]
        return (
            routed if step.route is not None and routed else [e for e in edges if e.kind == "next"]
        )

    def leave(self, step: StepView, taken: list[Edge]) -> None:
        """Arrive along `taken`; every other flow edge arrives as not taken."""
        node = self.spec.nodes[step.node_id]
        for e in edges_of(step.node_id, node):
            if self.end is not None or e.target not in self.spec.nodes:
                return
            if e in taken:
                self.arrive(e, taken=True)
            elif e.kind in _FLOW_KINDS:
                self.arrive(e, taken=False)

    def arrive(self, edge: Edge, *, taken: bool) -> None:
        target = edge.target
        if (edge.source, edge.slot) in self.back:
            if taken:
                self.enter(target, (edge_key(edge),))
            return
        if target not in self.joins:
            if taken:
                self.enter(target, (edge_key(edge),))
            else:
                self.skip(StepView(target, self.next_visit(target), "skipped"))
            return
        last = self.latest(target)
        pending = last if last is not None and last.status == "pending" else None
        if pending is None:
            pending = self.put(StepView(target, self.next_visit(target), "pending"))
        if taken:
            pending = self.put(replace(pending, via=(*pending.via, edge_key(edge))))
            if self.spec.nodes[target].join == "first":
                self.cancel_blockers(target)
                self.enter(target, pending.via, pending.visit)

    def skip(self, step: StepView) -> None:
        skipped = self.put(replace(step, status="skipped"))
        self.emit("step.skipped", skipped, node=step.node_id, visit=step.visit, reason="not_taken")
        self.leave(skipped, [])

    def blocked(self, node_id: str) -> bool:
        return any(
            s.status in ACTIVE and s.node_id != node_id and node_id in self.reach[s.node_id]
            for s in self.steps
        )

    def cancel_blockers(self, node_id: str) -> None:
        for s in list(self.steps):
            if s.status in ACTIVE and s.node_id != node_id and node_id in self.reach[s.node_id]:
                self.emit(
                    "step.cancelled", self.put(replace(s, status="cancelled")), reason="join_first"
                )

    def drain(self) -> None:
        """Resolve joins nothing can reach any more, then fail a run that went idle."""
        progressed = True
        while progressed and self.end is None:
            progressed = False
            for s in self.steps:
                if s.status == "pending" and not self.blocked(s.node_id):
                    if s.via:
                        self.enter(s.node_id, s.via, s.visit)
                    else:
                        self.skip(s)
                    progressed = True
                    break
        if self.end is None and not any(s.status in ACTIVE for s in self.steps):
            self.fail_run("step_failed", "The run ended without reaching a Finish step", None)

    def fail_run(self, reason: str, message: str, step: str | None) -> None:
        self.end_run(RunEnd("failed", reason=reason, message=message, step=step))

    def end_run(self, end: RunEnd) -> None:
        if self.end is not None:
            return
        self.end = end
        for s in list(self.steps):
            if s.status in ACTIVE:
                self.emit(
                    "step.cancelled", self.put(replace(s, status="cancelled")), reason=end.status
                )
        data: dict[str, JsonValue] = {"outcome": end.outcome} if end.outcome else {}
        if end.reason:
            data |= {"reason": end.reason, "message": end.message}
        self.events.append(Event(f"run.{end.status}", end.step, data))

    def result(self) -> Transition:
        return Transition(tuple(self.changed.values()), tuple(self.events), self.end)


def _closure(adjacency: Mapping[str, set[str]], root: str) -> frozenset[str]:
    seen: set[str] = set()
    stack = list(adjacency.get(root, ()))
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack += adjacency.get(n, ())
    return frozenset(seen)


def _title(node_id: str, node: AnyNode) -> str:
    return node.title or node_id


def _branch_slot(node: BranchNode, context: dict[str, JsonValue], now: datetime) -> str:
    for i, case in enumerate(node.cases):
        sources = expressions_in(case.when)
        value = evaluate(sources[0], context, now=now) if len(sources) == 1 else None
        if not isinstance(value, bool):
            raise ExpressionError(f"cases.{i}.when is not true or false")
        if value:
            return f"cases.{i}"
    if node.else_ is None:
        raise ExpressionError("No case matched and there is no else")
    return "else"


def _check_route(node_id: str, node: AnyNode, outcome: Succeeded) -> Outcome:
    if not isinstance(node, AgentNode | ApprovalNode | WaitNode) or not node.routes:
        return outcome
    otherwise = isinstance(node, ApprovalNode) and outcome.route == "otherwise" and node.otherwise
    if outcome.route is not None and outcome.route not in node.routes and not otherwise:
        return Failed("contract", f"Unknown route: {outcome.route}", reason="contract_invalid")
    if outcome.route is None and not node.next:
        return Failed(
            "contract", f"{_title(node_id, node)} must choose a route", reason="contract_invalid"
        )
    return outcome


# --- public transitions ---


def begin(spec: Spec, facts: RunFacts) -> Transition:
    """A new run enters its start node."""
    walk = _Walk(spec, (), facts)
    walk.enter(spec.start, ())
    walk.drain()
    return walk.result()


def enter_at(
    spec: Spec, steps: Sequence[StepView], node_id: str, via: tuple[str, ...], facts: RunFacts
) -> Transition:
    """A fork: the copied steps are in `steps`; the run enters `node_id`."""
    walk = _Walk(spec, steps, facts)
    walk.enter(node_id, via)
    walk.drain()
    return walk.result()


def resolve(
    spec: Spec,
    steps: Sequence[StepView],
    node_id: str,
    visit: int,
    outcome: Outcome,
    facts: RunFacts,
) -> Transition:
    """An executed step's final outcome: record it and follow its edges."""
    walk = _Walk(spec, steps, facts)
    walk.complete(walk.find(node_id, visit), outcome)
    walk.drain()
    return walk.result()


def retry_step(
    spec: Spec, steps: Sequence[StepView], node_id: str, visit: int, facts: RunFacts
) -> Transition:
    """Run a failed, cancelled or unknown step again, as the same visit."""
    walk = _Walk(spec, steps, facts)
    _reopen(walk, walk.find(node_id, visit))
    walk.drain()
    return walk.result()


def _reopen(walk: _Walk, step: StepView) -> None:
    node = walk.spec.nodes[step.node_id]
    if step.node_id in walk.joins:  # `drain` enters it once its branches are done
        walk.put(replace(step, status="pending", error_class=None, error_message=None))
        return
    ready = walk.put(replace(step, status="ready", error_class=None, error_message=None))
    walk.emit("step.ready", ready, node=step.node_id, visit=step.visit, via=list(step.via))
    if isinstance(node, INLINE):
        walk.run_inline(ready, node)


def redrive(spec: Spec, steps: Sequence[StepView], facts: RunFacts) -> Transition | None:
    """Reopen the latest failed and cancelled visits; None when there is nothing to re-run."""
    walk = _Walk(spec, steps, facts)
    latest = {s.node_id: walk.latest(s.node_id) for s in steps}
    again = [s for s in latest.values() if s is not None and s.status in ("failed", "cancelled")]
    if not again:
        return None
    for s in again:
        if walk.end is None:
            _reopen(walk, walk.find(s.node_id, s.visit))
    walk.drain()
    return walk.result()


def stop(steps: Sequence[StepView], spec: Spec, facts: RunFacts, end: RunEnd) -> Transition:
    """End the run from outside (cancel, deadline, budget): active steps are cancelled."""
    walk = _Walk(spec, steps, facts)
    walk.end_run(end)
    return walk.result()

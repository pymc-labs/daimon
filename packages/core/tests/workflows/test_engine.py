"""Pure engine transitions: edges, joins, loops, limits and recovery."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from daimon.core.workflows import engine
from daimon.core.workflows.engine import Failed, RunFacts, StepView, Succeeded, Transition
from daimon.core.workflows.ledger import GENESIS, event_hash, verify
from daimon.core.workflows.spec import Spec, parse_spec

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def spec_of(nodes: dict[str, Any], start: str = "a", **extra: Any) -> Spec:
    return parse_spec(
        {"schema": "daimon.workflow/v1", "name": "t", "start": start, "nodes": nodes, **extra}
    )


def agent(**flow: Any) -> dict[str, Any]:
    return {"kind": "agent", "instructions": "Do it", **flow}


FINISH: dict[str, Any] = {"kind": "finish", "output": "${{ steps.a.output }}"}


def facts(**kw: Any) -> RunFacts:
    return RunFacts(inputs=kw.pop("inputs", {}), run={}, workflow={}, trigger={}, now=NOW, **kw)


class Run:
    """Applies transitions to a step list the way the shell does."""

    def __init__(self, spec: Spec, **kw: Any) -> None:
        self.spec, self.facts = spec, facts(**kw)
        self.steps: list[StepView] = []
        self.events: list[str] = []
        self.end: engine.RunEnd | None = None
        self.apply(engine.begin(spec, self.facts))

    def apply(self, t: Transition) -> None:
        for s in t.steps:
            self.steps = [x for x in self.steps if (x.node_id, x.visit) != (s.node_id, s.visit)] + [
                s
            ]
        self.events += [e.type for e in t.events]
        self.end = t.end or self.end

    def settle(self, node: str, outcome: engine.Outcome, visit: int | None = None) -> None:
        visit = visit or max(s.visit for s in self.steps if s.node_id == node)
        self.apply(engine.resolve(self.spec, self.steps, node, visit, outcome, self.facts))

    def status(self) -> dict[str, str]:
        return {s.key: s.status for s in sorted(self.steps, key=lambda s: s.key)}


def test_branch_transform_finish_run_inline() -> None:
    spec = spec_of(
        {
            "b": {
                "kind": "branch",
                "cases": [{"when": "${{ inputs.n > 1 }}", "to": "t"}],
                "else": "f",
            },
            "t": {"kind": "transform", "set": {"double": "${{ inputs.n * 2 }}"}, "next": "f"},
            "f": {"kind": "finish", "output": "${{ has(steps.t) ? steps.t.output.double : 0 }}"},
        },
        start="b",
    )
    run = Run(spec, inputs={"n": 3})
    assert run.end is not None and run.end.status == "succeeded", f"run should finish: {run.end}"
    assert [s.output for s in run.steps if s.node_id == "f"] == [6], (
        "finish renders the transform output"
    )
    assert "step.route_chosen" in run.events and run.events[-1] == "run.succeeded", (
        f"events: {run.events}"
    )
    other = Run(spec, inputs={"n": 0})
    assert other.status()["t"] == "skipped", "the case not taken is skipped"


def test_parallel_next_waits_at_the_join() -> None:
    spec = spec_of(
        {
            "a": agent(next=["b", "c"]),
            "b": agent(next="d"),
            "c": agent(next="d"),
            "d": agent(next="f"),
            "f": FINISH,
        }
    )
    run = Run(spec)
    run.settle("a", Succeeded({"x": 1}))
    assert run.status() == {"a": "succeeded", "b": "ready", "c": "ready"}, "both branches start"
    run.settle("b", Succeeded())
    assert run.status()["d"] == "pending", "the join waits for c"
    run.settle("c", Succeeded())
    assert run.status()["d"] == "ready", "the join runs once both arrived"
    d = next(s for s in run.steps if s.node_id == "d")
    assert set(d.via) == {"b:next.0", "c:next.0"}, f"via records both arrivals: {d.via}"
    run.settle("d", Succeeded())
    assert run.end is not None and run.end.status == "succeeded", "the run finishes"


def test_join_runs_after_a_skipped_branch() -> None:
    spec = spec_of(
        {
            "a": agent(next="r"),
            "r": {"kind": "branch", "cases": [{"when": "${{ true }}", "to": "b"}], "else": "c"},
            "b": agent(next="d"),
            "c": agent(next="d"),
            "d": agent(next="f"),
            "f": FINISH,
        }
    )
    run = Run(spec)
    run.settle("a", Succeeded())
    assert run.status()["c"] == "skipped" and run.status()["d"] == "pending", (
        f"c skipped: {run.status()}"
    )
    run.settle("b", Succeeded())
    assert run.status()["d"] == "ready", "the skipped branch does not hold the join"


def test_join_first_cancels_the_slower_branch() -> None:
    spec = spec_of(
        {
            "a": agent(next=["b", "c"]),
            "b": agent(next="d"),
            "c": agent(next="d"),
            "d": agent(next="f", join="first"),
            "f": FINISH,
        }
    )
    run = Run(spec)
    run.settle("a", Succeeded())
    run.settle("b", Succeeded())
    assert run.status()["d"] == "ready" and run.status()["c"] == "cancelled", (
        f"first wins: {run.status()}"
    )


LOOP: dict[str, Any] = {
    "a": agent(next="r", max_visits=2),
    "r": {
        "kind": "branch",
        "cases": [{"when": "${{ steps.a.output.again }}", "to": "a"}],
        "else": "f",
    },
    "f": FINISH,
}


def test_loop_enters_new_visits_until_max_visits() -> None:
    run = Run(spec_of(LOOP))
    run.settle("a", Succeeded({"again": True}))
    assert run.status()["a#2"] == "ready", f"the back edge starts visit 2: {run.status()}"
    run.settle("a", Succeeded({"again": True}))
    assert run.end is not None and run.end.reason == "max_visits", (
        f"third visit fails the run: {run.end}"
    )
    assert run.status()["a#3"] == "skipped", "the refused visit is recorded"

    looped = spec_of({**LOOP, "a": agent(next="r", max_visits=1, on_max_visits="f")})
    run = Run(looped)
    run.settle("a", Succeeded({"again": True}))
    assert run.end is not None and run.end.status == "succeeded", (
        f"on_max_visits leads on: {run.end}"
    )


def test_visits_and_loop_carried_refs() -> None:
    spec = spec_of(
        {
            "a": agent(next="t"),
            "t": {
                "kind": "transform",
                "set": {"n": "${{ steps.a.visits }}", "prev": "${{ steps.t.output }}"},
                "next": "r",
            },
            "r": {
                "kind": "branch",
                "cases": [{"when": "${{ steps.a.visits < 2 }}", "to": "a"}],
                "else": "f",
            },
            "f": FINISH,
        }
    )
    run = Run(spec)
    run.settle("a", Succeeded())
    first = next(s for s in run.steps if s.key == "t")
    assert first.output == {"n": 1, "prev": None}, (
        f"t reads itself as null on the first pass: {first.output}"
    )
    run.settle("a", Succeeded())
    second = next(s for s in run.steps if s.key == "t#2")
    assert second.output == {"n": 2, "prev": {"n": 1, "prev": None}}, (
        f"loop-carried value: {second.output}"
    )


def test_failure_takes_on_error_or_fails_the_run() -> None:
    spec = spec_of({"a": agent(next="f", on_error="h"), "h": agent(next="f"), "f": FINISH})
    run = Run(spec)
    run.settle("a", Failed("agent", "boom"))
    assert run.status() == {"a": "failed", "f": "pending", "h": "ready"}, (
        f"on_error handles it: {run.status()}"
    )
    bare = Run(spec_of({"a": agent(next="f"), "f": FINISH}))
    bare.settle("a", Failed("agent", "boom"))
    assert bare.end is not None and bare.end.reason == "step_failed", (
        f"no handler fails the run: {bare.end}"
    )


def test_unknown_route_is_a_contract_failure() -> None:
    run = Run(spec_of({"a": agent(routes={"yes": "f"}), "f": FINISH}))
    run.settle("a", Succeeded(route="maybe"))
    assert run.end is not None and run.end.reason == "contract_invalid", f"bad route: {run.end}"


def test_pinned_outputs_skip_execution() -> None:
    pinned = {"a": StepView("a", 1, "succeeded", output={"x": 7})}
    run = Run(spec_of({"a": agent(next="f"), "f": FINISH}), pinned=pinned)
    a = next(s for s in run.steps if s.node_id == "a")
    assert a.reused == "pinned" and run.end is not None, "a test run reuses the pinned output"
    assert [s.output for s in run.steps if s.node_id == "f"] == [{"x": 7}], "downstream reads it"


def test_redrive_reopens_failed_and_cancelled_steps() -> None:
    spec = spec_of(
        {
            "a": agent(next=["b", "c"]),
            "b": agent(next="d"),
            "c": agent(next="d"),
            "d": agent(next="f"),
            "f": FINISH,
        }
    )
    run = Run(spec)
    run.settle("a", Succeeded())
    run.settle("b", Failed("agent", "boom"))
    assert run.status() == {"a": "succeeded", "b": "failed", "c": "cancelled"}, f"{run.status()}"
    run.end = None
    t = engine.redrive(spec, run.steps, run.facts)
    assert t is not None, "there is something to redrive"
    run.apply(t)
    assert (run.status()["b"], run.status()["c"]) == ("ready", "ready"), f"{run.status()}"
    run.settle("b", Succeeded())
    run.settle("c", Succeeded())
    assert run.status()["d"] == "ready", "the join runs again after the redriven branches"


def test_max_steps_and_stop() -> None:
    run = Run(spec_of({"a": agent(next="b"), "b": agent(next="f"), "f": FINISH}), max_steps=1)
    run.settle("a", Succeeded())
    assert run.end is not None and run.end.reason == "max_steps", f"step limit: {run.end}"
    live = Run(spec_of({"a": agent(next="f"), "f": FINISH}))
    t = engine.stop(
        live.steps, live.spec, live.facts, engine.RunEnd("cancelled", reason="cancelled_by")
    )
    assert [s.status for s in t.steps] == ["cancelled"] and t.events[-1].type == "run.cancelled", (
        "cancel"
    )


def test_retry_delay_and_inputs() -> None:
    node = spec_of({"a": agent(retry={"attempts": 3}), "f": FINISH}).nodes["a"]
    retryable = Failed("retryable", "busy")
    delays = [engine.retry_delay_s(node, n, retryable) for n in (1, 2, 3)]
    assert delays == [20, 40, None], f"backoff doubles until attempts run out: {delays}"
    assert engine.retry_delay_s(node, 1, Failed("agent", "no")) is None, (
        "only retryable errors retry"
    )
    spec = spec_of(
        {"a": FINISH},
        inputs={
            "type": "object",
            "required": ["q"],
            "properties": {"q": {"type": "string"}, "n": {"type": "integer", "default": 2}},
        },
    )
    assert engine.resolve_inputs(spec, {"q": "hi"}) == ({"q": "hi", "n": 2}, []), "defaults fill in"
    assert engine.resolve_inputs(spec, {"n": True})[1] == [
        "Missing input: q",
        "Input n must be a integer",
    ], "checks"


def test_project_status() -> None:
    def views(*statuses: str) -> Sequence[StepView]:
        return [
            replace(StepView("a", i + 1, "succeeded"), status=s) for i, s in enumerate(statuses)
        ]  # type: ignore[arg-type]

    assert engine.project_status(views("succeeded", "ready")) == ("running", None), (
        "ready means running"
    )
    assert engine.project_status(views("unknown")) == ("waiting", "unknown"), (
        "unknown waits on the owner"
    )
    assert engine.project_status(views("succeeded")) is None, "idle"


def test_ledger_chain_verifies_and_detects_edits() -> None:
    run_id = uuid4()

    class E:
        def __init__(self, seq: int, prev: str, data: dict[str, Any]) -> None:
            self.seq, self.at, self.type, self.step_key, self.actor = (
                seq,
                NOW,
                "step.ready",
                "a",
                "engine",
            )
            self.data, self.prev_hash = data, prev
            self.hash = event_hash(
                prev,
                run_id=run_id,
                seq=seq,
                at=NOW,
                type=self.type,
                step="a",
                actor="engine",
                data=data,
            )

    first = E(1, GENESIS, {"node": "a"})
    second = E(2, first.hash, {"node": "b"})
    assert verify(run_id, [first, second]).ok, "an untouched chain verifies"
    second.data = {"node": "c"}
    result = verify(run_id, [first, second])
    assert not result.ok and result.bad_seq == 2, f"an edited event breaks the chain: {result}"

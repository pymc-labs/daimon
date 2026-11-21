"""A spec as a graph: edges, reachability, loops, the steps sure to have run, and templates.

Mirrors `edgesOf`, `reachable`, `ancestors` and `backEdges` in
`web/src/modules/workflows/spec.ts`, so the canvas and the server agree on the shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from daimon.core.workflows.spec import (
    ActionNode,
    AgentNode,
    AnyNode,
    ApprovalNode,
    BranchNode,
    CodeNode,
    Deliver,
    FinishNode,
    ForEachNode,
    PostNode,
    Spec,
    TransformNode,
    WaitNode,
    WorkflowNode,
)
from pydantic import JsonValue

EdgeKind = Literal["next", "route", "case", "else", "error", "max", "otherwise"]
#: Which names a template may read: a node's own data, a For each item, a trigger's, a
#: poll's result, or a run's card (any step, read when it settles).
Scope = Literal["node", "item", "payload", "event", "run", "mention", "poll", "result", "card"]


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: EdgeKind
    slot: str
    label: str | None = None


@dataclass(frozen=True)
class Template:
    """One string of a spec that may hold `${{ }}` expressions, and where it sits."""

    field: str
    text: str
    scope: Scope


def edges_of(node_id: str, node: AnyNode) -> list[Edge]:
    if isinstance(node, FinishNode):
        return []
    out: list[Edge] = []
    nexts = [] if node.next is None else [node.next] if isinstance(node.next, str) else node.next
    out += [Edge(node_id, t, "next", f"next.{i}") for i, t in enumerate(nexts)]
    if isinstance(node, AgentNode | ApprovalNode | WaitNode) and node.routes:
        for label, route in node.routes.items():
            target = route if isinstance(route, str) else route.to
            out.append(Edge(node_id, target, "route", f"routes.{label}", label))
    if isinstance(node, BranchNode):
        out += [Edge(node_id, c.to, "case", f"cases.{i}") for i, c in enumerate(node.cases)]
        if node.else_:
            out.append(Edge(node_id, node.else_, "else", "else", "else"))
    if isinstance(node, ApprovalNode) and node.otherwise:
        out.append(Edge(node_id, node.otherwise, "otherwise", "otherwise", "otherwise"))
    if node.on_error and node.on_error != "fail":
        out.append(Edge(node_id, node.on_error, "error", "on_error", "error"))
    if node.on_max_visits:
        out.append(Edge(node_id, node.on_max_visits, "max", "on_max_visits"))
    return out


def all_edges(spec: Spec) -> list[Edge]:
    return [e for node_id, node in spec.nodes.items() for e in edges_of(node_id, node)]


def _adjacency(spec: Spec, *, reverse: bool = False) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in all_edges(spec):
        a, b = (e.target, e.source) if reverse else (e.source, e.target)
        if b in spec.nodes:
            out.setdefault(a, []).append(b)
    return out


def _closure(adjacency: dict[str, list[str]], roots: list[str]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        node_id = stack.pop()
        if node_id not in seen:
            seen.add(node_id)
            stack += adjacency.get(node_id, [])
    return seen


def reachable(spec: Spec) -> set[str]:
    return _closure(_adjacency(spec), [spec.start] if spec.start in spec.nodes else [])


def ancestors(spec: Spec, node_id: str) -> set[str]:
    """Every node with a path to `node_id`, loops included (a loop body reads earlier visits)."""
    return _closure(_adjacency(spec, reverse=True), _adjacency(spec, reverse=True).get(node_id, []))


def back_edges(spec: Spec) -> list[Edge]:
    """Edges that close a cycle, by depth-first search from the start, then the rest."""
    back: list[Edge] = []
    state: dict[str, int] = {}

    def visit(node_id: str) -> None:
        state[node_id] = 1
        for e in edges_of(node_id, spec.nodes[node_id]):
            if e.target not in spec.nodes:
                continue
            if state.get(e.target) == 1:
                back.append(e)
            elif e.target not in state:
                visit(e.target)
        state[node_id] = 2

    for node_id in ([spec.start] if spec.start in spec.nodes else []) + list(spec.nodes):
        if node_id not in state:
            visit(node_id)
    return back


def guaranteed(spec: Spec) -> dict[str, set[str]]:
    """For each reachable node, the nodes sure to have finished before it runs.

    Its dominators, plus, at a `join: all` node, everything an unconditional `next` of one
    of those starts on the way in: a join waits for every incoming branch, and a parallel
    `next` always takes all its targets.
    """
    live = reachable(spec)
    preds: dict[str, list[str]] = {n: [] for n in live}
    for e in all_edges(spec):
        if e.source in live and e.target in live:
            preds[e.target].append(e.source)
    dominators = {n: {n} if n == spec.start else set(live) for n in live}
    changed = True
    while changed:
        changed = False
        for n in live - {spec.start}:
            sets = [dominators[p] for p in preds[n]]
            new = {n} | (set[str].intersection(*sets) if sets else set[str]())
            if new != dominators[n]:
                dominators[n], changed = new, True
    nexts: dict[str, list[str]] = {}
    for e in all_edges(spec):
        if e.kind == "next":
            nexts.setdefault(e.source, []).append(e.target)
    out: dict[str, set[str]] = {}
    for n in live:
        sure = dominators[n] - {n}
        node = spec.nodes[n]
        if node.join != "first":
            up = ancestors(spec, n)
            grown = True
            while grown:
                extra = {t for s in sure for t in nexts.get(s, []) if t in up and t != n} - sure
                sure |= extra
                grown = bool(extra)
        out[n] = sure
    return out


def _strings(field: str, value: JsonValue, scope: Scope) -> list[Template]:
    if isinstance(value, str):
        return [Template(field, value, scope)]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in _strings(f"{field}.{k}", v, scope)]
    if isinstance(value, list):
        return [t for i, v in enumerate(value) for t in _strings(f"{field}.{i}", v, scope)]
    return []


def _deliver(prefix: str, deliver: Deliver, scope: Scope) -> list[Template]:
    fields: dict[str, JsonValue] = {
        "to": deliver.to,
        "thread": deliver.thread,
        "message": deliver.message,
        "also": list(deliver.also or []),
        "mention": list(deliver.mention or []),
    }
    return [t for name, value in fields.items() for t in _strings(prefix + name, value, scope)]


def templates_of(node: AnyNode, scope: Scope = "node") -> list[Template]:
    """Every template string a node carries; a For each's inline node reads `item`."""
    found: list[Template] = []
    if isinstance(node, AgentNode | CodeNode | WorkflowNode):
        found += _strings("inputs", node.inputs or {}, scope)
    match node:
        case BranchNode():
            found += [Template(f"cases.{i}.when", c.when, scope) for i, c in enumerate(node.cases)]
        case TransformNode():
            found += _strings("set", node.set, scope)
        case ApprovalNode():
            found += _strings("show", node.show, scope) + _strings("attach", node.attach, scope)
            found += _strings("approvers", list(node.approvers or []), scope)
            found += [
                t
                for k, f in (node.form or {}).items()
                for t in _strings(f"form.{k}.default", f.default, scope)
            ]
        case WaitNode():
            found += _strings("until", node.until, scope)
            if node.event:
                found += _strings("event.match", node.event.match, "event")
                found += _strings("event.key", node.event.key, scope)
            if node.poll:
                found += _strings("poll.args", node.poll.args or {}, scope)
                found += _strings("poll.until", node.poll.until, "result")
        case ForEachNode():
            found += _strings("items", node.items, scope) + templates_of(node.do, "item")
        case FinishNode():
            found += _strings("output", node.output, scope)
            if node.deliver:
                found += _deliver("deliver.", node.deliver, scope)
            for key, value in (node.state or {}).items():
                found += _strings("state", key, scope) + _strings(f"state.{key}", value, scope)
        case PostNode():
            found += _deliver("", node, scope)
        case ActionNode():
            found += _strings("args", node.args or {}, scope)
        case _:
            pass
    return found


def spec_templates(spec: Spec) -> list[Template]:
    """Template strings outside the nodes: label, concurrency key, trigger mappings and card."""
    found = _strings("label", spec.label, "run")
    if spec.concurrency:
        found += _strings("concurrency.key", spec.concurrency.key, "run")
    for i, trigger in enumerate(spec.triggers or []):
        if trigger.kind == "webhook":
            for name in ("inputs", "start_when", "delivery_id"):
                found += _strings(f"triggers.{i}.{name}", getattr(trigger, name), "payload")
        elif trigger.kind == "event":
            found += _strings(f"triggers.{i}.match", trigger.match, "event")
            found += _strings(f"triggers.{i}.inputs", trigger.inputs, "event")
        elif trigger.kind == "mention":
            found += _strings(f"triggers.{i}.match", trigger.match, "mention")
            found += _strings(f"triggers.{i}.inputs", trigger.inputs, "mention")
        elif trigger.kind == "poll":
            found += _strings(f"triggers.{i}.args", trigger.args or {}, "run")
            found += _strings(f"triggers.{i}.start_when", trigger.start_when, "poll")
            found += _strings(f"triggers.{i}.inputs", trigger.inputs, "poll")
    if spec.card:
        found += _strings("card.fields", spec.card.fields, "card")
    return found

"""Validate a workflow spec: errors block publish, warnings show on review, plus the grants
the version needs and each step's Rule of Two classes.

Pure: the caller passes what the editor's agents can call (`Catalog`), or None to skip the
agent and tool checks. The canvas runs a subset of these rules (`spec.ts validate`); the
codes it shares are listed in the shared fixtures
(`web/src/modules/workflows/fixtures/validation.json`), which both test suites read.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from daimon.core.workflows.expressions import (
    ExpressionError,
    check_template,
    expressions_in,
    is_single_expression,
    template_refs,
)
from daimon.core.workflows.graph import (
    Template,
    ancestors,
    back_edges,
    edges_of,
    guaranteed,
    reachable,
    spec_templates,
    templates_of,
)
from daimon.core.workflows.spec import (
    MAX_AGENT_TIMEOUT,
    MAX_CODE_TIMEOUT,
    MAX_DEADLINE,
    ActionNode,
    AgentNode,
    AnyNode,
    ApprovalNode,
    BranchNode,
    CodeNode,
    EventTrigger,
    FinishNode,
    ForEachNode,
    SchemaField,
    Spec,
    TransformNode,
    WaitNode,
    WebhookTrigger,
    parse_duration,
    parse_spec,
)
from daimon.core.workflows.taint import Catalog, node_writes, offers, tainted_steps
from daimon.core.workflows.unattended import bound_by
from pydantic import BaseModel, JsonValue, ValidationError

DEFAULT_RUN_BUDGET_USD: Final = 5.0
#: A For each running agents this many at once earns a cost warning.
_COSTLY_CONCURRENCY: Final = 5
#: The only event source that fires: a finished run of that workflow.
_EVENT_SOURCE: Final = re.compile(r"workflow:[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_BAD_EVENT_SOURCE: Final = (
    "An event source is a workflow, as `workflow:<id>`; other events never arrive. "
    "Use a webhook trigger for outside events"
)
_BAD_WAIT_SOURCE: Final = (
    "A wait's event is another workflow's run (`workflow:<id>`), a delivery to one of this "
    "workflow's webhook triggers (`trigger:<id>`) or `callback`"
)
_STEP_PARTS: Final = frozenset({"output", "summary", "route", "status", "files", "visits"})
_ROOTS: Final[dict[str, frozenset[str]]] = {
    "node": frozenset({"inputs", "trigger", "steps", "run", "workflow"}),
    "item": frozenset({"inputs", "trigger", "steps", "run", "workflow", "item", "index"}),
    "event": frozenset({"inputs", "trigger", "steps", "run", "workflow", "event"}),
    "payload": frozenset({"payload", "trigger"}),
    "run": frozenset({"inputs", "trigger", "run", "workflow"}),
}

Severity = Literal["error", "warning"]
RuleClass = Literal["U", "S", "W"]
GrantKind = Literal[
    "unattended_write",
    "builtins",
    "code_network",
    "unverified_webhook",
    "budget_over_default",
    "untrusted_waiver",
]


class Problem(BaseModel):
    severity: Severity
    code: str
    message: str
    node: str | None = None


class Grant(BaseModel):
    kind: GrantKind
    node: str | None = None
    detail: str


class Validation(BaseModel):
    errors: list[Problem]
    warnings: list[Problem]
    grants: list[Grant]
    classes: dict[str, list[RuleClass]]


class _Report:
    def __init__(self) -> None:
        self.errors: list[Problem] = []
        self.warnings: list[Problem] = []

    def error(self, code: str, message: str, node: str | None = None) -> None:
        problem = Problem(severity="error", code=code, message=message, node=node)
        if problem not in self.errors:
            self.errors.append(problem)

    def warn(self, code: str, message: str, node: str | None = None) -> None:
        problem = Problem(severity="warning", code=code, message=message, node=node)
        if problem not in self.warnings:
            self.warnings.append(problem)


def validate_document(
    document: JsonValue,
    catalog: Catalog | None = None,
    *,
    run_budget_usd: float = DEFAULT_RUN_BUDGET_USD,
) -> Validation:
    """Validate raw JSON: schema problems first, then `validate_spec`."""
    try:
        spec = parse_spec(document)
    except ValidationError as err:
        report = _Report()
        for e in err.errors():
            loc = [str(x) for x in e["loc"]]
            node = loc[1] if len(loc) > 1 and loc[0] == "nodes" else None
            report.error("schema", f"{'.'.join(loc)}: {e['msg']}", node)
        return Validation(errors=report.errors, warnings=[], grants=[], classes={})
    return validate_spec(spec, catalog, run_budget_usd=run_budget_usd)


def validate_spec(
    spec: Spec, catalog: Catalog | None = None, *, run_budget_usd: float = DEFAULT_RUN_BUDGET_USD
) -> Validation:
    report = _Report()
    nodes = spec.nodes
    if spec.start not in nodes:
        report.error("start", f"Start step missing: {spec.start}")
    if not any(isinstance(n, FinishNode) for n in nodes.values()):
        report.error("no_finish", "No Finish step")
    live = reachable(spec)
    sure = guaranteed(spec)
    for node_id, node in nodes.items():
        out = edges_of(node_id, node)
        for e in out:
            if e.target not in nodes:
                report.error("unknown_target", f"Unknown step: {e.target}", node_id)
        if not isinstance(node, FinishNode) and not out:
            report.error("dead_end", "No next step", node_id)
        if node_id not in live:
            report.error("unreachable", "Unreachable", node_id)
        _check_refs(spec, node_id, node, sure.get(node_id), report)
        _check_node(spec, node_id, node, catalog, report)
    for template in spec_templates(spec):
        _check_template(spec, template, None, None, report)
    _check_loops(spec, report)
    _check_spec(spec, report)
    tainted = tainted_steps(spec, catalog)
    classes, grants = _classes_and_grants(spec, catalog, tainted, run_budget_usd)
    for node_id, node in nodes.items():
        c = classes[node_id]
        waived = isinstance(node, AgentNode) and node.untrusted_waiver
        if "U" in c and "W" in c and not bound_by(spec, node_id) and not waived:
            report.error("rule_of_two", "Untrusted input reaches a write", node_id)
    return Validation(
        errors=report.errors, warnings=report.warnings, grants=grants, classes=classes
    )


def _output_fields(node: AnyNode) -> dict[str, SchemaField] | None:
    """Fields later steps may read from `output`; None when any field may be there."""
    match node:
        case AgentNode() | CodeNode():
            return node.output
        case ApprovalNode():
            fixed = ("decision", "route", "comment", "text", "values", "files", "by", "via")
            return {**{k: SchemaField() for k in fixed}, **(node.form or {})}
        case TransformNode():
            return {k: SchemaField() for k in node.set}
        case _:
            return None


def _check_template(
    spec: Spec, template: Template, node_id: str | None, sure: set[str] | None, report: _Report
) -> None:
    try:
        check_template(template.text)
        refs = template_refs(template.text)
    except ExpressionError as err:
        report.error("expression", f"{template.field}: {err}", node_id)
        return
    guards = {r.path[1] for r in refs if r.guarded and len(r.path) > 1 and r.path[0] == "steps"}
    up = ancestors(spec, node_id) if node_id else set[str]()
    for ref in refs:
        root, rest = ref.path[0], ref.path[1:]
        if root not in _ROOTS[template.scope]:
            report.error("unknown_ref", f"Not available here: {'.'.join(ref.path)}", node_id)
        elif (
            root == "inputs"
            and rest
            and rest[0] not in (spec.inputs.properties if spec.inputs else {})
        ):
            report.error("unknown_ref", f"Unknown input: {'.'.join(ref.path)}", node_id)
        elif root == "steps" and rest:
            _check_step_ref(
                spec, ref.path, node_id, up, sure, ref.guarded or rest[0] in guards, report
            )


def _check_step_ref(
    spec: Spec,
    path: tuple[str, ...],
    node_id: str | None,
    up: set[str],
    sure: set[str] | None,
    guarded: bool,
    report: _Report,
) -> None:
    text = ".".join(path)
    step = path[1]
    if step not in spec.nodes:
        report.error("unknown_ref", f"Unknown step: {text}", node_id)
        return
    if step not in up:
        report.error("not_upstream", f"Not upstream: {text}", node_id)
        return
    if len(path) > 2 and path[2] not in _STEP_PARTS:
        report.error("unknown_ref", f"Unknown field: {text}", node_id)
    fields = _output_fields(spec.nodes[step])
    if len(path) > 3 and path[2] == "output" and fields and path[3] not in fields:
        report.error("unknown_ref", f"Unknown field: {text}", node_id)
    in_loop = node_id is not None and node_id in ancestors(spec, step)
    if sure is not None and not guarded and step not in sure and not in_loop:
        report.error("unguarded_ref", f"{text} may not have run; guard it with has()", node_id)


def _check_refs(
    spec: Spec, node_id: str, node: AnyNode, sure: set[str] | None, report: _Report
) -> None:
    for template in templates_of(node):
        _check_template(spec, template, node_id, sure, report)
    if isinstance(node, BranchNode):
        for case in node.cases:
            if not is_single_expression(case.when):
                report.error("expression", "A condition is one ${{ }} expression", node_id)
    if isinstance(node, AgentNode | ForEachNode):
        agent = node.do if isinstance(node, ForEachNode) else node
        if isinstance(agent, AgentNode) and expressions_in(agent.instructions):
            report.error(
                "instructions_expression", "Instructions take no expressions; use inputs", node_id
            )


def _check_node(
    spec: Spec, node_id: str, node: AnyNode, catalog: Catalog | None, report: _Report
) -> None:
    match node:
        case AgentNode():
            _check_agent(spec, node_id, node, catalog, report)
            if node.timeout and parse_duration(node.timeout) > MAX_AGENT_TIMEOUT:
                report.error("limit", "An agent step runs at most 45 minutes", node_id)
        case CodeNode() if node.timeout and parse_duration(node.timeout) > MAX_CODE_TIMEOUT:
            report.error("limit", "A code step runs at most 60 minutes", node_id)
        case ActionNode() if catalog is not None and not offers(catalog.tools, node.tool):
            report.error("unknown_tool", f"Unknown tool: {node.tool}", node_id)
        case ApprovalNode():
            _check_approval(spec, node_id, node, report)
        case ForEachNode():
            inner = node.do
            if (
                inner.next
                or inner.on_error
                or inner.on_max_visits
                or getattr(inner, "routes", None)
            ):
                report.error("do_edges", "The step run for each item has no next steps", node_id)
            if isinstance(inner, AgentNode):
                _check_agent(spec, node_id, inner, catalog, report)
            if (
                isinstance(inner, ActionNode)
                and catalog is not None
                and not offers(catalog.tools, inner.tool)
            ):
                report.error("unknown_tool", f"Unknown tool: {inner.tool}", node_id)
            if (node.concurrency or 1) > _COSTLY_CONCURRENCY and isinstance(inner, AgentNode):
                report.warn("cost", "Agent per item, high concurrency", node_id)
        case WaitNode() if node.event and not _wait_source(spec, node.event.source):
            report.error("bad_event_source", _BAD_WAIT_SOURCE, node_id)
        case FinishNode() if node.deliver and node.deliver.to == "home" and spec.home is None:
            report.error("no_home", "Delivery to home needs a home channel", node_id)
        case _:
            pass


def _check_agent(
    spec: Spec, node_id: str, node: AgentNode, catalog: Catalog | None, report: _Report
) -> None:
    agent = node.agent or (spec.defaults.agent if spec.defaults else None)
    if agent is None:
        report.error("no_agent", "Choose an agent", node_id)
    if catalog is None or agent is None:
        return
    if agent not in catalog.agents:
        report.error("unknown_agent", f"Unknown agent: {agent}", node_id)
        return
    for tool in node.tools if isinstance(node.tools, list) else []:
        if not offers(catalog.agents[agent], tool):
            report.error("unknown_tool", f"{agent} cannot call {tool}", node_id)


def _wait_source(spec: Spec, source: str) -> bool:
    webhooks = {t.id for t in spec.triggers or [] if isinstance(t, WebhookTrigger)}
    return (
        bool(_EVENT_SOURCE.fullmatch(source))
        or source == "callback"
        or source.removeprefix("trigger:") in webhooks
        and source.startswith("trigger:")
    )


def _check_approval(spec: Spec, node_id: str, node: ApprovalNode, report: _Report) -> None:
    approvers = node.approvers or ["owner"]
    routes = node.routes or {}
    if "triggered_by" in approvers and not node.allow_self_approval:
        report.error("self_approval", "Naming who started the run needs self-approval on", node_id)
    if "thread" in approvers and node.deliver != "thread":
        report.error("approver_thread", "Anyone in the thread needs delivery to a thread", node_id)
    if node.deliver == "thread" and not node.channel and spec.home is None:
        report.error("no_home", "A thread needs a channel or a home channel", node_id)
    if node.timeout and node.timeout.route != "fail" and node.timeout.route not in routes:
        report.error("unknown_route", f"Timeout route missing: {node.timeout.route}", node_id)
    if node.answer == "choice" and len(routes) < 2:
        report.error("approval_routes", "A choice needs two or more routes", node_id)
    if node.answer in (None, "approve") and routes and "approve" not in routes:
        report.error("approval_routes", "Approve or reject needs an approve route", node_id)
    for bound in [node.binds] if isinstance(node.binds, str) else node.binds or []:
        if bound not in spec.nodes:
            report.error("unknown_bind", f"Binds unknown step: {bound}", node_id)
        elif not _directly_after(spec, routes.get("approve"), bound):
            report.error(
                "bind_not_direct", f"{bound} must be an Action right after approve", node_id
            )
        else:
            _check_bound_args(spec, node_id, bound, report)


def _directly_after(spec: Spec, start: str | None, bound: str) -> bool:
    """`bound` is an Action reached from `start` through Transforms and Branches only."""
    if not isinstance(spec.nodes.get(bound), ActionNode) or start is None:
        return False
    seen: set[str] = set()
    stack = [start]
    while stack:
        node_id = stack.pop()
        if node_id == bound:
            return True
        node = spec.nodes.get(node_id)
        if node_id in seen or not isinstance(node, TransformNode | BranchNode):
            continue
        seen.add(node_id)
        stack += [e.target for e in edges_of(node_id, node)]
    return False


def _check_bound_args(spec: Spec, approval: str, bound: str, report: _Report) -> None:
    """A bound Action's arguments resolve when the approver sees them: only from steps
    before the approval, or the approval's own answers."""
    allowed = ancestors(spec, approval) | {approval}
    for template in templates_of(spec.nodes[bound]):
        try:
            refs = template_refs(template.text)
        except ExpressionError:
            continue  # reported on the Action itself
        for ref in refs:
            if ref.path[0] == "steps" and len(ref.path) > 1 and ref.path[1] not in allowed:
                report.error(
                    "bind_unresolvable",
                    f"{bound} reads {ref.path[1]}, which runs after approval",
                    approval,
                )


def _check_loops(spec: Spec, report: _Report) -> None:
    budget = (spec.limits and spec.limits.budget_usd) or (
        spec.defaults and spec.defaults.budget_usd
    )
    for e in back_edges(spec):
        loop = [
            e.target,
            *(x for x in ancestors(spec, e.source) if e.target in ancestors(spec, x)),
            e.source,
        ]
        nodes = [spec.nodes[x] for x in loop if x in spec.nodes]
        if not any(not isinstance(n, FinishNode) and n.max_visits for n in nodes):
            report.error("loop_limit", "Loop needs max visits", e.target)
        if not budget and any(isinstance(n, AgentNode) for n in nodes):
            report.warn("cost", "Agent loop without budget", e.target)


def _check_spec(spec: Spec, report: _Report) -> None:
    if spec.limits and spec.limits.deadline and parse_duration(spec.limits.deadline) > MAX_DEADLINE:
        report.error("limit", "A run's deadline is at most 90 days")
    if any(
        isinstance(t, EventTrigger) and not _EVENT_SOURCE.fullmatch(t.source)
        for t in spec.triggers or []
    ):
        report.error("bad_event_source", _BAD_EVENT_SOURCE)
    webhooks = [t for t in spec.triggers or [] if isinstance(t, WebhookTrigger)]
    if (
        spec.concurrency
        and spec.concurrency.on_conflict == "replace"
        and any(not t.verify for t in webhooks)
    ):
        report.error("replace_unverified", "Replace needs every webhook verified")
    if any(t.respond == "result" for t in webhooks) and any(
        isinstance(n, AgentNode) or (isinstance(n, ForEachNode) and isinstance(n.do, AgentNode))
        for n in spec.nodes.values()
    ):
        report.error(
            "respond_result", "Answering with the result needs a workflow without agent steps"
        )


def _classes_and_grants(
    spec: Spec, catalog: Catalog | None, tainted: set[str], run_budget_usd: float
) -> tuple[dict[str, list[RuleClass]], list[Grant]]:
    classes: dict[str, list[RuleClass]] = {}
    grants: list[Grant] = []
    for node_id, node in spec.nodes.items():
        writes = node_writes(node, catalog)
        c: list[RuleClass] = ["U"] if node_id in tainted else []
        if isinstance(node, AgentNode | ForEachNode) or (
            isinstance(node, ActionNode) and not writes
        ):
            c.append("S")
        if writes:
            c.append("W")
        classes[node_id] = c
        if writes and not bound_by(spec, node_id):
            detail = node.tool if isinstance(node, ActionNode) else "Agent writes"
            if not isinstance(node, CodeNode):
                grants.append(Grant(kind="unattended_write", node=node_id, detail=detail))
        inner = node.do if isinstance(node, ForEachNode) else node
        if isinstance(inner, AgentNode) and inner.builtins not in (None, "none"):
            detail = "All built-ins" if inner.builtins == "all" else "Read built-ins"
            grants.append(Grant(kind="builtins", node=node_id, detail=detail))
        if isinstance(inner, CodeNode) and inner.network == "on":
            grants.append(Grant(kind="code_network", node=node_id, detail="Network on"))
        if isinstance(node, AgentNode) and node.untrusted_waiver:
            grants.append(
                Grant(kind="untrusted_waiver", node=node_id, detail=node.untrusted_waiver)
            )
    for trigger in spec.triggers or []:
        if isinstance(trigger, WebhookTrigger) and not trigger.verify:
            grants.append(Grant(kind="unverified_webhook", detail=trigger.id))
    budget = max(
        (spec.defaults and spec.defaults.budget_usd) or 0,
        (spec.limits and spec.limits.budget_usd) or 0,
    )
    if budget > run_budget_usd:
        grants.append(Grant(kind="budget_over_default", detail=f"${budget:g} a run"))
    return classes, grants

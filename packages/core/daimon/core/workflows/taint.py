"""Which steps see untrusted data, for the Rule of Two.

Untrusted: inputs and trigger data when a webhook, event, mention or poll can start the run
(or when the caller says so, as for a manual run by someone other than the publisher),
Action results, Wait payloads, answers from people who are not the owner or an editor,
`workflow.state` and `workflow.last_run` (earlier runs wrote them), built-in web tools,
third-party read tools (`tool_facts`), and agent lanes an exposed step shared
earlier. Taint follows references until nothing changes.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Final

from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    OPEN_TOOL_SAFETY,
    ToolSafetyPolicy,
    classify_tool,
)
from daimon.core.workflows.expressions import ExpressionError, template_refs
from daimon.core.workflows.graph import ancestors, templates_of
from daimon.core.workflows.spec import (
    ActionNode,
    AgentNode,
    AnyNode,
    ApprovalNode,
    CodeNode,
    ForEachNode,
    Spec,
    WaitNode,
    WriteScope,
)
from pydantic import BaseModel, ConfigDict, Field

_TRUSTED_ANSWERERS: Final = frozenset({"owner", "editors"})
#: The tool name for every tool of a server whose toolset is on by default (`server/*`):
#: its tools are not listed, so it counts as unknown, a write and an untrusted read.
ANY_TOOL: Final = "*"
#: Triggers whose runs start from outside data.
UNTRUSTED_TRIGGERS: Final = frozenset({"webhook", "event", "mention", "poll"})
#: `workflow.*` fields earlier runs wrote.
_UNTRUSTED_WORKFLOW: Final = frozenset({"state", "last_run"})


class ToolFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    writes: bool = False
    #: Returns outside content (mail, tickets, chat, web) from a server not marked trusted.
    untrusted: bool = False


class SkillPin(BaseModel):
    """A library skill at the version a publish saw."""

    model_config = ConfigDict(frozen=True)

    id: str
    version: str


class Catalog(BaseModel):
    """What the editor's agents can call, keyed `server/tool`; built by the caller."""

    model_config = ConfigDict(frozen=True)

    agents: dict[str, frozenset[str]]
    tools: dict[str, ToolFacts]
    #: Classes the tools it does not list, as the run-time rule does.
    policy: ToolSafetyPolicy = OPEN_TOOL_SAFETY
    #: The workspace library's skill names; None skips the skill check.
    skills: frozenset[str] | None = None
    #: Those skills by name, at their versions (the library's now, or as published).
    skill_pins: dict[str, SkillPin] = Field(default_factory=dict[str, SkillPin])
    #: The skill ids each listed agent already holds, against the session's skill cap.
    agent_skills: dict[str, frozenset[str]] = Field(default_factory=dict[str, frozenset[str]])


def catalog_of(
    policy: ToolSafetyPolicy,
    agent_tools: Mapping[str, Sequence[str]],
    skills: Mapping[str, SkillPin] | None = None,
    agent_skills: Mapping[str, frozenset[str]] | None = None,
) -> Catalog:
    """Agents' tools, each classed by the operator's policy as the run-time calls are."""
    return Catalog(
        agents={k: frozenset(v) for k, v in agent_tools.items()},
        tools={t: tool_facts(policy, t) for ids in agent_tools.values() for t in ids},
        policy=policy,
        skills=frozenset(skills) if skills is not None else None,
        skill_pins=dict(skills or {}),
        agent_skills=dict(agent_skills or {}),
    )


def tool_facts(policy: ToolSafetyPolicy, tool: str) -> ToolFacts:
    """A `server/tool` as `decide_unattended_call` classes it: the operator's overrides,
    then the name, and any name not plainly a read writes."""
    server, _, name = tool.rpartition("/")
    writes = classify_tool(policy, server_name=server, tool_name=name) == "write"
    # A third-party read returns outside content (mail, tickets, chat, web); Daimon's own do not.
    reads = not writes or name == ANY_TOOL
    return ToolFacts(writes=writes, untrusted=reads and server != DAIMON_SERVER_NAME)


def offers(tools: Collection[str], tool: str) -> bool:
    """`tools` includes `tool`, by name or under its server's `server/*`."""
    server, _, name = tool.rpartition("/")
    return name != ANY_TOOL and (tool in tools or f"{server}/{ANY_TOOL}" in tools)


def tool_writes(tool: str, catalog: Catalog | None) -> bool:
    if catalog is not None and tool in catalog.tools:
        return catalog.tools[tool].writes
    return tool_facts(catalog.policy if catalog else OPEN_TOOL_SAFETY, tool).writes


def node_writes(node: AnyNode, catalog: Catalog | None) -> bool:
    """An outside effect at run time beyond its `writes_to` scopes: a write Action whose
    tool no scope names, or an agent allowed to write. A post is not one: its targets are
    the spec's own, as a Finish's are."""
    match node:
        case ActionNode():
            scoped = any(s.tool == node.tool for s in node.writes_to or [])
            return tool_writes(node.tool, catalog) and not scoped
        case AgentNode():
            return node.writes in ("allow", "routine") or node.review == "auto"
        case CodeNode():
            return node.network == "on"
        case ForEachNode():
            return node_writes(node.do, catalog)
        case _:
            return False


def node_scopes(node: AnyNode) -> list[WriteScope]:
    """The scoped writes a step (or a For each's item step) may make unwatched."""
    inner = node.do if isinstance(node, ForEachNode) else node
    return list(inner.writes_to or []) if isinstance(inner, AgentNode | ActionNode) else []


def human_answer(node: ApprovalNode) -> bool:
    """The approval carries data from someone other than the owner or an editor."""
    carries_data = bool(node.form) or node.answer in ("text", "form")
    return carries_data and not set(node.approvers or ["owner"]) <= _TRUSTED_ANSWERERS


def untrusted_output(node: AnyNode) -> bool:
    """Steps whose output is outside data whatever they read."""
    if isinstance(node, ApprovalNode):
        return human_answer(node)
    return isinstance(node, ActionNode | WaitNode)


def _agent_tools(node: AgentNode, spec: Spec, catalog: Catalog) -> frozenset[str]:
    if isinstance(node.tools, list):
        return frozenset(node.tools)
    agent = node.agent or (spec.defaults.agent if spec.defaults else None)
    return catalog.agents.get(agent or "", frozenset())


def _exposed(node: AnyNode, spec: Spec, catalog: Catalog | None) -> bool:
    """Exposed to outside content by its own tools, whatever it reads."""
    inner = node.do if isinstance(node, ForEachNode) else node
    if isinstance(inner, AgentNode):
        if inner.builtins not in (None, "none"):
            return True
        if catalog is not None:
            return any(
                (catalog.tools.get(t) or tool_facts(catalog.policy, t)).untrusted
                for t in _agent_tools(inner, spec, catalog)
            )
    return isinstance(node, WaitNode) and (node.event is not None or node.poll is not None)


def _refs(node: AnyNode) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for template in templates_of(node):
        try:
            paths += [r.path for r in template_refs(template.text)]
        except ExpressionError:
            continue  # reported by the validator; an unparsable template reads nothing
    return paths


def lane_of(node: AnyNode) -> str | None:
    """The agent session a step continues; None for a fresh one or a step with none."""
    if not isinstance(node, AgentNode) or node.session == "fresh":
        return None
    return node.session or "main"


def tainted_steps(
    spec: Spec, catalog: Catalog | None = None, *, untrusted_inputs: bool | None = None
) -> set[str]:
    """Node ids that read or are exposed to untrusted data (class U)."""
    if untrusted_inputs is None:
        untrusted_inputs = any(t.kind in UNTRUSTED_TRIGGERS for t in spec.triggers or [])
    refs = {node_id: _refs(node) for node_id, node in spec.nodes.items()}
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node_id, node in spec.nodes.items():
            if node_id in tainted:
                continue
            reads = refs[node_id]
            from_inputs = untrusted_inputs and any(p[0] in ("inputs", "trigger") for p in reads)
            from_runs = any(
                p[0] == "workflow" and (len(p) == 1 or p[1] in _UNTRUSTED_WORKFLOW) for p in reads
            )
            from_steps = any(
                p[0] == "steps"
                and len(p) > 1
                and p[1] in spec.nodes
                and (p[1] in tainted or untrusted_output(spec.nodes[p[1]]))
                for p in reads
            )
            lane = lane_of(node)
            from_lane = lane is not None and any(
                lane_of(spec.nodes[other]) == lane for other in ancestors(spec, node_id) & tainted
            )
            if from_inputs or from_runs or from_steps or from_lane or _exposed(node, spec, catalog):
                tainted.add(node_id)
                changed = True
    return tainted

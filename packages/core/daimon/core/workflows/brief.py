"""The step brief an agent step sends, its nudge, and the check of an output against its contract.

Pure: `agent_step` renders and sends, `step_tools` validates what the agent records.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Final
from xml.sax.saxutils import quoteattr

from daimon.core.untrusted import render_untrusted
from daimon.core.workflows.expressions import render
from daimon.core.workflows.graph import edges_of
from daimon.core.workflows.spec import AgentNode, SchemaField, Spec
from pydantic import JsonValue

NUDGE: Final = (
    "You stopped without finishing the workflow step. Call complete_step with your"
    " output, or fail_step with a reason."
)


def _text(value: str) -> str:
    """Element text: `<` and `&` escaped, arrows kept readable."""
    return value.replace("&", "&amp;").replace("<", "&lt;")


def step_inputs(node: AgentNode, context: Mapping[str, JsonValue], now: datetime) -> JsonValue:
    """The node's declared inputs, resolved in full (`ExpressionError` when one cannot be).

    A For each item's agent also gets its `item` and `index`.
    """
    rendered = render(dict(node.inputs or {}), context, now=now)
    own: dict[str, JsonValue] = {k: context[k] for k in ("item", "index") if k in context}
    return own | rendered if own and isinstance(rendered, dict) else rendered


def _field(f: SchemaField) -> str:
    kind = "|".join(f.enum) if f.enum else (f.type or "any")
    if f.type == "array" and f.items is not None:
        kind = f"list of {_field(f.items)}"
    return kind + (f" ({f.description})" if f.description else "")


def _map(spec: Spec, node_id: str, context: Mapping[str, JsonValue]) -> str:
    lines: list[str] = []
    for nid, node in spec.nodes.items():
        targets = sorted({e.target for e in edges_of(nid, node)})
        name = f"[{nid}]" if nid == node_id else nid
        lines.append(f"{name} ({node.kind})" + (f" -> {', '.join(targets)}" if targets else ""))
    steps = context.get("steps")
    done = (
        [k for k, v in steps.items() if isinstance(v, dict) and v.get("status") == "succeeded"]
        if isinstance(steps, dict)
        else []
    )
    lines.append(f"done: {', '.join(done) or '(none yet)'}; you are at: {node_id}")
    return "\n".join(lines)


def _contract(node: AgentNode) -> str:
    out = (
        "{" + ", ".join(f"{k}: {_field(f)}" for k, f in node.output.items()) + "}"
        if node.output
        else "none, a summary only"
    )
    lines = [f"output: {out}"]
    if node.routes:
        routes = [
            r if isinstance(t, str) or not t.when else f"{r} = {t.when}"
            for r, t in node.routes.items()
        ]
        lines.append(f"routes (pick one): {'; '.join(routes)}")
    return "\n".join(lines)


def _rules(node: AgentNode, attempt_id: uuid.UUID) -> str:
    finish = "output, summary and route" if node.routes else "output and summary"
    rules = [
        f"Nobody is watching. Finish by calling complete_step with {finish}, or fail_step"
        " with a reason.",
        f'Pass step="{attempt_id}" to every step tool.',
        "Do not post results unless the instructions say so.",
    ]
    if node.can_finish_early:
        rules.insert(1, "If there is nothing to do, call finish_early with a summary instead.")
    return "\n".join(rules)


def render_brief(
    spec: Spec,
    node_id: str,
    node: AgentNode,
    *,
    run_number: int,
    attempt: int,
    attempt_id: uuid.UUID,
    context: Mapping[str, JsonValue],
    inputs: JsonValue,
    untrusted_source: str | None,
) -> str:
    """The XML brief; inputs go inside the untrusted envelope when any came from outside."""
    data = json.dumps(inputs, ensure_ascii=False, default=str)
    attrs = " ".join(
        f"{k}={quoteattr(v)}"
        for k, v in (
            ("workflow", spec.name),
            ("run", str(run_number)),
            ("step", node_id),
            ("attempt", str(attempt)),
        )
    )
    parts = [f"<workflow_step {attrs}>"]
    if spec.goal:
        parts.append(f"<goal>{_text(spec.goal)}</goal>")
    parts += [
        f"<map>{_text(_map(spec, node_id, context))}</map>",
        f"<instructions>{_text(node.instructions)}</instructions>",
        render_untrusted(data, source=untrusted_source)
        if untrusted_source is not None
        else f"<inputs>{_text(data)}</inputs>",
        f"<contract>{_text(_contract(node))}</contract>",
        f"<rules>{_text(_rules(node, attempt_id))}</rules>",
        "</workflow_step>",
    ]
    return "\n".join(parts)


_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def _check(path: str, f: SchemaField, value: JsonValue) -> list[str]:
    if f.type is not None:
        wanted = _TYPES[f.type]
        if not isinstance(value, wanted) or (f.type != "boolean" and isinstance(value, bool)):
            return [f"{path} must be a {f.type}"]
    if f.enum and value not in f.enum:
        return [f"{path} must be one of {', '.join(f.enum)}"]
    if f.max_length and isinstance(value, str) and len(value) > f.max_length:
        return [f"{path} must be at most {f.max_length} characters"]
    if f.items is not None and isinstance(value, list):
        return [e for i, v in enumerate(value) for e in _check(f"{path}[{i}]", f.items, v)]
    return []


def check_output(
    fields: Mapping[str, SchemaField] | None, output: JsonValue
) -> tuple[JsonValue, list[str]]:
    """The output with defaults filled, and the exact errors against the node's contract."""
    if fields is None:
        return None, [] if output in (None, {}) else ["This step takes no output: use summary"]
    if not isinstance(output, dict):
        return output, ["output must be an object"]
    errors = [f"output.{k} is not in the contract" for k in output if k not in fields]
    filled = dict(output)
    for name, f in fields.items():
        if output.get(name) is None and f.default is not None:
            filled[name] = f.default
        if filled.get(name) is None:
            errors.append(f"output.{name} is missing")
        else:
            errors += _check(f"output.{name}", f, filled[name])
    return filled, errors

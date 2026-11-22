"""One rule for every third-party call a workflow makes with nobody watching: an agent
step's tool call, an Action, and the send of an approved proposal.

`decide_unattended_call` is the only place that says whether such a call goes out, is
recorded as a proposal for a person to review, or is refused. A contract test holds every
Action call and proposal send to it. `scope_allows` is the pure matcher for `writes_to`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from daimon.core.continuity.watch_projection import project
from daimon.core.tool_safety import ToolAnnotations, ToolSafetyPolicy, classify_tool
from daimon.core.workflows.spec import (
    AgentNode,
    ApprovalNode,
    ArgRule,
    ForEachNode,
    Spec,
    WriteScope,
)
from daimon.core.workflows.taint import Catalog, node_scopes, tainted_steps
from pydantic import JsonValue

Writes = Literal["refuse", "propose", "allow", "routine"]
Verdict = Literal["allow", "propose", "refuse"]
#: Ledger event: ids an allowed call returned that a `created_by` rule may name later.
CREATED: Final = "write.created"
#: One plain address: exactly one `@`, and no display name, quotes, comments, `%` or `!` routes.
_ADDRESS: Final = re.compile(r"[A-Za-z0-9.#$&*+/=?^_{|}~-]+@([A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)")


@dataclass(frozen=True)
class Call:
    """A third-party call as the step would make it.

    `writes` is what the step may do with writes (an Action under its grant is `allow`);
    `exposed` says untrusted data can steer it in this run (the Rule of Two at run time);
    `approved` says a person approved exactly this call (a bound approval whose hash still
    matches, or a reviewed proposal). `scopes` are the step's granted `writes_to` (empty
    while `workflow_scoped_writes` is off) and `created` the ids this run's earlier allowed
    calls returned, by `server/tool`.
    """

    server: str
    tool: str
    writes: Writes
    test: bool = False
    exposed: bool = False
    approved: bool = False
    #: The server's own listing of the tool; only `readOnlyHint` can make a read of it.
    annotations: ToolAnnotations | None = None
    args: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    scopes: Sequence[WriteScope] = ()
    created: Mapping[str, Collection[str]] = field(default_factory=dict[str, Collection[str]])


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str | None = None
    message: str = ""


def decide_unattended_call(policy: ToolSafetyPolicy, call: Call) -> Decision:
    """Reads go out; operator denies never do; writes go out only when a person approved
    them or a grant allows them on a step untrusted data cannot steer. A test run never
    writes. Writes that need a person become proposals."""
    key = f"{call.server}/{call.tool}"
    if key in policy.denied or call.server in policy.denied:
        return Decision("refuse", "denied", f"{key} is turned off here by the operator.")
    kind = classify_tool(
        policy, server_name=call.server, tool_name=call.tool, annotations=call.annotations
    )
    if kind == "read":
        return Decision("allow")
    if call.test:
        return Decision("refuse", "test_run", "A test run never writes.")
    if call.approved or scope_allows(call.scopes, key, call.args, call.created):
        return Decision("allow")
    granted = call.writes in ("allow", "routine")
    if call.writes == "propose" or (granted and call.exposed):
        reason = "untrusted" if call.exposed else None
        return Decision("propose", reason, "A person reviews this write before it is sent.")
    if granted:
        return Decision("allow")
    return Decision("refuse", "writes_refused", "This step may not write.")


def scope_allows(
    scopes: Sequence[WriteScope],
    tool: str,
    args: Mapping[str, JsonValue],
    created: Mapping[str, Collection[str]],
) -> bool:
    """Some scope for `tool` covers the call: each argument is named in `free` or passes its
    `where` rule, and every `where` argument is there. A list passes when each item does."""
    return any(s.tool == tool and s.where and _fits(s, args, created) for s in scopes)


def _fits(
    scope: WriteScope, args: Mapping[str, JsonValue], created: Mapping[str, Collection[str]]
) -> bool:
    free = frozenset(scope.free or ())
    return all(_present(args, path) for path in scope.where) and all(
        _covered(scope, free, name, value, created) for name, value in args.items()
    )


def _present(args: Mapping[str, JsonValue], path: str) -> bool:
    value: JsonValue = dict(args)
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def _covered(
    scope: WriteScope,
    free: frozenset[str],
    path: str,
    value: JsonValue,
    created: Mapping[str, Collection[str]],
) -> bool:
    """`path` is free, passes its rule, or is an object whose every key is covered."""
    if path in free:
        return True
    if path in scope.where:
        return _passes(scope.where[path], value, created)
    deeper = any(p.startswith(f"{path}.") for p in (*scope.where, *free))
    return (
        deeper
        and isinstance(value, dict)
        and all(_covered(scope, free, f"{path}.{k}", v, created) for k, v in value.items())
    )


def _passes(rule: ArgRule, value: JsonValue, created: Mapping[str, Collection[str]]) -> bool:
    if isinstance(value, list):
        return bool(value) and all(_passes(rule, v, created) for v in value)
    # `type is` keeps True from matching 1.
    if any(type(value) is type(x) and value == x for x in rule.in_ or ()):
        return True
    if not isinstance(value, str):
        return False
    if any(value.startswith(p) for p in rule.prefix or ()):
        return True
    address = _ADDRESS.fullmatch(value)
    domains = {d.lower() for d in rule.domains or ()}
    if address and address.group(1).lower() in domains:
        return True
    return rule.created_by is not None and value in created.get(rule.created_by.tool, ())


def created_paths(spec: Spec) -> dict[str, frozenset[str]]:
    """By `server/tool`, where in its result each `created_by` rule of the spec finds an id."""
    paths: dict[str, set[str]] = {}
    for node in spec.nodes.values():
        for scope in node_scopes(node):
            for rule in scope.where.values():
                if rule.created_by is not None:
                    paths.setdefault(rule.created_by.tool, set()).add(rule.created_by.id)
    return {tool: frozenset(p) for tool, p in paths.items()}


def created_by_tool(events: Iterable[Mapping[str, JsonValue]]) -> dict[str, set[str]]:
    """The run's `CREATED` events' data as the ids each `server/tool` returned."""
    created: dict[str, set[str]] = {}
    for data in events:
        tool, ids = data.get("tool"), data.get("ids")
        if isinstance(tool, str) and isinstance(ids, list):
            created.setdefault(tool, set()).update(i for i in ids if isinstance(i, str))
    return created


def created_ids(paths: Collection[str], result: JsonValue) -> list[str]:
    """The ids at `paths` in a call's result; a path that finds no text finds nothing."""
    ids: list[str] = []
    for path in sorted(paths):
        try:
            found = project(result, path)
        except ValueError:
            continue
        if isinstance(found, str) and found not in ids:
            ids.append(found)
    return ids


def exposed_at_run(
    spec: Spec, node_id: str, *, untrusted_inputs: bool, catalog: Catalog | None = None
) -> bool:
    """Untrusted data reaches this step in this run, and no admin waived it. A manual run by
    anyone but the publisher counts its inputs as untrusted."""
    node = spec.nodes[node_id]
    inner = node.do if isinstance(node, ForEachNode) else node
    if isinstance(inner, AgentNode) and inner.untrusted_waiver:
        return False
    return node_id in tainted_steps(spec, catalog, untrusted_inputs=untrusted_inputs)


def bound_by(spec: Spec, node_id: str) -> str | None:
    """The approval whose `binds` names this Action, if any."""
    for approval_id, node in spec.nodes.items():
        if isinstance(node, ApprovalNode):
            binds = [node.binds] if isinstance(node.binds, str) else node.binds or []
            if node_id in binds:
                return approval_id
    return None


def call_hash(tool: str, args: JsonValue) -> str:
    """The digest an approval binds: the exact tool and arguments a person approved."""
    blob = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()

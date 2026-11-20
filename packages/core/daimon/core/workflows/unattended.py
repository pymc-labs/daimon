"""One rule for every third-party call a workflow makes with nobody watching: an agent
step's tool call, an Action, and the send of an approved proposal.

`decide_unattended_call` is the only place that says whether such a call goes out, is
recorded as a proposal for a person to review, or is refused. A contract test holds every
Action call and proposal send to it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from daimon.core.tool_safety import ToolSafetyPolicy, classify_tool
from daimon.core.workflows.spec import AgentNode, ApprovalNode, ForEachNode, Spec
from daimon.core.workflows.taint import tainted_steps
from pydantic import JsonValue

Writes = Literal["refuse", "propose", "allow"]
Verdict = Literal["allow", "propose", "refuse"]


@dataclass(frozen=True)
class Call:
    """A third-party call as the step would make it.

    `writes` is what the step may do with writes (an Action under its grant is `allow`);
    `exposed` says untrusted data can steer it in this run (the Rule of Two at run time);
    `approved` says a person approved exactly this call (a bound approval whose hash still
    matches, or a reviewed proposal).
    """

    server: str
    tool: str
    writes: Writes
    test: bool = False
    exposed: bool = False
    approved: bool = False


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
    if classify_tool(policy, server_name=call.server, tool_name=call.tool) == "read":
        return Decision("allow")
    if call.test:
        return Decision("refuse", "test_run", "A test run never writes.")
    if call.approved:
        return Decision("allow")
    if call.writes == "propose" or (call.writes == "allow" and call.exposed):
        reason = "untrusted" if call.exposed else None
        return Decision("propose", reason, "A person reviews this write before it is sent.")
    if call.writes == "allow":
        return Decision("allow")
    return Decision("refuse", "writes_refused", "This step may not write.")


def exposed_at_run(spec: Spec, node_id: str, *, untrusted_inputs: bool) -> bool:
    """Untrusted data reaches this step in this run, and no admin waived it. A manual run by
    anyone but the publisher counts its inputs as untrusted."""
    node = spec.nodes[node_id]
    inner = node.do if isinstance(node, ForEachNode) else node
    if isinstance(inner, AgentNode) and inner.untrusted_waiver:
        return False
    return node_id in tainted_steps(spec, untrusted_inputs=untrusted_inputs)


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

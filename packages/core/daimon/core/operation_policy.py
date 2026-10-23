"""Pure operation policy for shared-agent changes.

Spec edits (including skill add/remove) refuse defaults-managed targets even
for admins, then allow admins. Attachment writes allow admins first, then
refuse managed targets. Other callers cannot make these shared-agent changes.

New MCP connections are attachment writes: a private token or OAuth grant
still adds servers and tools to the shared agent spec. Only key_add and
keys_import retain the posted-token exception, which does not change the
agent's spec. GitHub grants, and a GitHub Connect for one agent, require an
admin. A server-wide Connect has
no agent target, so its facts are all False and only an admin passes.

The I/O shells read TargetFacts and retain their own refusal wording.
"""

from __future__ import annotations

from typing import Literal

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Action, AgentReach, OperationFamily, Subject, Surface, authorize
from daimon.core.security_audit import record_policy_decision
from pydantic import BaseModel

OperationKind = Literal[
    "key_add",
    "key_replace",
    "key_remove",
    "keys_import",
    "mcp_connect",
    "mcp_replace",
    "mcp_remove",
    "repo_bind",
    "skill_repo_connect",
    "agent_spec_edit",
    "skill_add",
    "skill_remove",
    "github_connect",
    "github_grant",
]

PolicyOutcome = Literal["allow", "needs_admin", "managed_agent"]

_SPEC_OPERATIONS: frozenset[OperationKind] = frozenset(
    {"agent_spec_edit", "skill_add", "skill_remove"}
)

_ATTACHMENT_OPERATIONS: frozenset[OperationKind] = frozenset(
    {
        "key_replace",
        "key_remove",
        "mcp_connect",
        "mcp_replace",
        "mcp_remove",
        "repo_bind",
        "skill_repo_connect",
    }
)

_POSTED_TOKEN_OPERATIONS: frozenset[OperationKind] = frozenset({"key_add", "keys_import"})


class TargetFacts(BaseModel):
    """The facts about a target agent that the policy table reads.

    Frozen: a decision is a pure function of these facts plus the caller's
    admin status, never mutated after construction. The locality fact
    defaults to False.
    """

    model_config = {"frozen": True}

    is_daimon_managed: bool
    is_reachable_in_tenant: bool


def delegated_action_for_operation(operation: OperationKind) -> DelegatedAction | None:
    """Only spec/skill edits admit grants; credential/connection admin gates are native."""
    if operation == "agent_spec_edit":
        return DelegatedAction.AGENT_SETTINGS
    if operation in {"skill_add", "skill_remove"}:
        return DelegatedAction.SKILLS
    return None


def _family(operation: OperationKind) -> OperationFamily:
    if operation in _POSTED_TOKEN_OPERATIONS:
        return "posted_token"
    if operation in _SPEC_OPERATIONS:
        return "spec"
    # operation in _ATTACHMENT_OPERATIONS — the only remaining family.
    return "attachment"


#: The table reads no tenant policy: a shared-agent change is decided on the
#: target's reach and the caller's admin status alone.
_NO_POLICY = TenantAccessPolicy()


def _decide_operation(
    operation: OperationKind, *, is_admin: bool, target: TargetFacts
) -> PolicyOutcome:
    """Return the policy outcome for `operation` against `target`.

    GitHub operations have their own admin rules. Other operations belong to
    the three families described above; `authorize` decides each family's
    fixed order (`Action.CHANGE_SHARED_AGENT`).
    """
    if operation in ("github_connect", "github_grant", "repo_bind"):
        return "allow" if is_admin else "needs_admin"
    decision = authorize(
        _NO_POLICY,
        subject=Subject(is_admin=is_admin),
        action=Action.CHANGE_SHARED_AGENT,
        surface=Surface.CONFIG,
        operation_family=_family(operation),
        reach=AgentReach(
            managed=target.is_daimon_managed,
            reachable=target.is_reachable_in_tenant,
        ),
    )
    if decision:
        return "allow"
    return "managed_agent" if decision.reason == "managed_agent" else "needs_admin"


def decide_operation(
    operation: OperationKind, *, is_admin: bool, target: TargetFacts
) -> PolicyOutcome:
    """Evaluate the policy and annotate the active security-audit request, if any."""
    outcome = _decide_operation(operation, is_admin=is_admin, target=target)
    record_policy_decision(operation, outcome)
    return outcome


def needs_reachability_read(
    operation: OperationKind, *, is_admin: bool, is_daimon_managed: bool
) -> bool:
    """True only when `decide_operation` cannot answer without the reachability fact.

    Exists so the I/O-avoidance property — an admin or a posted-token write
    never pays for a reachability read, and a managed-agent target never
    needs one either, since both families settle the outcome before
    reachability is consulted — is provable in one unit test rather than
    re-derived at each of the three call sites that gate a live DB read on
    it.
    """
    return False


__all__ = [
    "OperationKind",
    "PolicyOutcome",
    "TargetFacts",
    "decide_operation",
    "delegated_action_for_operation",
    "needs_reachability_read",
]

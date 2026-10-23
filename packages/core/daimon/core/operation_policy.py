"""Pure operation policy for shared-agent changes.

Spec edits (including skill add/remove) refuse defaults-managed targets even
for admins, then allow admins and the agent's owner. Attachment writes allow
admins first, then refuse managed targets, then allow the owner, except repo
and skill-repo binds, which spend the workspace's GitHub installation. Other
callers cannot make these shared-agent changes.

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

#: Operations that bind the workspace's GitHub installation, not the owner's own
#: credential: an agent's owner may not make them, only an admin.
_OWNER_INELIGIBLE: frozenset[OperationKind] = frozenset(
    {"repo_bind", "skill_repo_connect", "github_connect", "github_grant"}
)


class TargetFacts(BaseModel):
    """The facts about a target agent that the policy table reads.

    Frozen: a decision is a pure function of these facts plus the caller's
    admin status, never mutated after construction. The locality fact
    defaults to False.
    """

    model_config = {"frozen": True}

    is_daimon_managed: bool
    is_reachable_in_tenant: bool
    # The caller is the agent's recorded owner. Never counts for an operation
    # that spends someone else's credential (`_OWNER_INELIGIBLE`).
    owned_by_caller: bool = False


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
    if operation in ("github_connect", "github_grant"):
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
            owned=target.owned_by_caller and operation not in _OWNER_INELIGIBLE,
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
    """True only when `decide_operation` cannot answer without reading the target's facts.

    The fact that can change an answer is whether the caller owns the agent
    (`TargetFacts.owned_by_caller`; reachability no longer does). An admin, a
    posted-token write, a managed target and an operation the owner may not
    make settle without it, so they never pay for the read. Derived from the
    table itself, so it can't drift from `decide_operation`.
    """
    unowned = TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=False)
    owned = unowned.model_copy(update={"owned_by_caller": True})
    return _decide_operation(operation, is_admin=is_admin, target=unowned) != _decide_operation(
        operation, is_admin=is_admin, target=owned
    )


__all__ = [
    "OperationKind",
    "PolicyOutcome",
    "TargetFacts",
    "decide_operation",
    "needs_reachability_read",
]

"""Pure policy table for the "blast radius" authorization rule.

Three call sites (`daimon.adapters.mcp.tools.reachability`,
`daimon.adapters.discord.agent_setup.authz` /
`daimon.adapters.discord.credential_repo_bind`, and
`daimon.adapters.slack.agent_policy`) each re-implement the same
underlying rule — blast radius of one agent -> open to any member; blast
radius of the whole tenant (a channel or workspace default) -> admin only —
with a load-bearing difference in what gets checked first. This module is
the single place that rule and its ordering are written down; the shell
gates read a `PolicyOutcome` from here and keep their own copy of the
strings and I/O, because the readers (a `ToolError`, a Discord ephemeral, a
Slack ephemeral) differ.

There are three rule families, each a fixed short-circuit order:

- **spec** (`agent_spec_edit`, `skill_add`, `skill_remove`): a spec edit
  never stamps the defaults reconciler's spec hash, so a defaults-managed
  agent refuses the edit even for an admin — an admin bypass here would leave
  permanent, silent drift against the repo defaults that reconcile never
  notices. Order: managed ->
  `managed_agent` (admin included); admin -> `allow`; reachable ->
  `needs_admin`; else `allow`. Adding or removing one of an agent's skills
  changes what it does, so it follows the same order: the managed agent is
  forked first, and anyone may change an agent that answers nowhere. For the
  two skill kinds "nowhere" is strict (`needs_strict_reach`): no bound
  thread and no routine or queued continuation of anyone else's either.

- **attachment** (`key_replace`, `key_remove`, `mcp_remove`, `repo_bind`):
  attachments never enter the agent spec, so the managed-agent absolutism
  above does not apply, and an admin attaching to a shared or managed agent
  is the first-run onboarding step this family exists to allow. Order: admin
  -> `allow` (checked BEFORE the managed check — this is the one step
  ordered differently from the spec family, and it is what keeps an admin
  able to bind a repo or replace a key on the seeded agent); managed ->
  `managed_agent`; reachable -> `needs_admin`; else `allow`.

- **posted-token exception** (`key_add`, `keys_import`, `mcp_connect`,
  `skill_repo_connect`): always `allow`. A single-use posted-token write is
  scoped to one value the requester alone holds, on one key, on one agent —
  a new contribution never overwrites or removes existing shared state, so
  it needs no admin and no reachability read. Only the destructive
  attachment writes (replace, remove) need an admin.

Where either of the first two families would answer `needs_admin`, a channel
admin whose channels hold every place the agent answers
(`is_local_to_caller_channels`, see `daimon.core.agent_reach`) is allowed
instead. The managed check still refuses them: only a server admin passes it.
"""

from __future__ import annotations

from typing import Literal

from daimon.core.security_audit import record_policy_decision
from pydantic import BaseModel

OperationKind = Literal[
    "key_add",
    "key_replace",
    "key_remove",
    "keys_import",
    "mcp_connect",
    "mcp_remove",
    "repo_bind",
    "skill_repo_connect",
    "agent_spec_edit",
    "skill_add",
    "skill_remove",
]

PolicyOutcome = Literal["allow", "needs_admin", "managed_agent"]

_SPEC_OPERATIONS: frozenset[OperationKind] = frozenset(
    {"agent_spec_edit", "skill_add", "skill_remove"}
)

_ATTACHMENT_OPERATIONS: frozenset[OperationKind] = frozenset(
    {"key_replace", "key_remove", "mcp_remove", "repo_bind"}
)

_POSTED_TOKEN_OPERATIONS: frozenset[OperationKind] = frozenset(
    {"key_add", "keys_import", "mcp_connect", "skill_repo_connect"}
)

_STRICT_REACH_OPERATIONS: frozenset[OperationKind] = frozenset({"skill_add", "skill_remove"})


class TargetFacts(BaseModel):
    """The facts about a target agent that the policy table reads.

    Frozen: a decision is a pure function of these facts plus the caller's
    admin status, never mutated after construction. The locality fact
    defaults to False, which is every caller with no channel admin grant.
    """

    model_config = {"frozen": True}

    is_daimon_managed: bool
    is_reachable_in_tenant: bool
    is_local_to_caller_channels: bool = False


def _decide_operation(
    operation: OperationKind, *, is_admin: bool, target: TargetFacts
) -> PolicyOutcome:
    """Return the policy outcome for `operation` against `target`.

    Every `OperationKind` belongs to exactly one of the three families
    described in the module docstring; this dispatches to that family's
    fixed order. See the module docstring for why the order differs between
    the spec and attachment families and why the posted-token family always
    allows.
    """
    if operation in _POSTED_TOKEN_OPERATIONS:
        return "allow"
    if operation in _SPEC_OPERATIONS:
        if target.is_daimon_managed:
            return "managed_agent"
        if is_admin:
            return "allow"
        return _reachable_outcome(target)
    # operation in _ATTACHMENT_OPERATIONS — the only remaining family.
    if is_admin:
        return "allow"
    if target.is_daimon_managed:
        return "managed_agent"
    return _reachable_outcome(target)


def _reachable_outcome(target: TargetFacts) -> PolicyOutcome:
    if target.is_reachable_in_tenant and not target.is_local_to_caller_channels:
        return "needs_admin"
    return "allow"


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
    if operation in _POSTED_TOKEN_OPERATIONS:
        return False
    # Both remaining families only consult reachability once neither the
    # managed check nor the admin check has already settled the outcome.
    return not is_admin and not is_daimon_managed


def needs_strict_reach(operation: OperationKind) -> bool:
    """True when "reachable" must count bound threads and others' unattended runs too.

    New skill files reach everyone who talks to the agent and every run that
    fires it, so a member may add or remove one only on an agent nobody else
    uses in any way. The other kinds keep the cascade-only reading.
    """
    return operation in _STRICT_REACH_OPERATIONS


__all__ = [
    "OperationKind",
    "PolicyOutcome",
    "TargetFacts",
    "decide_operation",
    "needs_reachability_read",
    "needs_strict_reach",
]

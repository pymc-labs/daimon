"""Pure policy table for the "blast radius" authorization rule.

Three call sites (`daimon.adapters.mcp.tools.reachability`,
`daimon.adapters.discord.agent_setup.authz` /
`daimon.adapters.discord.credential_repo_bind`, and
`daimon.adapters.slack.agent_policy`) each re-implement the same
underlying rule — blast radius of one agent -> open to any member; blast
radius of the whole tenant (a channel or workspace default) -> admin only —
with a load-bearing difference in what gets checked first. This module names
the operations and their families; `daimon.core.authz.authorize`
(`Action.CHANGE_SHARED_AGENT`) decides each family's order; the shell
gates read a `PolicyOutcome` from here and keep their own copy of the
strings and I/O, because the readers (a `ToolError`, a Discord ephemeral, a
Slack ephemeral) differ.

There are three rule families, each a fixed short-circuit order:

- **spec** (`agent_spec_edit`): a spec edit never stamps the defaults
  reconciler's spec hash, so a defaults-managed agent refuses the edit even
  for an admin — an admin bypass here would leave permanent, silent drift
  against the repo defaults that reconcile never notices. Order: managed ->
  `managed_agent` (admin included); admin -> `allow`; reachable ->
  `needs_admin`; else `allow`.

- **attachment** (`key_replace`, `key_remove`, `mcp_replace`, `mcp_remove`,
  `repo_bind`, `skill_repo_connect`): attachments never enter the agent
  spec, so the managed-agent absolutism above does not apply, and an admin
  attaching to a shared or managed agent is the first-run onboarding step
  this family exists to allow. Order: admin -> `allow` (checked BEFORE the managed check — this
  is the one step ordered differently from the spec family, and it is what
  keeps an admin able to bind a repo or replace a key on the seeded agent);
  managed -> `managed_agent`; reachable -> `needs_admin`; else `allow`.
  `skill_repo_connect` imports skills and attaches them to the agent, which
  changes what it runs for everyone it answers, so it gates like the other
  attachment writes. The attach itself still refuses a managed agent for
  everyone, admins included: skills are part of the agent spec.

- **posted-token exception** (`key_add`, `keys_import`, `mcp_connect`):
  always `allow`. A single-use posted-token write is scoped to one value the
  requester alone holds, on one key, on one agent — a new contribution never
  overwrites or removes existing shared state, so it needs no admin and no
  reachability read. Only the destructive attachment writes (replace,
  remove) and the skill-repo import need an admin. `mcp_connect` covers a
  new server name or the same name at the same URL; repointing an existing
  name at another URL, or overwriting the agent's shared token for a URL, is
  `mcp_replace`.

Where either of the first two families would answer `needs_admin`, a channel
admin whose channels hold every place the agent answers or runs
(`is_local_to_caller_channels`, see `daimon.core.agent_reach`) is allowed
instead. The managed check still refuses them: only a server admin passes it.
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
]

PolicyOutcome = Literal["allow", "needs_admin", "managed_agent"]

_SPEC_OPERATIONS: frozenset[OperationKind] = frozenset({"agent_spec_edit"})

_ATTACHMENT_OPERATIONS: frozenset[OperationKind] = frozenset(
    {"key_replace", "key_remove", "mcp_replace", "mcp_remove", "repo_bind", "skill_repo_connect"}
)

_POSTED_TOKEN_OPERATIONS: frozenset[OperationKind] = frozenset(
    {"key_add", "keys_import", "mcp_connect"}
)


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
    # Why a channel admin's agent is not local: an unattended run owed to someone
    # with wider rights. Explains a refusal; decisions never read it.
    runs_unattended_beyond_caller: bool = False
    # Or another member's conversation or routine in a channel never recorded,
    # which could be anywhere. Explains a refusal too.
    has_unplaced_run: bool = False


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

    Every `OperationKind` belongs to exactly one of the three families
    described in the module docstring; `authorize` decides that family's
    fixed order (`Action.CHANGE_SHARED_AGENT`). See the module docstring for
    why the order differs between the spec and attachment families and why
    the posted-token family always allows.
    """
    decision = authorize(
        _NO_POLICY,
        subject=Subject(is_admin=is_admin),
        action=Action.CHANGE_SHARED_AGENT,
        surface=Surface.CONFIG,
        operation_family=_family(operation),
        reach=AgentReach(
            managed=target.is_daimon_managed,
            reachable=target.is_reachable_in_tenant,
            local_to_caller=target.is_local_to_caller_channels,
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
    if operation in _POSTED_TOKEN_OPERATIONS:
        return False
    # Both remaining families only consult reachability once neither the
    # managed check nor the admin check has already settled the outcome.
    return not is_admin and not is_daimon_managed


__all__ = [
    "OperationKind",
    "PolicyOutcome",
    "TargetFacts",
    "decide_operation",
    "needs_reachability_read",
]

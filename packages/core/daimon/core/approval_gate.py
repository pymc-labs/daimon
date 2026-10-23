"""Admin-owned overrides for the attended third-party write confirmation gate.

No category inheritance: only the exact tenant, channel and thread count.
The stored setter travels with the effective choice for each auto-allow log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.access_policy import Approvals, ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.errors import DaimonError
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.security_audit import append_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class ApprovalGate:
    value: Approvals
    scope: str
    set_by: str | None
    tenant_id: uuid.UUID | None = None


def resolve_approval_gate(
    policy: TenantAccessPolicy,
    *,
    platform: str,
    channel_id: str | None,
    thread_id: str | None,
) -> ApprovalGate:
    """Most specific explicit value wins, including on below off and off below on."""
    result = ApprovalGate(policy.approvals, "server", policy.approvals_set_by)
    keys = [(channel_id, "channel")]
    if thread_id is not None and channel_id is not None:
        key = f"{channel_id}:{thread_id}" if platform == "slack" else thread_id
        keys.append((key, "thread"))
    for key, kind in keys:
        rule = policy.channel_rules.get(key) if key is not None else None
        if rule is not None and rule.approvals != "inherit":
            result = ApprovalGate(rule.approvals, f"{kind}:{key}", rule.approvals_set_by)
    return result


async def set_approval_gate(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    target: str | None,
    value: Approvals,
    subject: Subject,
) -> ApprovalGate:
    """Authorize before I/O, then merge and audit atomically under the policy lock.

    Adapters must supply a fresh native server-admin check, never a requester's
    ownership of a thread. None targets the server, otherwise an exact rule key.
    """
    if not authorize(TenantAccessPolicy(), subject=subject, action=Action.SET_APPROVAL_GATE):
        raise DaimonError("Only a server or workspace admin can change approval settings.")
    if value not in ("inherit", "off", "on") or target == "":
        raise DaimonError("Invalid approval setting.")
    setter = subject.platform_user_id
    if not setter:
        raise DaimonError("Approval changes require an identified administrator.")
    async with policy_write_transaction(factory, tenant_id=tenant_id) as session:
        await lock_access_policy(session, tenant_id=tenant_id)
        policy = await load_access_policy(session, tenant_id=tenant_id)
        if target is None:
            before = policy.approvals
            changed = policy.model_copy(
                update={
                    "approvals": value,
                    "approvals_set_by": setter if value != "inherit" else None,
                }
            )
        else:
            rule = policy.channel_rules.get(target, ChannelRule())
            before = rule.approvals
            rules = dict(policy.channel_rules)
            updated = rule.model_copy(update={"approvals": value, "approvals_set_by": setter})
            # Clearing removes provenance too, so an otherwise open rule disappears.
            if value == "inherit":
                updated = updated.model_copy(update={"approvals_set_by": None})
            if updated == ChannelRule():
                rules.pop(target, None)
            else:
                rules[target] = updated
            changed = policy.model_copy(update={"channel_rules": rules})
        await set_access_policy(session, tenant_id=tenant_id, policy=changed)
        await append_event(
            session,
            tenant_id=tenant_id,
            account_id=None,
            agent_id=None,
            platform=platform,
            platform_user_id=setter,
            tool_name="approval_gate",
            operation=Action.SET_APPROVAL_GATE,
            outcome="allowed",
            reason=f"{target or 'server'}:{before}->{value}",
        )
    return ApprovalGate(value, target or "server", setter, tenant_id)

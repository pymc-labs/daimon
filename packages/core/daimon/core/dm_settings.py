"""Admin changes to who a direct message reaches, for the web app and chat alike.

Each change is checked against the workspace's DM agents, written through the DM
stores and leaves one audit row in the same transaction. Audit details name an
agent or a group, never a person. A refusal raises `UserFacingError`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Final

from daimon.core.dm_access import DmAgent
from daimon.core.errors import UserFacingError
from daimon.core.features.registry import FEATURES
from daimon.core.stores import dm_access as store
from daimon.core.stores.direct_messages import get_dm_agent_name, set_dm_agent_name
from daimon.core.stores.domain import DmAgentGrantRow, DmGrantMode, DmRuleSubject
from daimon.core.stores.tenants import get_tenant
from daimon.core.web_admin import audit
from pydantic import StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

TARGET: Final = "Direct messages"
MAX_SUBJECTS: Final = 100
"""The most people, groups or rules one change may list."""
PlatformId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[\w:.@-]+$"),
]
"""A person's or group's id on the workspace's platform, as pasted or picked."""


@dataclass(frozen=True)
class DmActor:
    """Who changes the settings: their account (None for an operator) and when."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    now: datetime

    @property
    def set_by(self) -> str | None:
        return None if self.account_id is None else str(self.account_id)


@dataclass(frozen=True)
class DmRuleSpec:
    """One rule as an admin states it: whom it matches, their agent, and whether it is locked."""

    subject: DmRuleSubject
    subject_id: str | None = None
    agent: str | None = None
    locked: bool = False


async def dm_platform(db: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    """The workspace's chat platform when it has direct messages, else None."""
    tenant = await get_tenant(db, tenant_id)
    if tenant is None or not FEATURES["direct_messages"].on_platform(tenant.platform):
        return None
    return tenant.platform


def require_agent(agents: Sequence[DmAgent], name: str | None) -> None:
    """Refuse an agent name that is not one of the workspace's DM agents."""
    if name is not None and name not in {a.name for a in agents}:
        raise UserFacingError(f"There is no agent called {name} here.")


async def set_default(
    db: AsyncSession, actor: DmActor, agents: Sequence[DmAgent], name: str | None
) -> bool:
    """Set the agent a DM starts with when nothing else chose; False when unchanged."""
    require_agent(agents, name)
    if await get_dm_agent_name(db, tenant_id=actor.tenant_id) == name:
        return False
    await set_dm_agent_name(db, tenant_id=actor.tenant_id, agent_name=name)
    await _audit(db, actor, "Changed DM default agent", name or "None")
    return True


async def add_rule(
    db: AsyncSession,
    actor: DmActor,
    agents: Sequence[DmAgent],
    rule: DmRuleSpec,
    position: int | None,
) -> None:
    """Insert a rule at `position`, or last."""
    require_agent(agents, rule.agent)
    await store.add_rule(
        db,
        tenant_id=actor.tenant_id,
        subject=rule.subject,
        subject_id=rule.subject_id,
        agent_name=rule.agent,
        locked=rule.locked,
        set_by=actor.set_by,
        now=actor.now,
        position=position,
    )
    await _audit(db, actor, "Added DM rule", rule_text(rule))


async def change_rule(
    db: AsyncSession,
    actor: DmActor,
    agents: Sequence[DmAgent],
    position: int,
    *,
    agent: str | None,
    locked: bool,
    to: int | None,
) -> bool:
    """Set a rule's agent and lock, then move it to `to`; False when there is no such rule."""
    require_agent(agents, agent)
    rules = await store.list_rules(db, tenant_id=actor.tenant_id)
    if not 0 <= position < len(rules):
        return False
    before = rules[position]
    after = DmRuleSpec(before.subject, before.subject_id, agent, locked)
    if (agent, locked) != (before.agent_name, before.locked):
        await store.update_rule(
            db,
            tenant_id=actor.tenant_id,
            position=position,
            agent_name=agent,
            locked=locked,
            set_by=actor.set_by,
            now=actor.now,
        )
        await _audit(db, actor, "Changed DM rule", rule_text(after))
    if to is not None and min(to, len(rules) - 1) != position:
        await store.move_rule(
            db, tenant_id=actor.tenant_id, position=position, to=to, now=actor.now
        )
        moved = f"{subject_text(after)}: {position + 1} to {min(to, len(rules) - 1) + 1}"
        await _audit(db, actor, "Moved DM rule", moved)
    return True


async def remove_rule(db: AsyncSession, actor: DmActor, position: int) -> bool:
    """Delete a rule; False when there is no such rule."""
    rules = await store.list_rules(db, tenant_id=actor.tenant_id)
    if not 0 <= position < len(rules):
        return False
    gone = rules[position]
    await store.remove_rule(db, tenant_id=actor.tenant_id, position=position, now=actor.now)
    spec = DmRuleSpec(gone.subject, gone.subject_id, gone.agent_name, gone.locked)
    await _audit(db, actor, "Removed DM rule", rule_text(spec))
    return True


async def replace_rules(
    db: AsyncSession, actor: DmActor, agents: Sequence[DmAgent], rules: Sequence[DmRuleSpec]
) -> bool:
    """Replace the whole ordered list; False when it is unchanged."""
    for rule in rules:
        require_agent(agents, rule.agent)
    current = [
        DmRuleSpec(r.subject, r.subject_id, r.agent_name, r.locked)
        for r in await store.list_rules(db, tenant_id=actor.tenant_id)
    ]
    if current == list(rules):
        return False
    await store.replace_rules(
        db,
        tenant_id=actor.tenant_id,
        rules=[(r.subject, r.subject_id, r.agent, r.locked) for r in rules],
        set_by=actor.set_by,
        now=actor.now,
    )
    count = f"{len(rules)} rule{'' if len(rules) == 1 else 's'}"
    await _audit(db, actor, "Changed DM rules", count)
    return True


async def set_grant(
    db: AsyncSession,
    actor: DmActor,
    agent: DmAgent,
    label: str,
    *,
    who: DmGrantMode,
    user_ids: Sequence[str],
    group_ids: Sequence[str],
) -> bool:
    """Replace who may DM `agent` (audited under `label`); False when unchanged."""
    users = [] if who == "members" else list(dict.fromkeys(user_ids))
    groups = [] if who == "members" else list(dict.fromkeys(group_ids))
    before = (await store.list_grants(db, tenant_id=actor.tenant_id)).get(agent.name)
    if _grant_key(before) == (who, sorted(users), sorted(groups)):
        return False
    await store.set_grant(
        db,
        tenant_id=actor.tenant_id,
        agent_name=agent.name,
        who=who,
        user_ids=users,
        group_ids=groups,
        set_by=actor.set_by,
        now=actor.now,
    )
    await _audit(db, actor, "Changed DM access", grant_text(who, len(users), len(groups)), label)
    return True


def subject_text(rule: DmRuleSpec) -> str:
    """How an audit row names a rule's subject: never a person's id."""
    return {"user": "A person", "group": f"Group {rule.subject_id}", "everyone": "Everyone"}[
        rule.subject
    ]


def rule_text(rule: DmRuleSpec) -> str:
    locked = ", locked" if rule.locked else ""
    return f"{subject_text(rule)} to {rule.agent or 'the default agent'}{locked}"


def grant_text(who: DmGrantMode, people: int, groups: int) -> str:
    """Who may DM an agent, as counts: never a person's id."""
    if who == "members":
        return "Everyone"
    listed = [
        f"{n} {one if n == 1 else many}"
        for n, one, many in ((people, "person", "people"), (groups, "group", "groups"))
        if n
    ]
    label = "Listed" if who == "listed" else "Everyone except listed"
    return f"{label}: {', '.join(listed) or 'no one'}"


def _grant_key(grant: DmAgentGrantRow | None) -> tuple[str, list[str], list[str]]:
    if grant is None:
        return ("members", [], [])
    return (grant.who, sorted(grant.user_ids), sorted(grant.group_ids))


async def _audit(
    db: AsyncSession, actor: DmActor, action: str, detail: str, target: str = TARGET
) -> None:
    await audit(
        db,
        tenant_id=actor.tenant_id,
        actor_account_id=actor.account_id,
        action=action,
        target=target,
        detail=detail,
        at=actor.now,
    )

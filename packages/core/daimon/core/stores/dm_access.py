"""Store for direct message access: per-agent grants, ordered rules and picks."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from daimon.core._models import DmAgentGrant, DmAgentPick, DmRule
from daimon.core.errors import UserFacingError
from daimon.core.stores.domain import (
    DmAgentGrantRow,
    DmAgentPickRow,
    DmGrantMode,
    DmRuleRow,
    DmRuleSubject,
)
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def list_grants(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, DmAgentGrantRow]:
    """Grants by agent name. An agent with none admits every member."""
    rows = await session.scalars(select(DmAgentGrant).where(DmAgentGrant.tenant_id == tenant_id))
    return {r.agent_name: DmAgentGrantRow.model_validate(r) for r in rows}


async def set_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    who: DmGrantMode,
    user_ids: Sequence[str],
    group_ids: Sequence[str],
    set_by: str | None,
    now: datetime,
) -> DmAgentGrantRow:
    """Replace one agent's grant. `members` drops the row: that is the default."""
    if who == "members":
        await session.execute(
            delete(DmAgentGrant).where(
                DmAgentGrant.tenant_id == tenant_id, DmAgentGrant.agent_name == agent_name
            )
        )
        return DmAgentGrantRow(tenant_id=tenant_id, agent_name=agent_name, set_by=set_by)
    values = {
        "who": who,
        "user_ids": list(dict.fromkeys(user_ids)),
        "group_ids": list(dict.fromkeys(group_ids)),
        "set_by": set_by,
        "updated_at": now,
    }
    orm = await session.scalar(
        insert(DmAgentGrant)
        .values(tenant_id=tenant_id, agent_name=agent_name, **values)
        .on_conflict_do_update(
            index_elements=[DmAgentGrant.tenant_id, DmAgentGrant.agent_name], set_=values
        )
        .returning(DmAgentGrant)
    )
    assert orm is not None, "an upsert returns its row"
    return DmAgentGrantRow.model_validate(orm)


async def list_rules(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[DmRuleRow]:
    """Rules in match order."""
    rows = await session.scalars(
        select(DmRule).where(DmRule.tenant_id == tenant_id).order_by(DmRule.position)
    )
    return [DmRuleRow.model_validate(r) for r in rows]


async def _rewrite_rules(
    session: AsyncSession, tenant_id: uuid.UUID, rules: Sequence[DmRuleRow], now: datetime
) -> list[DmRuleRow]:
    """Store `rules` as positions 0..n-1, replacing the tenant's list."""
    await session.execute(delete(DmRule).where(DmRule.tenant_id == tenant_id))
    out = [r.model_copy(update={"position": i}) for i, r in enumerate(rules)]
    for r in out:
        session.add(
            DmRule(
                tenant_id=tenant_id,
                position=r.position,
                subject=r.subject,
                subject_id=r.subject_id,
                agent_name=r.agent_name,
                locked=r.locked,
                set_by=r.set_by,
                updated_at=now,
            )
        )
    await session.flush()
    return out


def _check_subject(subject: DmRuleSubject, subject_id: str | None) -> None:
    if (subject == "everyone") != (subject_id is None):
        raise UserFacingError("A user or group rule needs an id; an everyone rule takes none.")


def _check_lock(agent_name: str | None, locked: bool) -> None:
    if locked and agent_name is None:
        raise UserFacingError("A locked rule needs an agent.")


async def add_rule(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: DmRuleSubject,
    subject_id: str | None,
    agent_name: str | None,
    locked: bool,
    set_by: str | None,
    now: datetime,
    position: int | None = None,
) -> DmRuleRow:
    """Insert a rule at `position`, or at the end."""
    _check_subject(subject, subject_id)
    _check_lock(agent_name, locked)
    rules = await list_rules(session, tenant_id=tenant_id)
    at = len(rules) if position is None else max(0, min(position, len(rules)))
    rule = DmRuleRow(
        tenant_id=tenant_id,
        position=at,
        subject=subject,
        subject_id=subject_id,
        agent_name=agent_name,
        locked=locked,
        set_by=set_by,
    )
    rules.insert(at, rule)
    return (await _rewrite_rules(session, tenant_id, rules, now))[at]


async def replace_rules(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    rules: Sequence[tuple[DmRuleSubject, str | None, str | None, bool]],
    set_by: str | None,
    now: datetime,
) -> list[DmRuleRow]:
    """Replace every rule with `rules` (subject, subject id, agent, locked), in order."""
    rows: list[DmRuleRow] = []
    for at, (subject, subject_id, agent_name, locked) in enumerate(rules):
        _check_subject(subject, subject_id)
        _check_lock(agent_name, locked)
        rows.append(
            DmRuleRow(
                tenant_id=tenant_id,
                position=at,
                subject=subject,
                subject_id=subject_id,
                agent_name=agent_name,
                locked=locked,
                set_by=set_by,
            )
        )
    return await _rewrite_rules(session, tenant_id, rows, now)


async def remove_rule(
    session: AsyncSession, *, tenant_id: uuid.UUID, position: int, now: datetime
) -> bool:
    """Delete the rule at `position`; later rules close the gap. False when none."""
    rules = await list_rules(session, tenant_id=tenant_id)
    if not 0 <= position < len(rules):
        return False
    del rules[position]
    await _rewrite_rules(session, tenant_id, rules, now)
    return True


async def move_rule(
    session: AsyncSession, *, tenant_id: uuid.UUID, position: int, to: int, now: datetime
) -> bool:
    """Move the rule at `position` to `to`. False when `position` is out of range."""
    rules = await list_rules(session, tenant_id=tenant_id)
    if not 0 <= position < len(rules):
        return False
    rule = rules.pop(position)
    rules.insert(max(0, min(to, len(rules))), rule)
    await _rewrite_rules(session, tenant_id, rules, now)
    return True


async def update_rule(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    position: int,
    agent_name: str | None,
    locked: bool,
    set_by: str | None,
    now: datetime,
) -> DmRuleRow | None:
    """Change the agent and lock of the rule at `position`; None when there is none."""
    _check_lock(agent_name, locked)
    rules = await list_rules(session, tenant_id=tenant_id)
    if not 0 <= position < len(rules):
        return None
    rules[position] = rules[position].model_copy(
        update={"agent_name": agent_name, "locked": locked, "set_by": set_by}
    )
    return (await _rewrite_rules(session, tenant_id, rules, now))[position]


async def get_pick(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> DmAgentPickRow | None:
    orm = await session.get(DmAgentPick, (tenant_id, account_id))
    return None if orm is None else DmAgentPickRow.model_validate(orm)


async def set_pick(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_name: str,
    now: datetime,
) -> None:
    values = {"agent_name": agent_name, "updated_at": now}
    await session.execute(
        insert(DmAgentPick)
        .values(tenant_id=tenant_id, account_id=account_id, **values)
        .on_conflict_do_update(
            index_elements=[DmAgentPick.tenant_id, DmAgentPick.account_id], set_=values
        )
    )


async def clear_pick(session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID) -> None:
    await session.execute(
        delete(DmAgentPick).where(
            DmAgentPick.tenant_id == tenant_id, DmAgentPick.account_id == account_id
        )
    )


async def count_picks_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(DmAgentPick)
            .where(DmAgentPick.account_id == account_id)
        )
    ) or 0


async def delete_picks_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    result = await session.execute(delete(DmAgentPick).where(DmAgentPick.account_id == account_id))
    return int(getattr(result, "rowcount", 0) or 0)


async def count_user_rules(session: AsyncSession, *, keys: Sequence[tuple[uuid.UUID, str]]) -> int:
    """User rules naming one of these (tenant, platform user id) pairs."""
    total = 0
    for tenant_id, user_id in keys:
        total += (
            await session.scalar(
                select(func.count())
                .select_from(DmRule)
                .where(
                    DmRule.tenant_id == tenant_id,
                    DmRule.subject == "user",
                    DmRule.subject_id == user_id,
                )
            )
        ) or 0
    return total


async def delete_user_rules(
    session: AsyncSession, *, keys: Sequence[tuple[uuid.UUID, str]], now: datetime
) -> int:
    """Remove user rules naming these people and close the gaps they leave."""
    deleted = 0
    for tenant_id in {t for t, _ in keys}:
        ids = {u for t, u in keys if t == tenant_id}
        rules = await list_rules(session, tenant_id=tenant_id)
        kept = [r for r in rules if not (r.subject == "user" and r.subject_id in ids)]
        if len(kept) != len(rules):
            deleted += len(rules) - len(kept)
            await _rewrite_rules(session, tenant_id, kept, now)
    return deleted


async def count_grants_listing(
    session: AsyncSession, *, keys: Sequence[tuple[uuid.UUID, str]]
) -> int:
    """Grants whose user list holds one of these (tenant, platform user id) pairs."""
    total = 0
    for tenant_id in {t for t, _ in keys}:
        ids = {u for t, u in keys if t == tenant_id}
        grants = await list_grants(session, tenant_id=tenant_id)
        total += sum(1 for g in grants.values() if ids.intersection(g.user_ids))
    return total


async def scrub_user_from_grants(
    session: AsyncSession, *, keys: Sequence[tuple[uuid.UUID, str]], now: datetime
) -> int:
    """Remove these people's ids from every grant's user list in their workspaces."""
    changed = 0
    for tenant_id in {t for t, _ in keys}:
        ids = {u for t, u in keys if t == tenant_id}
        for name, grant in (await list_grants(session, tenant_id=tenant_id)).items():
            kept = [u for u in grant.user_ids if u not in ids]
            if len(kept) != len(grant.user_ids):
                changed += 1
                await set_grant(
                    session,
                    tenant_id=tenant_id,
                    agent_name=name,
                    who=grant.who,
                    user_ids=kept,
                    group_ids=grant.group_ids,
                    set_by=grant.set_by,
                    now=now,
                )
    return changed

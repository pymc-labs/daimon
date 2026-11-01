"""Store for web_invites: invite links, one-time sign-in links and owner links.

Only a token's SHA-256 is stored. Accepting is a single conditional UPDATE, so two
browsers racing on one link cannot both get in.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal

from daimon.core._models import WebInvite, WebInviteAgent
from daimon.core.cursor import Cursor
from daimon.core.stores.domain import WebInviteKind, WebInviteRole, WebInviteRow
from sqlalchemy import literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_invite(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    token_sha256: bytes,
    kind: WebInviteKind,
    expires_at: datetime,
    email: str = "",
    role: WebInviteRole = "visitor",
    credit_usd: Decimal = Decimal(0),
    agent_ids: Sequence[str] = (),
    person_id: uuid.UUID | None = None,
    created_by_account_id: uuid.UUID | None = None,
) -> WebInviteRow:
    orm = WebInvite(
        tenant_id=tenant_id,
        token_sha256=token_sha256,
        kind=kind,
        email=email,
        role=role,
        credit_usd=credit_usd,
        person_id=person_id,
        created_by_account_id=created_by_account_id,
        expires_at=expires_at,
    )
    session.add(orm)
    await session.flush()
    agents = tuple(dict.fromkeys(agent_ids))
    session.add_all(
        WebInviteAgent(invite_id=orm.id, tenant_id=tenant_id, agent_id=a) for a in agents
    )
    await session.flush()
    await session.refresh(orm)
    return _row(orm, agents)


def _row(orm: WebInvite, agent_ids: Iterable[str]) -> WebInviteRow:
    return WebInviteRow.model_validate(orm).model_copy(update={"agent_ids": tuple(agent_ids)})


async def _agents(session: AsyncSession, invite_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    rows = await session.execute(
        select(WebInviteAgent.invite_id, WebInviteAgent.agent_id)
        .where(WebInviteAgent.invite_id.in_(invite_ids))
        .order_by(WebInviteAgent.agent_id)
    )
    out: dict[uuid.UUID, list[str]] = {i: [] for i in invite_ids}
    for invite_id, agent_id in rows:
        out[invite_id].append(agent_id)
    return out


async def _with_agents(session: AsyncSession, orms: Sequence[WebInvite]) -> list[WebInviteRow]:
    agents = await _agents(session, [o.id for o in orms])
    return [_row(o, agents[o.id]) for o in orms]


async def get_invite(
    session: AsyncSession, *, tenant_id: uuid.UUID, invite_id: uuid.UUID
) -> WebInviteRow | None:
    orm = await session.scalar(
        select(WebInvite).where(WebInvite.id == invite_id, WebInvite.tenant_id == tenant_id)
    )
    return None if orm is None else (await _with_agents(session, [orm]))[0]


async def emails_by_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, invite_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The address of each of these invites that still exists and has one."""
    if not invite_ids:
        return {}
    rows = await session.execute(
        select(WebInvite.id, WebInvite.email).where(
            WebInvite.tenant_id == tenant_id, WebInvite.id.in_(list(invite_ids))
        )
    )
    return {invite_id: email for invite_id, email in rows if email}


async def find_by_token(session: AsyncSession, *, token_sha256: bytes) -> WebInviteRow | None:
    """The link a token opens, in whichever workspace, used or not."""
    orm = await session.scalar(select(WebInvite).where(WebInvite.token_sha256 == token_sha256))
    return None if orm is None else (await _with_agents(session, [orm]))[0]


async def list_invites(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: int | None = None,
    before: Cursor | None = None,
) -> list[WebInviteRow]:
    """A workspace's invites, not sign-in or owner links, newest first; revoked ones left out.

    `limit` and `before` (the `(created_at, id)` of the last invite already seen) page it.
    """
    query = select(WebInvite).where(
        WebInvite.tenant_id == tenant_id,
        WebInvite.kind == "invite",
        WebInvite.revoked_at.is_(None),
    )
    if before is not None:
        query = query.where(
            tuple_(WebInvite.created_at, WebInvite.id)
            < tuple_(literal(before[0]), literal(before[1]))
        )
    query = query.order_by(WebInvite.created_at.desc(), WebInvite.id.desc())
    if limit is not None:
        query = query.limit(limit)
    return await _with_agents(session, (await session.scalars(query)).all())


async def claim_invite(
    session: AsyncSession, *, token_sha256: bytes, now: datetime
) -> WebInviteRow | None:
    """Use a link once: None when it is unknown, used, revoked or expired."""
    orm = await session.scalar(
        update(WebInvite)
        .where(
            WebInvite.token_sha256 == token_sha256,
            WebInvite.accepted_at.is_(None),
            WebInvite.revoked_at.is_(None),
            WebInvite.expires_at > now,
        )
        .values(accepted_at=now)
        .returning(WebInvite)
    )
    return None if orm is None else (await _with_agents(session, [orm]))[0]


async def set_accepted_by(
    session: AsyncSession, *, invite_id: uuid.UUID, person_id: uuid.UUID
) -> None:
    await session.execute(
        update(WebInvite).where(WebInvite.id == invite_id).values(accepted_by_person_id=person_id)
    )


async def revoke_invite(
    session: AsyncSession, *, tenant_id: uuid.UUID, invite_id: uuid.UUID, now: datetime
) -> WebInviteRow | None:
    """Revoke an invite; its visitor, if it was accepted, loses its agents. None if absent."""
    orm = await session.scalar(
        update(WebInvite)
        .where(
            WebInvite.id == invite_id,
            WebInvite.tenant_id == tenant_id,
            WebInvite.kind == "invite",
            WebInvite.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(WebInvite)
    )
    return None if orm is None else (await _with_agents(session, [orm]))[0]


async def revoke_open_links(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kind: WebInviteKind,
    now: datetime,
    person_id: uuid.UUID | None = None,
) -> int:
    """Revoke every link of `kind` nobody has used yet, for one person when given.

    Returns how many were revoked. Used links are left alone: they were spent, not open.
    """
    where = [
        WebInvite.tenant_id == tenant_id,
        WebInvite.kind == kind,
        WebInvite.accepted_at.is_(None),
        WebInvite.revoked_at.is_(None),
    ]
    if person_id is not None:
        where.append(WebInvite.person_id == person_id)
    revoked = await session.scalars(
        update(WebInvite).where(*where).values(revoked_at=now).returning(WebInvite.id)
    )
    return len(revoked.all())


async def invited_agent_ids(
    session: AsyncSession, *, tenant_id: uuid.UUID, invite_id: uuid.UUID
) -> frozenset[str]:
    """The agents an invite opens, none once it is revoked."""
    rows = await session.scalars(
        select(WebInviteAgent.agent_id)
        .join(WebInvite, WebInvite.id == WebInviteAgent.invite_id)
        .where(
            WebInvite.id == invite_id,
            WebInvite.tenant_id == tenant_id,
            WebInvite.revoked_at.is_(None),
        )
    )
    return frozenset(rows)

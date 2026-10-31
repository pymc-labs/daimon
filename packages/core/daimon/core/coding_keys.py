"""One agent's coding keys: who may see and revoke them, and what a listing shows.

A coding key is an agent key made from the agent's "coding tools" button. A server
admin sees and revokes every live coding key of the agent; the agent's owner sees
and revokes only the keys they made themselves; anyone else sees nothing. The
caller reads the two facts live (the platform's admin status, `owns_agent`) and
passes them in: this module never trusts a panel snapshot.

A listing carries who made the key, when, when it ends and whether it is held to a
channel. It never carries the token, which exists only once, on screen, at mint.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Literal

from daimon.core.stores import mcp_tokens, platform_names
from daimon.core.stores.domain import McpTokenRow
from daimon.core.stores.identity import platform_external_ids
from sqlalchemy.ext.asyncio import AsyncSession

type CodingKeyAccess = Literal["admin", "owner"]


def coding_key_access(*, is_admin: bool, owns_agent: bool) -> CodingKeyAccess | None:
    """What the caller may do with the agent's coding keys: all of them, their own, or nothing."""
    if is_admin:
        return "admin"
    return "owner" if owns_agent else None


@dataclasses.dataclass(frozen=True)
class CodingKeyEntry:
    """One live coding key as a list shows it: no token, only what to tell keys apart."""

    jti: uuid.UUID
    creator: str
    created_at: dt.datetime
    expires_at: dt.datetime | None
    bound: bool
    mine: bool


async def list_coding_keys(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    access: CodingKeyAccess,
    viewer_account_id: uuid.UUID,
    platform: str,
    now: dt.datetime,
) -> list[CodingKeyEntry]:
    """The agent's live coding keys the viewer may see, newest first."""
    rows = await mcp_tokens.list_coding_keys(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        now=now,
        account_id=viewer_account_id if access == "owner" else None,
    )
    creators = await _creator_labels(session, tenant_id=tenant_id, rows=rows, platform=platform)
    return [
        CodingKeyEntry(
            jti=row.jti,
            creator=creators.get(row.account_id, "unknown"),
            created_at=row.created_at,
            expires_at=row.expires_at,
            bound=row.channel_id is not None,
            mine=row.account_id == viewer_account_id,
        )
        for row in rows
    ]


async def revoke_coding_key(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    access: CodingKeyAccess,
    viewer_account_id: uuid.UUID,
    now: dt.datetime,
) -> bool:
    """Revoke one coding key of this agent; False when the viewer may not, or it is gone.

    An owner reaches only their own keys; an admin any key of the agent. A jti of
    another agent, another kind of token or an already revoked key revokes nothing.
    """
    row = await mcp_tokens.revoke_coding_key(
        session,
        jti=jti,
        tenant_id=tenant_id,
        agent_id=agent_id,
        now=now,
        account_id=viewer_account_id if access == "owner" else None,
    )
    return row is not None


async def _creator_labels(
    session: AsyncSession, *, tenant_id: uuid.UUID, rows: list[McpTokenRow], platform: str
) -> dict[uuid.UUID, str]:
    """A name for each key maker: the platform's stored display name, else their user id."""
    external = await platform_external_ids(
        session,
        tenant_id=tenant_id,
        platform=platform,
        account_ids={row.account_id for row in rows},
    )
    names = await platform_names.get_user_names(
        session, tenant_id=tenant_id, platform=platform, user_ids=set(external.values())
    )
    labels: dict[uuid.UUID, str] = {}
    for account_id, user_id in external.items():
        known = names.get(user_id)
        labels[account_id] = (known.label if known is not None else None) or user_id
    return labels

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

from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_ownership import account_owns_agent
from daimon.core.agent_target import ChangeRecorder, load_target
from daimon.core.agent_view import people_by_account
from daimon.core.authz import build_agent_ref
from daimon.core.mcp_auth import (
    authorize_coding_token,
    coding_key_ttl_days,
    coding_tool_config,
    mint_agent_mcp_token,
    token_jti,
)
from daimon.core.stores import mcp_tokens, platform_names
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import AccountIdentityRow, McpTokenRow, Role
from daimon.core.stores.identity import list_cli_principals_for_account, platform_external_ids
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

type CodingKeyAccess = Literal["admin", "owner"]


def coding_key_access(*, is_admin: bool, owns_agent: bool) -> CodingKeyAccess | None:
    """What the caller may do with the agent's coding keys: all of them, their own, or nothing."""
    if is_admin:
        return "admin"
    return "owner" if owns_agent else None


async def holds_coding_key_standing(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    identity: AccountIdentityRow | None = None,
) -> bool:
    """Whether the account's coding key for this agent passes the verifier's rule now.

    True while the account is a server admin by stored role, is the CLI operator's
    own account, or owns the agent. The verifier asks it on every request, and a
    mint asks it first, so a key is only ever made for someone it would work for.
    """
    if identity is None:
        identity = await get_account_with_tenant(session, account_id=account_id)
    if identity is None or identity.account_id != account_id or identity.tenant_id != tenant_id:
        return False
    if identity.role is Role.ADMIN:
        return True
    if await list_cli_principals_for_account(session, account_id=identity.account_id):
        return True
    return await account_owns_agent(
        session, tenant_id=tenant_id, agent_id=agent_id, account_id=identity.account_id
    )


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
    """A name for each key maker: the platform's stored display name, else their user id.

    The web has no platform ids: the person's name there, else any chat name they have.
    """
    if platform == "web":
        people = await people_by_account(session, tenant_id, {row.account_id for row in rows})
        return {account: person.name for account, person in people.items()}
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


@dataclasses.dataclass(frozen=True)
class MyCodingKey:
    """One of the person's own live coding keys, across every agent of the workspace."""

    jti: uuid.UUID
    agent_id: str | None
    created_at: dt.datetime
    expires_at: dt.datetime | None
    bound: bool


async def list_my_coding_keys(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID, now: dt.datetime
) -> list[MyCodingKey]:
    """The person's live coding keys on any agent, newest first."""
    rows = await mcp_tokens.list_account_coding_keys(
        session, tenant_id=tenant_id, account_id=account_id, now=now
    )
    return [
        MyCodingKey(
            jti=row.jti,
            agent_id=row.agent_id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            bound=row.channel_id is not None,
        )
        for row in rows
    ]


async def revoke_my_coding_key(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    now: dt.datetime,
) -> bool:
    """Revoke one of the person's own coding keys; False when it is not theirs or is gone."""
    row = await mcp_tokens.revoke_account_coding_key(
        session, jti=jti, tenant_id=tenant_id, account_id=account_id, now=now
    )
    return row is not None


class MintedCodingKey(BaseModel):
    """A freshly made key. The token is shown once and held nowhere else."""

    jti: uuid.UUID
    token: str
    expires_at: dt.datetime
    command: str
    config: str


async def mint_coding_key(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    public_url: str,
    secret: bytes,
    now: dt.datetime,
    audited: ChangeRecorder | None = None,
) -> MintedCodingKey:
    """Make a coding key for the actor on this agent: an admin, or the agent's owner.

    Refused unless the key would pass the verifier's rule for the actor (stored
    admin, CLI operator or owner), since one that would be turned away on its
    first call is not worth showing. The key acts as the actor.
    """
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    async with deps.session_factory() as session:
        policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        standing = await holds_coding_key_standing(
            session,
            tenant_id=actor.tenant_id,
            account_id=actor.account_id,
            agent_id=target.agent_id,
        )
    decision, bound = authorize_coding_token(
        policy,
        subject=actor.subject,
        agent=build_agent_ref(target.agent.name, target.agent.metadata),
        channel_id=None,
        owned_by_caller=target.owned,
    )
    if not decision or not standing:
        raise AgentEditError(
            "Only an admin or this agent's owner can make a coding key.", kind="forbidden"
        )
    ttl_days = coding_key_ttl_days(actor.subject)
    async with deps.session_factory() as session, session.begin():
        token = await mint_agent_mcp_token(
            session,
            account_id=actor.account_id,
            tenant_id=actor.tenant_id,
            agent_id=target.agent_id,
            label=target.agent.name,
            secret=secret,
            now=now,
            ttl_days=ttl_days,
            platform=None,
            channel_id=bound,
            purpose="coding",
        )
        if audited is not None:
            await audited(session, target.agent.name)
    command, config = coding_tool_config(
        agent_name=target.agent.name, public_url=public_url, jwt=token
    )
    return MintedCodingKey(
        jti=token_jti(token),
        token=token,
        expires_at=now + dt.timedelta(days=ttl_days),
        command=command,
        config=config,
    )


async def list_agent_coding_keys(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str, now: dt.datetime
) -> list[CodingKeyEntry]:
    """The coding keys of this agent the actor may see: all for an admin, their own for an owner."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    access = coding_key_access(is_admin=actor.subject.is_admin, owns_agent=target.owned)
    if access is None:
        return []
    async with deps.session_factory() as session:
        return await list_coding_keys(
            session,
            tenant_id=actor.tenant_id,
            agent_id=target.agent_id,
            access=access,
            viewer_account_id=actor.account_id,
            platform="web",
            now=now,
        )


async def revoke_agent_coding_key(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    jti: uuid.UUID,
    now: dt.datetime,
    audited: ChangeRecorder | None = None,
) -> None:
    """Revoke one coding key of this agent: any for an admin, the actor's own for an owner."""
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    access = coding_key_access(is_admin=actor.subject.is_admin, owns_agent=target.owned)
    if access is None:
        raise AgentEditError("No such key.", kind="not_found")
    async with deps.session_factory() as session, session.begin():
        revoked = await revoke_coding_key(
            session,
            jti=jti,
            tenant_id=actor.tenant_id,
            agent_id=target.agent_id,
            access=access,
            viewer_account_id=actor.account_id,
            now=now,
        )
        if not revoked:
            raise AgentEditError("No such key.", kind="not_found")
        if audited is not None:
            await audited(session, target.agent.name)

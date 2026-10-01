"""Async store for mcp_oauth_flows — one row per in-flight MCP OAuth authorization.

The click mints the row (`create_flow`), `/oauth/mcp/start` fills in the
registered client (`save_flow_client`), and the callback spends it
(`consume_flow`), which is the single-use gate: one UPDATE whose WHERE
clause only matches an unused, unexpired row. `mark_flow_completed` then
stamps the row whose grant actually reached a vault, and it outlives the
handshake as the record of who connected what: `list_completed_grants` reads
those rows so a session mounts only the servers its caller can authenticate.
No try/except — DB exceptions propagate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from daimon.core._models import McpOAuthFlow
from daimon.core.stores.domain import McpOAuthFlowRow, McpOAuthGrantRow
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_flow(
    session: AsyncSession,
    *,
    state: str,
    request_token: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    server_name: str,
    mcp_server_url: str,
    redirect_uri: str,
    code_verifier: str,
    expires_at: datetime,
) -> McpOAuthFlowRow:
    orm = McpOAuthFlow(
        state=state,
        request_token=request_token,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        server_name=server_name,
        mcp_server_url=mcp_server_url,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        expires_at=expires_at,
    )
    session.add(orm)
    await session.flush()
    return McpOAuthFlowRow.model_validate(orm)


async def get_flow(session: AsyncSession, *, state: str) -> McpOAuthFlowRow | None:
    """The row for `state` regardless of lifecycle, or None when unknown."""
    orm = await session.scalar(select(McpOAuthFlow).where(McpOAuthFlow.state == state))
    return None if orm is None else McpOAuthFlowRow.model_validate(orm)


async def save_flow_client(
    session: AsyncSession,
    *,
    state: str,
    client_id: str,
    client_secret_encrypted: str | None,
    token_endpoint_auth_method: str,
    token_endpoint: str,
    authorization_endpoint: str,
    resource: str | None,
    scope: str | None,
) -> McpOAuthFlowRow | None:
    """Record the discovered endpoints and registered client on an unspent row.

    Only a row with no client yet takes the write: a second open of the same
    link must reuse the client the first open registered, or the code the
    provider issues to one client would be exchanged as the other.
    """
    stmt = (
        update(McpOAuthFlow)
        .where(
            McpOAuthFlow.state == state,
            McpOAuthFlow.used_at.is_(None),
            McpOAuthFlow.client_id.is_(None),
        )
        .values(
            client_id=client_id,
            client_secret_encrypted=client_secret_encrypted,
            token_endpoint_auth_method=token_endpoint_auth_method,
            token_endpoint=token_endpoint,
            authorization_endpoint=authorization_endpoint,
            resource=resource,
            scope=scope,
        )
        .returning(McpOAuthFlow)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else McpOAuthFlowRow.model_validate(orm)


async def consume_flow(
    session: AsyncSession, *, state: str, now: datetime
) -> McpOAuthFlowRow | None:
    """Atomically spend `state` iff unused and unexpired; None otherwise."""
    stmt = (
        update(McpOAuthFlow)
        .where(
            McpOAuthFlow.state == state,
            McpOAuthFlow.used_at.is_(None),
            McpOAuthFlow.expires_at > now,
        )
        .values(used_at=now)
        .returning(McpOAuthFlow)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    return None if orm is None else McpOAuthFlowRow.model_validate(orm)


async def mark_flow_completed(session: AsyncSession, *, state: str, now: datetime) -> None:
    """Record that this flow's grant reached the person's vault.

    Separate from `consume_flow`, which spends the row before the callback
    knows whether the person approved: only a stored credential makes them
    connected.
    """
    await session.execute(
        update(McpOAuthFlow).where(McpOAuthFlow.state == state).values(completed_at=now)
    )
    await session.flush()


async def list_completed_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, server_urls: Iterable[str]
) -> tuple[McpOAuthGrantRow, ...]:
    """Who has finished a sign-in to any of `server_urls`, on which agent,
    across the tenant.

    Tenant-wide and filtered to the caller's URLs; `mcp_personal_servers`
    explains why a sign-in on one agent matters to another. One row per
    completed flow, so callers work in sets. Trailing slashes are ignored on
    both sides, as everywhere a server URL is compared; the partial index
    from `0023_mcp_oauth_flows_url_ix` covers exactly this predicate. A flow
    that was spent but never exchanged for a grant — a decline, a refused
    code — is not listed.
    """
    result = await session.execute(
        select(McpOAuthFlow.agent_id, McpOAuthFlow.account_id, McpOAuthFlow.mcp_server_url).where(
            McpOAuthFlow.tenant_id == tenant_id,
            McpOAuthFlow.completed_at.is_not(None),
            func.rtrim(McpOAuthFlow.mcp_server_url, "/").in_(
                {url.rstrip("/") for url in server_urls}
            ),
        )
    )
    return tuple(
        McpOAuthGrantRow(agent_id=agent_id, account_id=account_id, mcp_server_url=url)
        for agent_id, account_id, url in result.all()
    )

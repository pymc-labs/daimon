"""OAuth connector tokens Daimon holds and refreshes, for agents not on Managed Agents.

Each grant has one refresh owner. On a Managed Agents agent the MA vault holds
and refreshes the token (`vault`); on every other backend Daimon does, in the
`connector_tokens` table. A backend switch therefore means a reconnect.

A refresh claims the row first (a compare-and-set, no lock held over HTTP), so
concurrent turns spend a rotating refresh token once; the others wait for the
winner's row. The refresh asks for the stored scope only, sends the stored
`resource`, and refuses an answer that widens the scope. Every token-endpoint
call goes through `pinned_http_client`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import MultiFernet
from daimon.core.channel_backend import current_agent_backend, resolve_agent_backend
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.mcp_oauth.flow import (
    McpOAuthFlowError,
    McpOAuthTokenRefusedError,
    refresh_access_token,
)
from daimon.core.mcp_oauth.models import ClientRegistration, TokenResponse
from daimon.core.stores import connector_tokens as store
from daimon.core.stores.domain import ConnectorRefreshOwner, ConnectorTokenRow, McpOAuthFlowRow
from mux.contracts.config import ResolvedBackend
from mux.profiles import MANAGED_AGENTS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

REFRESH_LEASE = dt.timedelta(seconds=60)
"""A claim older than this belongs to a refresher that died; another may take it."""
REFRESH_EARLY = dt.timedelta(seconds=60)
"""Refresh a token this close to expiry before handing it to a turn."""
_WAIT_STEP_S = 0.2

Clock = Callable[[], dt.datetime]
Sleep = Callable[[float], Awaitable[None]]
HttpFactory = Callable[[], httpx.AsyncClient]


class ConnectorReconnectRequiredError(McpOAuthFlowError):
    """The grant is gone or refused; the person must sign in again."""


class ConnectorScopeWidenedError(McpOAuthFlowError):
    """The token endpoint answered a refresh with more scope than was granted."""


class ConnectorResourceRefusedError(McpOAuthFlowError):
    """The grant's `resource` is not on the MCP server's own origin."""


def refresh_owner_for(selection: ResolvedBackend) -> ConnectorRefreshOwner:
    """Who refreshes a grant made for an agent running on `selection`."""
    return "ma" if selection.profile == MANAGED_AGENTS.profile_id else "daimon"


async def agent_refresh_owner(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, selection_enabled: bool
) -> ConnectorRefreshOwner:
    """The refresh owner for the agent's current backend (MA when selection is off)."""
    revision = await current_agent_backend(session, tenant_id, agent_id)
    return refresh_owner_for(resolve_agent_backend(None, revision, enabled=selection_enabled))


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


async def store_connector_grant(
    session: AsyncSession,
    *,
    flow: McpOAuthFlowRow,
    tokens: TokenResponse,
    client: ClientRegistration,
    fernet: MultiFernet,
    now: dt.datetime,
) -> ConnectorTokenRow:
    """Store a finished sign-in as the requester's own Daimon-refreshed token."""
    if flow.token_endpoint is None:
        raise McpOAuthFlowError("a connector grant needs the flow's token endpoint")
    if flow.resource is not None and _origin(flow.resource) != _origin(flow.mcp_server_url):
        raise ConnectorResourceRefusedError("the grant's resource is not the MCP server")
    return await store.upsert_connector_token(
        session,
        tenant_id=flow.tenant_id,
        account_id=flow.account_id,
        agent_id=flow.agent_id,
        server_url=flow.mcp_server_url,
        access_token_encrypted=encrypt_token(fernet, tokens.access_token),
        refresh_token_encrypted=(
            encrypt_token(fernet, tokens.refresh_token) if tokens.refresh_token else None
        ),
        token_endpoint=flow.token_endpoint,
        client_id=client.client_id,
        client_secret_encrypted=(
            encrypt_token(fernet, client.client_secret) if client.client_secret else None
        ),
        token_endpoint_auth_method=client.token_endpoint_auth_method,
        scope=tokens.scope,
        resource=flow.resource,
        expires_at=_expiry(tokens, now),
        refresh_owner="daimon",
        now=now,
    )


def _expiry(tokens: TokenResponse, now: dt.datetime) -> dt.datetime | None:
    return None if tokens.expires_in is None else now + dt.timedelta(seconds=tokens.expires_in)


async def connector_token_for_turn(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    server_url: str,
    requester_account_id: uuid.UUID | None,
    hide_personal: bool,
) -> ConnectorTokenRow | None:
    """The token a turn may use: the requester's own, else the agent-wide one.

    `hide_personal` (the turn's `hide_personal_for` is set: a shared or public
    place) never yields a personal token. MA-owned rows are never returned.
    """
    async with session_factory() as session:
        if requester_account_id is not None and not hide_personal:
            row = await store.get_connector_token(
                session,
                tenant_id=tenant_id,
                account_id=requester_account_id,
                agent_id=agent_id,
                server_url=server_url,
            )
            if row is not None and row.refresh_owner == "daimon":
                return row
        row = await store.get_connector_token(
            session, tenant_id=tenant_id, account_id=None, agent_id=agent_id, server_url=server_url
        )
    return row if row is not None and row.refresh_owner == "daimon" else None


async def connector_access_token(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    server_url: str,
    requester_account_id: uuid.UUID | None,
    hide_personal: bool,
    fernet: MultiFernet,
    clock: Clock = lambda: dt.datetime.now(dt.UTC),
    http_factory: HttpFactory | None = None,
    sleep: Sleep = asyncio.sleep,
) -> str | None:
    """The bearer token for this turn, refreshed first when near expiry; None to reconnect."""
    row = await connector_token_for_turn(
        session_factory,
        tenant_id=tenant_id,
        agent_id=agent_id,
        server_url=server_url,
        requester_account_id=requester_account_id,
        hide_personal=hide_personal,
    )
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at - clock() <= REFRESH_EARLY:
        try:
            row = await refresh_connector_token(
                session_factory,
                tenant_id=tenant_id,
                token_id=row.id,
                fernet=fernet,
                clock=clock,
                http_factory=http_factory,
                sleep=sleep,
            )
        except ConnectorReconnectRequiredError:
            return None
        except (McpOAuthFlowError, httpx.HTTPError):
            # A transient failure: a token that has not expired yet still works.
            if row.expires_at <= clock():
                raise
    return decrypt_token(fernet, row.access_token_encrypted)


async def refresh_connector_token(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    token_id: uuid.UUID,
    fernet: MultiFernet,
    clock: Clock = lambda: dt.datetime.now(dt.UTC),
    http_factory: HttpFactory | None = None,
    sleep: Sleep = asyncio.sleep,
    wait_s: float = 30.0,
) -> ConnectorTokenRow:
    """Refresh one Daimon-owned grant; concurrent callers share one token call.

    Raises `ConnectorReconnectRequiredError` when the grant is gone, has no
    refresh token, or the server answers `invalid_grant` (the row is deleted).
    """
    async with session_factory() as session:
        row = await store.get_connector_token_by_id(session, tenant_id=tenant_id, token_id=token_id)
    if row is None or row.refresh_owner != "daimon" or row.refresh_token_encrypted is None:
        raise ConnectorReconnectRequiredError("the connector grant cannot be refreshed")
    now = clock()
    async with session_factory() as session, session.begin():
        claim = await store.claim_refresh(
            session,
            tenant_id=tenant_id,
            token_id=token_id,
            read_at=row.updated_at,
            stale_before=now - REFRESH_LEASE,
            now=now,
        )
    if claim is None:
        return await _await_other_refresh(
            session_factory, row, tenant_id=tenant_id, clock=clock, sleep=sleep, wait_s=wait_s
        )
    try:
        tokens = await _request_refresh(claim, fernet=fernet, http_factory=http_factory)
    except McpOAuthTokenRefusedError as err:
        if err.error_code != "invalid_grant":
            await _release(session_factory, claim, clock=clock)
            raise
        async with session_factory() as session, session.begin():
            await store.delete_connector_token(
                session, tenant_id=tenant_id, token_id=token_id, read_at=claim.updated_at
            )
        raise ConnectorReconnectRequiredError("the server refused the grant") from err
    except BaseException:
        await _release(session_factory, claim, clock=clock)
        raise
    assert claim.refresh_token_encrypted is not None  # checked on the row it claimed
    now = clock()
    async with session_factory() as session, session.begin():
        written = await store.update_refreshed_token(
            session,
            tenant_id=tenant_id,
            token_id=token_id,
            read_at=claim.updated_at,
            access_token_encrypted=encrypt_token(fernet, tokens.access_token),
            refresh_token_encrypted=(
                encrypt_token(fernet, tokens.refresh_token)
                if tokens.refresh_token
                else claim.refresh_token_encrypted
            ),
            scope=tokens.scope or claim.scope,
            expires_at=_expiry(tokens, now),
            now=now,
        )
        if written is None:  # a reconnect or disconnect landed meanwhile; it wins
            written = await store.get_connector_token_by_id(
                session, tenant_id=tenant_id, token_id=token_id
            )
    if written is None:
        raise ConnectorReconnectRequiredError("the connector grant was removed")
    return written


async def _release(
    session_factory: async_sessionmaker[AsyncSession], claim: ConnectorTokenRow, *, clock: Clock
) -> None:
    async with session_factory() as session, session.begin():
        await store.release_refresh(
            session,
            tenant_id=claim.tenant_id,
            token_id=claim.id,
            read_at=claim.updated_at,
            now=clock(),
        )


async def _request_refresh(
    row: ConnectorTokenRow, *, fernet: MultiFernet, http_factory: HttpFactory | None
) -> TokenResponse:
    assert row.refresh_token_encrypted is not None
    if http_factory is None:
        # Imported here: mcp_tool_call reaches back into this package.
        from daimon.core.mcp_tool_call import pinned_http_client

        http_factory = pinned_http_client
    client = ClientRegistration(
        client_id=row.client_id,
        client_secret=(
            decrypt_token(fernet, row.client_secret_encrypted)
            if row.client_secret_encrypted is not None
            else None
        ),
        token_endpoint_auth_method=row.token_endpoint_auth_method,
    )
    async with http_factory() as http:
        tokens = await refresh_access_token(
            http,
            token_endpoint=row.token_endpoint,
            refresh_token=decrypt_token(fernet, row.refresh_token_encrypted),
            client=client,
            scope=row.scope,
            resource=row.resource,
        )
    if tokens.scope is not None and row.scope is not None:
        granted = {*row.scope.split(), "offline_access"}
        if not set(tokens.scope.split()) <= granted:
            raise ConnectorScopeWidenedError("the refresh returned more scope than was granted")
    return tokens


async def _await_other_refresh(
    session_factory: async_sessionmaker[AsyncSession],
    row: ConnectorTokenRow,
    *,
    tenant_id: uuid.UUID,
    clock: Clock,
    sleep: Sleep,
    wait_s: float,
) -> ConnectorTokenRow:
    """Wait for the refresher holding the claim, then use its row."""
    deadline = clock() + dt.timedelta(seconds=wait_s)
    while clock() < deadline:
        async with session_factory() as session:
            current = await store.get_connector_token_by_id(
                session, tenant_id=tenant_id, token_id=row.id
            )
        if current is None:
            raise ConnectorReconnectRequiredError("the connector grant was removed")
        if current.refresh_started_at is None and current.updated_at != row.updated_at:
            return current
        await sleep(_WAIT_STEP_S)
    raise McpOAuthFlowError("another refresh of this connector is still running")


__all__ = [
    "REFRESH_EARLY",
    "REFRESH_LEASE",
    "ConnectorReconnectRequiredError",
    "ConnectorResourceRefusedError",
    "ConnectorScopeWidenedError",
    "agent_refresh_owner",
    "connector_access_token",
    "connector_token_for_turn",
    "refresh_connector_token",
    "refresh_owner_for",
    "store_connector_grant",
]

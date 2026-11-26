"""A provider turn's GitHub MCP router session, for the `github` connector off Claude.

OpenAI and Gemini cannot hold a GitHub token, so the connector relay calls the
GitHub MCP router for them with a router bearer only Daimon holds. Each turn
credential gets its own router session, built the way a Managed Agents session's
is (`github_router_sessions.create`, `prepare_app_access`, `finish_app_delivery`):
its tokens carry the turn's session id, so `close_turn_router` at turn end closes
the record first and then revokes every token, through `revoke_session_tokens`.
The record lapses with the turn credential, inside the tokens' own hour, so it
never needs a rotation (a longer credential gets a fresh router); the scheduler's
stale sweep still revokes a token whose grant is removed mid-turn.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from cryptography.fernet import MultiFernet
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import (
    REQUESTER_CACHE,
    effective_app_grant_view,
    finish_app_delivery,
    prepare_app_access,
    revoke_app_access,
    revoke_session_tokens,
)
from daimon.core.stores import github_router_sessions
from daimon.core.stores.mcp_tokens import get_mcp_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TURN_SESSION_PREFIX: Final = "relay-turn:"
TURN_ROUTER_TTL: Final = timedelta(minutes=50)
"""A router record's longest life: inside its tokens' hour, so none needs a rotation."""


def turn_session_id(turn_jti: uuid.UUID) -> str:
    """The session id a turn's router record and tokens carry."""
    return f"{TURN_SESSION_PREFIX}{turn_jti}"


async def open_turn_router(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    turn_jti: uuid.UUID,
    expires_at: datetime,
    config: GithubAppSettings,
    fernet: MultiFernet,
) -> str | None:
    """A router bearer for this turn's App tokens; None without repository access.

    Opened only while the turn credential is live: one revoked meanwhile (the turn
    ended) gets everything opened here closed again.
    """
    grant_view = await effective_app_grant_view(
        sessionmaker, tenant_id=tenant_id, agent_id=agent_id, fernet=fernet
    )
    if not grant_view[0]:
        return None
    session_id = turn_session_id(turn_jti)
    async with sessionmaker.begin() as db:
        record, bearer = await github_router_sessions.create(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            expires_at=min(expires_at, datetime.now(UTC) + TURN_ROUTER_TTL),
            ma_session_id=session_id,
        )
    provisional = f"pending:{session_id}:{record.id}"
    access = None
    try:
        access = await prepare_app_access(
            sessionmaker,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            provisional_session_id=provisional,
            config=config,
            fernet=fernet,
            cache=REQUESTER_CACHE,
            grant_view=grant_view,
            router_session_id=record.id,
        )
        await finish_app_delivery(
            sessionmaker,
            client,
            access=access,
            provisional_session_id=provisional,
            session_id=session_id,
            router_session_id=record.id,
        )
    except BaseException:
        async with sessionmaker.begin() as db:
            await github_router_sessions.close_local(db, router_session_id=record.id)
        if access is not None:
            await revoke_app_access(sessionmaker, client, access)
        raise
    async with sessionmaker() as db:
        credential = await get_mcp_token(db, jti=turn_jti)
    if not access.tokens or credential is None or credential.revoked_at is not None:
        await close_turn_router(sessionmaker, client, turn_jti=turn_jti, fernet=fernet)
        return None
    return bearer


async def close_turn_router(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    turn_jti: uuid.UUID,
    fernet: MultiFernet,
) -> None:
    """Close every router record of the turn, then revoke its tokens (idempotent)."""
    await revoke_session_tokens(
        sessionmaker, client, session_id=turn_session_id(turn_jti), fernet=fernet
    )


__all__ = [
    "TURN_ROUTER_TTL",
    "TURN_SESSION_PREFIX",
    "close_turn_router",
    "open_turn_router",
    "turn_session_id",
]

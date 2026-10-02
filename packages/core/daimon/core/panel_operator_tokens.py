"""Operator tokens a server admin mints, lists and revokes from a setup panel.

The panel offers only the tenant scopes; `promo:create` acts for the whole
deployment, so only `daimon mcp mint-operator-token` mints it, and a token
carrying it is neither listed nor revoked here. The adapter
checks the clicker is a server admin live before calling in; minting stores
that role on their account, as a turn does, because the verifier reads the
stored role on every call.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Final

from daimon.core.mcp_auth import mint_operator_mcp_token, token_jti
from daimon.core.operator_tokens import (
    DEPLOYMENT_SCOPES,
    OPERATOR_SCOPES,
    OperatorScope,
    OperatorTokenError,
    parse_operator_scopes,
)
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import McpTokenRow, Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.mcp_tokens import get_mcp_token, list_mcp_tokens, revoke_mcp_token
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "PANEL_SCOPES",
    "PANEL_TTL_DAYS",
    "MintedOperatorToken",
    "list_panel_operator_tokens",
    "mint_panel_operator_token",
    "operator_token_line",
    "revoke_panel_operator_token",
]

PANEL_SCOPES: Final[tuple[OperatorScope, ...]] = tuple(
    scope for scope in OPERATOR_SCOPES if scope not in DEPLOYMENT_SCOPES
)
PANEL_TTL_DAYS: Final = 30
LABEL_MAX_CHARS: Final = 100


@dataclasses.dataclass(frozen=True)
class MintedOperatorToken:
    token: str
    jti: uuid.UUID
    scopes: frozenset[OperatorScope]
    expires_at: dt.datetime


async def mint_panel_operator_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    scopes: Iterable[str],
    label: str | None,
    secret: bytes,
    now: dt.datetime,
) -> MintedOperatorToken:
    """Mint a tenant-scoped operator token for a clicker already checked to be a server admin.

    Raises `OperatorTokenError` for no scope, an unknown one, or a deployment scope.
    """
    parsed = parse_operator_scopes(scopes)
    beyond = sorted(parsed.difference(PANEL_SCOPES))
    if beyond:
        raise OperatorTokenError(
            f"{', '.join(beyond)} is minted only with daimon mcp mint-operator-token"
        )
    principal = await get_or_create_platform_principal(
        session, tenant_id=tenant_id, platform=platform, external_id=platform_user_id
    )
    await set_role(session, principal.account_id, Role.ADMIN)
    token = await mint_operator_mcp_token(
        session,
        account_id=principal.account_id,
        tenant_id=tenant_id,
        scopes=parsed,
        label=(label or "").strip()[:LABEL_MAX_CHARS] or None,
        secret=secret,
        now=now,
        ttl_days=PANEL_TTL_DAYS,
    )
    return MintedOperatorToken(
        token=token,
        jti=token_jti(token),
        scopes=parsed,
        expires_at=now + dt.timedelta(days=PANEL_TTL_DAYS),
    )


def _is_panel_token(row: McpTokenRow) -> bool:
    """A tenant-scoped operator token; one with a deployment scope belongs to the CLI."""
    return row.kind == "operator" and set(row.scopes) <= set(PANEL_SCOPES)


async def list_panel_operator_tokens(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: dt.datetime
) -> list[McpTokenRow]:
    """The tenant's live tenant-scoped operator tokens, newest first. Rows hold no secret."""
    rows = await list_mcp_tokens(session, now=now, tenant_id=tenant_id, kind="operator")
    return [row for row in rows if _is_panel_token(row)]


async def revoke_panel_operator_token(
    session: AsyncSession, *, tenant_id: uuid.UUID, jti: uuid.UUID, now: dt.datetime
) -> bool:
    """Revoke one of the tenant's live tenant-scoped operator tokens; False when there is
    none to revoke. A token with a deployment scope is revoked only with the CLI."""
    row = await get_mcp_token(session, jti=jti)
    if row is None or row.tenant_id != tenant_id or not _is_panel_token(row):
        return False
    return await revoke_mcp_token(session, jti=jti, now=now) is not None


def operator_token_line(row: McpTokenRow) -> str:
    """One listing line: short id, label, scopes and expiry. Never the token."""
    expires = row.expires_at.date().isoformat() if row.expires_at else "never"
    label = row.label or "no label"
    return f"{str(row.jti)[:8]} · {label} · {', '.join(sorted(row.scopes))} · expires {expires}"

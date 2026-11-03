"""Operator tokens a server admin mints, lists and revokes from a setup panel.

The panel offers only the tenant scopes; `promo:create` acts for the whole
deployment, so only `daimon mcp mint-operator-token` mints it, and a token
carrying it is neither listed nor revoked here. Each call takes the actor as a
`Subject` and refuses a non-admin (`authorize(MANAGE_INTEGRATIONS)`). Minting never
changes account roles and requires the native snapshot the verifier enforces; a
missing snapshot is refused before a token exists.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Final

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.mcp_auth import mint_operator_mcp_token, token_jti
from daimon.core.operator_tokens import (
    DEPLOYMENT_SCOPES,
    OPERATOR_SCOPES,
    OperatorScope,
    OperatorTokenError,
    parse_operator_scopes,
)
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import McpTokenRow, Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.mcp_tokens import get_mcp_token, list_mcp_tokens, revoke_mcp_token
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "PANEL_SCOPES",
    "PANEL_TTL_DAYS",
    "MintedOperatorToken",
    "OperatorTokenNotAllowed",
    "panel_admin",
    "list_panel_operator_tokens",
    "mint_panel_operator_token",
    "operator_token_line",
    "revoke_panel_operator_token",
]


class OperatorTokenNotAllowed(OperatorTokenError):
    """The caller is not a server admin."""


def panel_admin(platform_user_id: str) -> Subject:
    """The actor for a panel click already checked, live, to be a server admin."""
    return Subject(is_admin=True, platform_user_id=platform_user_id)


def _require_admin(subject: Subject) -> None:
    if not authorize(TenantAccessPolicy(), subject=subject, action=Action.MANAGE_INTEGRATIONS):
        raise OperatorTokenNotAllowed("Ask an admin or an owner")


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
    subject: Subject,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    scopes: Iterable[str],
    label: str | None,
    secret: bytes,
    now: dt.datetime,
) -> MintedOperatorToken:
    """Mint a tenant-scoped operator token for a server admin.

    Raises `OperatorTokenNotAllowed` for a non-admin and `OperatorTokenError` for no scope,
    an unknown one, or a deployment scope.
    """
    _require_admin(subject)
    parsed = parse_operator_scopes(scopes)
    beyond = sorted(parsed.difference(PANEL_SCOPES))
    if beyond:
        raise OperatorTokenError(
            f"{', '.join(beyond)} can't be granted from here; whoever runs this Daimon "
            "install can make a token with it"
        )
    principal = await get_or_create_platform_principal(
        session, tenant_id=tenant_id, platform=platform, external_id=platform_user_id
    )
    account = await get_account(session, principal.account_id)
    if account is None or account.role is not Role.ADMIN:
        raise OperatorTokenNotAllowed(
            "Your platform admin role has not been recorded yet. "
            "Send Daimon a message in this server or workspace, then try minting again. "
            "No operator token was created"
        )
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
    """The tenant's live tenant-scoped operator tokens, newest first. Rows hold no secret.

    Listing takes no actor: every caller already shows it to admins only."""
    rows = await list_mcp_tokens(session, now=now, tenant_id=tenant_id, kind="operator")
    return [row for row in rows if _is_panel_token(row)]


async def revoke_panel_operator_token(
    session: AsyncSession,
    *,
    subject: Subject,
    tenant_id: uuid.UUID,
    jti: uuid.UUID,
    now: dt.datetime,
) -> bool:
    """Revoke one of the tenant's live tenant-scoped operator tokens; False when there is
    none to revoke. A token with a deployment scope is revoked only with the CLI."""
    _require_admin(subject)
    row = await get_mcp_token(session, jti=jti)
    if row is None or row.tenant_id != tenant_id or not _is_panel_token(row):
        return False
    return await revoke_mcp_token(session, jti=jti, now=now) is not None


def operator_token_line(row: McpTokenRow) -> str:
    """One listing line: short id, label, scopes and expiry. Never the token.

    The id comes first so it survives the platforms' option-length cuts."""
    expires = row.expires_at.date().isoformat() if row.expires_at else "never"
    label = row.label or "no label"
    scopes = ", ".join(sorted(row.scopes))
    return f"{str(row.jti)[:8]} {label}: {scopes}, expires {expires}"

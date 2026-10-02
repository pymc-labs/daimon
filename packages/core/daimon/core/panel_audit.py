"""Security audit rows for the panel writes that need admin rights.

Setup and billing panels run outside the MCP audit scope, so each admin-tier
write records its own `security_audit_events` row, allowed or refused, with
tool name `panel:<op>`. Like the MCP trail it is best effort: a lost row never
changes what the panel answers. Never pass a promo code, a token or any other
secret; a token's jti is its public id.

The clicker's ids are kept only when they have an account here, because
privacy erasure clears audit rows by account: a row naming a platform user with
no account could never be erased.
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.security_audit import append_event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["PanelOp", "PanelOutcome", "record_panel_write"]

log = structlog.get_logger(__name__)

PanelOp = Literal[
    "isolation",
    "channel_admins",
    "environment",
    "channel_skills",
    "coding_token_mint",
    "coding_token_revoke",
    "promo_redeem",
    "operator_token_mint",
    "operator_token_revoke",
]

PanelOutcome = Literal["allowed", "denied", "error"]


async def record_panel_write(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    op: PanelOp,
    outcome: PanelOutcome,
    reason: str,
    token_kind: str | None = None,
    token_jti: uuid.UUID | None = None,
) -> None:
    """Append one panel write's row in its own transaction; never raises on a DB failure.

    `reason` is a fixed code (`completed`, `needs_admin`, `authz:<reason>`, ...),
    never person-facing copy or anything the caller typed.
    """
    try:
        async with sessionmaker.begin() as session:
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform=platform, external_id=platform_user_id
            )
            await append_event(
                session,
                tenant_id=tenant_id,
                account_id=principal.account_id if principal is not None else None,
                agent_id=None,
                platform=platform,
                platform_user_id=platform_user_id if principal is not None else None,
                tool_name=f"panel:{op}",
                operation=op,
                outcome=outcome,
                reason=reason,
                token_kind=token_kind,
                token_jti=token_jti,
            )
    except SQLAlchemyError as exc:  # boundary: a lost audit row must not fail the click
        log.warning(
            "security_audit.write_failed", tool_name=f"panel:{op}", error=type(exc).__name__
        )

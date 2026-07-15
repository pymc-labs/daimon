"""Pure JWT minting for daimon-mcp tokens.

Shared primitive between `daimon.core.mcp_vault.ensure_mcp_vault` and the
CLI's `daimon mcp mint-token` command — both need to sign, neither should
cross the core/adapter boundary to reach the FastMCP verifier.

No I/O, no globals. `now` is injected per guideline:architecture so tests
are deterministic and the function stays pure.

Server-side verification lives in `daimon.adapters.mcp.auth.verifier`
(FastMCP's `JWTVerifier`, which is authlib-backed). We don't verify here.

`mint_internal_mcp_token` is a v1.1 seam used by the headless routine runner
and Phase 28's `/mcp-token mint` command. Phase 20 (Billing & Usage) may
supersede it with a tenant-aware variant — keep the signature stable.
"""

from __future__ import annotations

import datetime as dt
import uuid

import jwt as pyjwt
from sqlalchemy.ext.asyncio import AsyncSession


def mint_jwt(
    *,
    account_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
    agent_id: uuid.UUID | None = None,
    is_admin: bool = False,
) -> str:
    """Sign `{sub: <account_uuid>, iat: <unix ts>}` HS256 with `secret`.

    This is the Discord/CLI mint path. It intentionally NEVER emits the ``internal``
    claim, so a Discord vault token cannot be treated as a trusted internal token
    by the MCP admin gate (ADMIN-01/closes #162). Admin elevation for Discord tokens
    comes exclusively from the live DB ``role`` re-read by the verifier each turn.

    - ``sub`` is the string form of the account UUID (stable across principal renames).
    - ``iat`` is ``int(now.timestamp())`` (UTC assumed; caller owns tz).
    - No ``exp`` — role is resolved live from the DB on each request.
    - When ``agent_id`` is supplied, the optional ``"agent_id"`` claim is added (Phase 19,
      D-10/D-27). Used by the MCP ``get_cli_token`` tool to mint per-service tokens
      scoped to the agent without trusting tool-supplied parameters.
    - When ``is_admin`` is ``True``, an ``"is_admin": True`` claim is added (Phase 50,
      RBAC-02). Omitted when ``False`` to keep non-admin tokens minimal. Note: the MCP
      admin gate only trusts ``is_admin`` when the token also carries ``internal=True``
      (minted only by ``mint_internal_mcp_token``), so a Discord vault token's baked
      ``is_admin`` claim alone never elevates a non-admin caller.
    """
    claims: dict[str, str | int | bool] = {
        "sub": str(account_id),
        "iat": int(now.timestamp()),
    }
    if agent_id is not None:
        claims["agent_id"] = str(agent_id)
    if is_admin:
        claims["is_admin"] = True
    return pyjwt.encode(claims, secret, algorithm="HS256")


def mint_internal_mcp_token(
    *,
    account_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
) -> str:
    """Sign a token for headless (routine-driven) MCP calls.

    Claims: ``{sub: <account_uuid>, iat, is_admin: True, internal: True}``.
    Signed HS256 with ``secret``. ``sub`` is the stringified account UUID (stable
    across principal renames); ``iat`` is ``int(now.timestamp())``.

    ``is_admin: True`` is always minted per D-50b — headless/routine tokens run with
    admin privileges.

    ``internal: True`` is the trusted-token discriminator (ADMIN-02). The MCP admin
    gate keys on ``is_admin AND internal`` together: a Discord vault token minted by
    ``mint_jwt`` never carries ``internal``, so a stale pre-sweep Discord vault cred
    with ``is_admin=True`` and ``role=user`` is DENIED admin elevation at the gate.
    Only tokens minted here (CLI/scheduler/headless) carry both claims and can gain
    admin via the claim path.

    Phase 20 (Billing & Usage) may supersede this with a tenant-aware
    variant — the signature is the seam, keep it stable. Phase 28's
    ``/mcp-token mint`` reuses this helper directly.
    """
    return pyjwt.encode(
        {
            "sub": str(account_id),
            "iat": int(now.timestamp()),
            "is_admin": True,
            "internal": True,
        },
        secret,
        algorithm="HS256",
    )


async def mint_agent_mcp_token(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    label: str | None,
    secret: bytes,
    now: dt.datetime,
    ttl_days: int = 90,
) -> str:
    """Mint a long-lived, revocable, agent-scoped MCP JWT.

    Writes an mcp_tokens row (jti registry) then signs a token carrying
    exactly {sub, agent_id, jti, exp} per A1 — no role, no tenant_id.
    The verifier re-derives role and tenant from the DB on each request.

    `agent_id` is the derived per-agent UUID (A2, derive_agent_uuid output).
    It is stringified into both the row column and the `agent_id` claim.

    `now` and `secret` are injected per guideline:architecture — no globals,
    no datetime.now() inside core logic.

    `ttl_days` defaults to 90 days (long-lived, revoked via revoke_mcp_token
    rather than token rotation).
    """
    from daimon.core.stores.mcp_tokens import create_mcp_token_row

    jti = uuid.uuid4()
    await create_mcp_token_row(
        session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=str(agent_id),
        label=label,
        created_at=now,
    )
    exp = int((now + dt.timedelta(days=ttl_days)).timestamp())
    return pyjwt.encode(
        {
            "sub": str(account_id),
            "agent_id": str(agent_id),
            "jti": str(jti),
            "exp": exp,
        },
        secret,
        algorithm="HS256",
    )

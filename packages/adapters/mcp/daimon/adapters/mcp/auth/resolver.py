"""Role resolution — reads from DB-populated JWT claims.

Phase 10: role is stashed in `claims["role"]` by `DaimonJWTVerifier.verify_token`
(which reads the DB `accounts.role` column). `resolve_role` is a pure sync
function that maps the claim string to the `Role` enum; unknown/missing defaults
to USER (safe default).

The `AuthIdentity` dataclass is what tool handlers read from
`await ctx.get_state("auth")` — it's the ONLY place role/account
information flows into tool code.

Phase 22 adds two optional fields populated from JWT claims by `IdentityMiddleware`:
  - `platform`: the caller's platform (e.g. "discord"), or None for v1.0-style tokens.
  - `external_id`: the caller's guild snowflake (from tenant.external_id), or None.
Existing tools (agents/sessions/time/environments/skills/vault) ignore these fields.
Routines tools require both to be non-None and raise `ToolError` otherwise.

Phase 19 (D-10/D-27) adds one optional field populated from the JWT `agent_id` claim:
  - `agent_id`: the MA agent UUID for the caller's agent-session token, or None.
The MCP `get_cli_token` tool reads this server-side instead of accepting it as a tool
parameter (confused-deputy mitigation, T-19-04-01). Path B (most-recent-active-session
walks) and Path C (re-call MA `sessions.retrieve`) are explicitly REJECTED.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.stores.domain import Role


@dataclass(frozen=True, kw_only=True)
class AuthIdentity:
    account_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
    platform: str | None = None
    external_id: str | None = None  # guild snowflake from tenant.external_id
    agent_id: uuid.UUID | None = None  # Phase 19 (D-10/D-27)
    platform_user_id: str | None = None
    # Phase 50 (RBAC-02): True when the minted JWT carries is_admin=True.
    # Derived by the adapter from Discord owner/manage_guild/administrator or CLI context.
    is_admin: bool = False


def resolve_role(role_str: str | None) -> Role:
    """Map the role claim string to a Role enum. Unknown/missing defaults to USER."""
    if role_str == Role.ADMIN.value:
        return Role.ADMIN
    return Role.USER

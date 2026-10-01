"""Role resolution — reads from DB-populated JWT claims.

Role is stashed in `claims["role"]` by `DaimonJWTVerifier.verify_token`
(which reads the DB `accounts.role` column). `resolve_role` is a pure sync
function that maps the claim string to the `Role` enum; unknown/missing defaults
to USER (safe default).

The `AuthIdentity` dataclass is what tool handlers read from
`await ctx.get_state("auth")` — it's the ONLY place role/account
information flows into tool code.

Two optional fields are populated from JWT claims by `IdentityMiddleware`:
  - `platform`: the caller's platform (e.g. "discord"), or None for v1.0-style tokens.
  - `external_id`: the caller's guild snowflake (from tenant.external_id), or None.
Existing tools (agents/sessions/time/environments/skills/vault) ignore these fields.
Routines tools require both to be non-None and raise `ToolError` otherwise.

One optional field is populated from the JWT `agent_id` claim:
  - `agent_id`: the MA agent UUID for the caller's agent-session token, or None.
The MCP `get_cli_token` tool reads this server-side instead of accepting it as a tool
parameter (confused-deputy mitigation, T-19-04-01). Path B (most-recent-active-session
walks) and Path C (re-call MA `sessions.retrieve`) are explicitly REJECTED.

The separate ``chat_agent_id`` claim populates ``chat_agent_id`` only. Ordinary
chat keeps ``agent_id=None``; only the Google token path consumes chat identity.

``token_kind``, ``token_jti`` and ``scopes`` come from the token's registry row
(the verifier writes them under verifier-only claim keys). An operator token
sees only the tools tagged with its scopes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.stores.domain import McpTokenKind, Role


@dataclass(frozen=True, kw_only=True)
class AuthIdentity:
    account_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
    platform: str | None = None
    external_id: str | None = None  # guild snowflake from tenant.external_id
    agent_id: uuid.UUID | None = None
    # Ordinary chat execution identity; consumed only by the Google token broker.
    chat_agent_id: uuid.UUID | None = None
    platform_user_id: str | None = None
    # True when the minted JWT carries is_admin=True.
    # Derived by the adapter from Discord owner/manage_guild/administrator or CLI context.
    is_admin: bool = False
    # Signed execution grant; never supplied as a tool parameter.
    slack_turn_context_id: uuid.UUID | None = None
    # Set from the token's `mcp_tokens` row by the verifier; None for jti-less tokens.
    token_kind: McpTokenKind | None = None
    token_jti: uuid.UUID | None = None
    # What an operator token may call, read from its row on every request.
    scopes: frozenset[str] = frozenset()
    # Role ids the account held on its last chat turn, and the channels its
    # channel admin grants name; both read by the verifier from the database.
    platform_role_ids: tuple[str, ...] = ()
    administered_channel_ids: frozenset[str] = frozenset()

    @property
    def is_operator(self) -> bool:
        return self.token_kind == "operator"

    @property
    def is_channel_admin(self) -> bool:
        return bool(self.administered_channel_ids)


def resolve_role(role_str: str | None) -> Role:
    """Map the role claim string to a Role enum. Unknown/missing defaults to USER."""
    if role_str == Role.ADMIN.value:
        return Role.ADMIN
    return Role.USER

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
parameter (confused-deputy mitigation). Path B (most-recent-active-session
walks) and Path C (re-call MA `sessions.retrieve`) are explicitly REJECTED.

The separate ``chat_agent_id`` claim populates ``chat_agent_id`` only. Ordinary
chat keeps ``agent_id=None``. Origin tools additionally require the signed
``chat_session_id`` grant to match the live origin's server-prepared session.

``token_kind``, ``token_jti`` and ``scopes`` come from the token's registry row
(the verifier writes them under verifier-only claim keys). An operator token
sees only the tools tagged with its scopes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.admin_actions import DelegatedAction, admin_for_action, is_personal_user_credential
from daimon.core.stores.domain import McpTokenKind, Role


@dataclass(frozen=True, kw_only=True)
class AuthIdentity:
    account_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role
    platform: str | None = None
    external_id: str | None = None  # guild snowflake from tenant.external_id
    agent_id: uuid.UUID | None = None
    # Signed lead-intake routine execution grant, never a tool argument.
    routine_id: uuid.UUID | None = None
    execution_id: uuid.UUID | None = None
    # Ordinary chat execution identity (see the module docstring).
    chat_agent_id: uuid.UUID | None = None
    platform_user_id: str | None = None
    # Resolved from current platform authority and grants at the request boundary.
    # Platform callers never inherit a baked JWT admin claim.
    is_admin: bool = False
    # Separately attested; delegated admins never acquire this bit.
    is_platform_admin: bool = False
    # Live membership evidence, never copied from token claims or account role ids.
    verified_role_ids: tuple[str, ...] = ()
    # Signed execution grant; never supplied as a tool parameter.
    slack_turn_context_id: uuid.UUID | None = None
    # Signed grant for one isolated provider session, never a tool argument.
    chat_session_id: str | None = None
    # Set from the token's `mcp_tokens` row by the verifier; None for jti-less tokens.
    is_chat_execution: bool = False
    token_kind: McpTokenKind | None = None
    token_label: str | None = None
    token_jti: uuid.UUID | None = None
    # What an operator token may call, read from its row on every request.
    scopes: frozenset[str] = frozenset()
    # The channel the agent key was minted in, from its mcp_tokens row; read
    # through `token_channel_id`.
    bound_channel_id: str | None = None

    def admin_for(self, action: DelegatedAction | None = None) -> bool:
        return (
            admin_for_action(
                platform_admin=self.is_platform_admin,
                daimon_admin=self.is_admin,
                action=action,
            )
            and not self.is_external_agent
        )

    @property
    def is_personal_user(self) -> bool:
        """Human-held user credentials only; execution shape never confers human intent."""
        return is_personal_user_credential(
            token_kind=self.token_kind,
            token_label=self.token_label,
            is_chat_execution=self.is_chat_execution,
            execution_bindings=(
                self.agent_id,
                self.chat_agent_id,
                self.routine_id,
                self.execution_id,
                self.chat_session_id,
                self.slack_turn_context_id,
            ),
        )

    @property
    def is_external_agent(self) -> bool:
        """External keys cannot inherit their owner's configuration authority."""
        return self.agent_id is not None or (
            self.token_kind == "agent" and not self.is_chat_execution
        )

    @property
    def is_operator(self) -> bool:
        return self.token_kind == "operator"


def token_channel_id(auth: AuthIdentity) -> str | None:
    """The channel an agent key's calls run inside, or None.

    A key minted in a sealed channel, or in a channel its agent is pinned to,
    records that channel (`daimon.core.mcp_auth.coding_token_channel`), and
    its calls count as a turn there: the pin, the seal, the environment, the
    channel budget and usage attribution all see it. Every other caller (an
    unbound key, a chat turn, the hub, a CLI token) runs outside every channel.
    """
    return auth.bound_channel_id if auth.agent_id is not None else None


def resolve_role(role_str: str | None) -> Role:
    """Map the role claim string to a Role enum. Unknown/missing defaults to USER."""
    if role_str == Role.ADMIN.value:
        return Role.ADMIN
    return Role.USER

"""Originating-context scope for an MA session's vault JWT.

Sibling module of ``daimon.core.sessions``. The class lives here so
``daimon.core.mcp_vault`` can import it without creating a cycle
(``mcp_vault → sessions → mcp_vault``). ``daimon.core.sessions`` re-exports
``SessionContext`` so adapters can keep importing it from there.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class SessionContext:
    """Originating-context scope for an MA session's vault JWT.

    Built by the adapter that creates the session (Discord bot, CLI sessions
    command) and threaded through ``create_session`` → ``ensure_mcp_vault`` →
    ``mint_jwt`` so the resulting vault credential JWT carries the
    ``is_admin`` claim. Identity (tenant/platform) is resolved server-side by
    the verifier from the accounts→tenants JOIN — no platform/guild wire claims.

    ``is_admin`` is derived by the adapter from the invoking member's Discord
    permissions (owner | manage_guild | administrator) or CLI context, and
    threaded into the minted JWT as the ``"is_admin"`` claim.
    """

    is_admin: bool

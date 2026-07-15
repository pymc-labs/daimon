"""Typed accessor for per-request AuthIdentity set by IdentityMiddleware."""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from fastmcp import Context
from fastmcp.exceptions import ToolError


async def _auth(ctx: Context) -> AuthIdentity:  # pyright: ignore[reportUnusedFunction]
    """Return the AuthIdentity seeded into request state by IdentityMiddleware.

    Raises ToolError if the state is missing — this is a programming error
    (middleware failed to run), not a caller-facing condition.
    """
    identity = await ctx.get_state("auth")
    if not isinstance(identity, AuthIdentity):
        raise ToolError("internal: missing auth context")
    return identity


def _require_admin(auth: AuthIdentity) -> None:  # pyright: ignore[reportUnusedFunction]
    """Raise ToolError with the D-28 message if the caller is not an admin.

    Call at the top of every mutating _*_impl to enforce RBAC-02 chat gating.
    Reads (list_*/get_*/self_read*/self_list*) stay ungated.
    """
    if not auth.is_admin:
        raise ToolError(
            "Changing my setup needs Manage Server — ask a server admin to use /agent-setup"
        )

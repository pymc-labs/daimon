"""Explicit Gemini native endpoint selection; authorization is shared."""

from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.native_credential_requests import (
    GrantFactory as GrantFactory,
)
from daimon.adapters.mcp.tools.native_credential_requests import (
    NativeCredentialGrant,
    register_native_credential_tools,
)
from fastmcp import FastMCP
from mux.profiles.gemini import INLINE_REUSE

GeminiCredentialGrant = NativeCredentialGrant


async def register_gemini_credential_tools(
    mcp: FastMCP,
    *,
    operator_enabled: bool = False,
    grants: GrantFactory | None = None,
) -> tuple[str, ...]:
    """Select only this explicitly supplied native endpoint, disabled by default."""
    return await register_native_credential_tools(
        mcp,
        profile=INLINE_REUSE,
        auth_resolver=_auth,
        operator_enabled=operator_enabled,
        grants=grants,
    )

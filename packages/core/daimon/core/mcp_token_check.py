"""The token forms' pre-save check, shared by the Discord and Slack submit paths.

`check_token` turns an injectable probe into a verdict the form can act on:
only a definite 401/403 refuses the save, and the refusal carries whether the
server also offers browser sign-in (it advertised OAuth resource metadata in
its `WWW-Authenticate` header, RFC 9728). A probe that raises (network,
timeout, TLS) is logged and answered False — an unreachable server is not a
verdict on the token, and MA will report it on the first turn instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog
from daimon.core.mcp_oauth.discovery import McpTokenProbe
from daimon.core.mcp_oauth.urls import McpUrlError

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenRejection:
    """A server's definite no to a pasted token."""

    #: The server advertised OAuth resource metadata, so it signs people in
    #: through a browser and the sign-in card would work where a token did not.
    supports_sign_in: bool


async def check_token(
    probe: McpTokenProbe | None, *, mcp_server_url: str, token: str
) -> TokenRejection | None:
    """The rejection when the server refused `token`, else None."""
    if probe is None:
        return None
    try:
        result = await probe(mcp_server_url, token)
    except (httpx.HTTPError, McpUrlError) as err:
        # Boundary by design: see the module docstring. A URL the probe will
        # not contact (http, private address) is left to MA, as before.
        log.warning(
            "mcp_token_check.probe_failed",
            mcp_server_url=mcp_server_url,
            err_type=type(err).__name__,
        )
        return None
    if not result.rejects_credentials:
        return None
    supports_sign_in = result.resource_metadata_url is not None
    log.info(
        "mcp_token_check.rejected",
        mcp_server_url=mcp_server_url,
        status_code=result.status_code,
        advertises_oauth=supports_sign_in,
    )
    return TokenRejection(supports_sign_in=supports_sign_in)


async def is_token_rejected(
    probe: McpTokenProbe | None, *, mcp_server_url: str, token: str
) -> bool:
    """Whether the server refused `token`; `check_token` without the detail."""
    return await check_token(probe, mcp_server_url=mcp_server_url, token=token) is not None


def rejected_token_message(rejection: TokenRejection) -> str:
    """The ephemeral for the person who pasted a token the server refused.

    The form stays usable after a rejection (`credential_submit` releases the
    request for a retry), so the way out is the same form, not a new one. The
    sign-in line appears only for a server that advertised browser sign-in;
    for any other server it would send people after a card that cannot work.
    """
    lines = [
        "That token didn't work. Nothing was saved.",
        "Check the token and try the form again.",
    ]
    if rejection.supports_sign_in:
        lines.append("This server also lets you sign in. Ask the agent to connect your account.")
    return "\n\n".join(lines)

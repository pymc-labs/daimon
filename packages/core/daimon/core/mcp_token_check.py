"""The token forms' pre-save check, shared by the Discord and Slack submit paths.

`is_token_rejected` turns an injectable probe into a yes/no the form can act
on: only a definite 401/403 refuses the save. A probe that raises (network,
timeout, TLS) is logged and answered False — an unreachable server is not a
verdict on the token, and MA will report it on the first turn instead.
"""

from __future__ import annotations

import httpx
import structlog
from daimon.core.mcp_oauth.discovery import McpTokenProbe
from daimon.core.mcp_oauth.urls import McpUrlError

log = structlog.get_logger(__name__)


async def is_token_rejected(
    probe: McpTokenProbe | None, *, mcp_server_url: str, token: str
) -> bool:
    if probe is None:
        return False
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
        return False
    if result.rejects_credentials:
        log.info(
            "mcp_token_check.rejected",
            mcp_server_url=mcp_server_url,
            status_code=result.status_code,
            advertises_oauth=result.resource_metadata_url is not None,
        )
    return result.rejects_credentials


def rejected_token_message(mcp_server_url: str) -> str:
    """The ephemeral for the person who pasted a token the server refused."""
    return (
        f"`{mcp_server_url}` did not accept that token, so nothing was saved. "
        "If this server signs people in through a browser (Notion does), ask the agent "
        "to connect it with your account instead; otherwise check the token and ask again."
    )

"""get_cli_token MCP tool.

The agent calls this tool from inside its MA sandbox. Identity comes
from the JWT middleware: ``auth.account_id`` (always populated) and
``auth.agent_id`` (populated when the JWT was minted for an agent
session). For Google only, ordinary chat supplies ``auth.chat_agent_id``.
The tool dispatches to the broker, audit-logs metadata, and returns the token.

The CLI never calls this tool. (The former ``daimon auth github`` OAuth
flow and its ``/oauth/github/*`` + ``/cli/auth/status`` routes were removed
— repo credentials now come from the GitHub App or a bound PAT.)

Audit log invariant (T-19-04-02): the token plaintext NEVER appears in
any log line emitted by this module. The combined-log integration test
(``tests/test_audit_log_no_token.py``) asserts this across both the
broker and tool layers using a sentinel token.
"""

from __future__ import annotations

from typing import Literal

import structlog
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.broker import dispatch_mint_token
from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

logger = structlog.get_logger()


async def _get_cli_token_impl(
    runtime: McpRuntime,
    ctx: Context,
    *,
    service: Literal["github", "gcloud"],
) -> str:
    """Dispatch to the broker for ``service``; map BrokerError → ToolError.

    Reads identity server-side via ``await ctx.get_state("auth")`` (T-19-04-01:
    no tool-supplied agent_id; confused-deputy by construction).
    """
    auth = await _auth(ctx)
    agent_id = auth.agent_id or (auth.chat_agent_id if service == "gcloud" else None)
    try:
        # Only Google uses chat execution identity. GitHub chat callers retain
        # their account principal-default PAT; operator defaults are never returned.
        token = await dispatch_mint_token(
            service=service,
            account_id=auth.account_id,
            agent_id=agent_id,
            sessionmaker=runtime.session_factory,
            settings=runtime.settings,
            allow_service_default=False,
        )
    except NoBindingError as e:
        logger.warning(
            "cli_token outcome=no_binding service=%s account=%s agent=%s",
            service,
            auth.account_id,
            agent_id,
        )
        raise ToolError(str(e)) from e
    except ProviderConfigError as e:
        logger.warning(
            "cli_token outcome=provider_config_error service=%s account=%s",
            service,
            auth.account_id,
        )
        raise ToolError(str(e)) from e
    logger.info(
        "cli_token outcome=success service=%s account=%s agent=%s",
        service,
        auth.account_id,
        agent_id,
    )
    return token


def register_cli_token_tool(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def get_cli_token(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        service: Literal["github", "gcloud"],
    ) -> str:
        """Mint a short-lived CLI access token for the named service.

        Fails with a ``ToolError`` when the calling agent has no credential
        bound for that service — see the ``cli-auth`` skill for the
        per-agent binding flow. On success, returns the token as plaintext
        (e.g. for ``export GH_TOKEN=$(...)``).
        """
        return await _get_cli_token_impl(runtime, ctx, service=service)

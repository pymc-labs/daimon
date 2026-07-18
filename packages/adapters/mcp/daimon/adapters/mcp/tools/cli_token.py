"""get_cli_token MCP tool.

The agent calls this tool from inside its MA sandbox. Identity comes
from the JWT middleware: ``auth.account_id`` (always populated) and
``auth.agent_id`` (populated when the JWT was minted for an agent
session — the tool dispatches to the
broker, audit-logs metadata, and returns the plaintext token.

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
    try:
        token = await dispatch_mint_token(
            service=service,
            account_id=auth.account_id,
            agent_id=auth.agent_id,
            sessionmaker=runtime.session_factory,
            settings=runtime.settings,
        )
    except NoBindingError as e:
        logger.warning(
            "cli_token outcome=no_binding service=%s account=%s agent=%s",
            service,
            auth.account_id,
            auth.agent_id,
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
        auth.agent_id,
    )
    return token


def register_cli_token_tool(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def get_cli_token(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        service: Literal["github", "gcloud"],
    ) -> str:
        """Mint a short-lived CLI access token for the named service.

        Returns the token as plaintext (e.g. for ``export GH_TOKEN=$(...)``).
        See the ``cli-auth`` skill for env-var mappings.
        """
        return await _get_cli_token_impl(runtime, ctx, service=service)

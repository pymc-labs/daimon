"""Current-place status tool, sharing the slash commands' fixed card."""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._isolation import load_caller_isolation
from daimon.adapters.mcp.tools.discord._read import (
    _list_channels_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._read import (
    _slack_list_channels_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.agent_details import GitHubDeploymentFacts
from daimon.core.here_card import HereCard, load_here_card
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def _where_am_i_impl(
    runtime: McpRuntime, auth: AuthIdentity, channel_id: str | None, thread_id: str | None
) -> HereCard:
    channel_id = channel_id or token_channel_id(auth)
    if channel_id is None:
        raise ToolError("The current channel is unknown; pass its parent channel id.")
    caller = await load_caller_isolation(runtime, auth)
    if caller.isolated_place(channel_id) != caller.inside_channel_id:
        raise ToolError("That channel is across an isolated channel's line.")
    category: tuple[tuple[str, str], ...] = ()
    if auth.platform == "discord":
        rows = await _list_channels_impl(runtime, auth)
        visible = {row.id for row in rows}
        if channel_id not in visible:
            raise ToolError("missing channel access")
        here = next(row for row in rows if row.id == channel_id)
        category = tuple(
            (row.id, f"#{row.name}: yes")
            for row in rows
            if row.id != channel_id
            and here.category_id is not None
            and row.category_id == here.category_id
        )
    elif auth.platform == "slack":
        visible = {row.id for row in await _slack_list_channels_impl(runtime, auth)}
        if channel_id not in visible:
            raise ToolError("missing channel access")
    else:
        raise ToolError("/here is available for Discord and Slack channel turns")
    github = runtime.settings.github
    async with runtime.session_factory() as session:
        return await load_here_card(
            session,
            runtime.client,
            tenant_id=auth.tenant_id,
            platform=auth.platform,
            channel_id=channel_id,
            thread_id=thread_id,
            default=runtime.deployment_default,
            github=GitHubDeploymentFacts(
                has_fallback_pat=github.fallback_pat is not None,
                app_configured=github.app_id is not None and github.app_private_key is not None,
            ),
            public_mcp_url=str(runtime.settings.mcp.public_url)
            if runtime.settings.mcp.public_url is not None
            else None,
            is_admin=auth.is_admin,
            caller_account_id=auth.account_id,
            visible_channel_ids=visible,
            bot_can_view=True,
            caller_can_view=True,
            category_channels_bot_can_view=category,
        )


def register_here_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def where_am_i(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, channel_id: str | None = None, thread_id: str | None = None
    ) -> HereCard:
        """Return the fixed /here card with structured facts and rendered text.
        Use this when someone asks which agent you are, who answers here, whether
        you can see a channel, or whose token or sign-in you hold. Never guess
        these facts from the conversation. Pass the parent channel id from turn
        controls and the current thread id when present. A bound coding token
        can omit channel_id. Discord and Slack channel turns only. Unknown
        visibility is reported as unknown.
        """
        return await _where_am_i_impl(runtime, await _auth(ctx), channel_id, thread_id)

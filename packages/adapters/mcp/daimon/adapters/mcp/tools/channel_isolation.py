"""Channel isolation tool: keep a channel's own agents inside it.

``register_channel_isolation_tools(mcp, runtime)`` wires the ``@mcp.tool``
closure; it delegates to ``_set_channel_isolation_impl``, which tests call
without a FastMCP Context. Isolation is a tenant-wide change, so server
admins only. The rules live in ``daimon.core.channel_isolation_setup``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.slack._client import (
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.core.agent_fork import fork_agent
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_isolation_setup import ForkAgent, set_channel_isolation
from daimon.core.errors import DaimonError
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError

_NOTE = (
    "While a channel is isolated its own agents answer only there and are hidden everywhere "
    "else, and from inside it only they are visible. Its messages are readable only from "
    "inside it; memory stays writable."
)


@dataclass(frozen=True)
class SetChannelIsolationResult:
    """Result returned from set_channel_isolation."""

    channel_id: str
    isolated: bool
    agent_name: str | None
    """The channel's own agent after the change; None once isolation ended."""
    forked_from: str | None
    """The agent copied to make it, when this call made one."""
    changed: bool
    note: str


async def _channel_label(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str | None:
    """The channel's name, to name a copied agent after; None when it can't be read."""
    try:
        if auth.platform == "discord":
            guild_id = _require_guild_id(auth)
            async with rest_client(_require_bot_token(runtime)) as client:
                channel = await client.fetch_channel(int(channel_id))
            if isinstance(channel, discord.abc.GuildChannel) and str(channel.guild.id) == guild_id:
                return channel.name
            return None
        client = await slack_web_client(runtime, team_id=_require_team_id(auth))
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        name = cast("dict[str, object]", info["channel"]).get("name")
        return name if isinstance(name, str) else None
    except (ToolError, discord.HTTPException, SlackApiError, ValueError):
        return None


async def _set_channel_isolation_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    isolated: bool,
    fork_from: str | None = None,
) -> SetChannelIsolationResult:
    _require_admin(auth)
    if auth.platform not in ("discord", "slack"):
        raise ToolError("Channel isolation exists only on Discord and Slack.")
    try:
        channel, _, _ = normalize_channel_admin_ids(
            auth.platform, channel_id=channel_id, role_ids=(), user_ids=()
        )
    except InvalidChannelAdminIds as exc:
        raise ToolError(f"{exc}. Nothing was changed.") from exc
    fork: ForkAgent | None = None
    label: str | None = None
    if isolated and fork_from is not None:
        public_url = runtime.settings.mcp.public_url

        async def fork_copy(source: str, new_name: str) -> None:
            await fork_agent(
                runtime.client,
                runtime.session_factory,
                tenant_id=auth.tenant_id,
                source_name=source,
                new_name=new_name,
                public_url=str(public_url) if public_url is not None else None,
            )

        fork = fork_copy
        label = await _channel_label(runtime, auth, channel)
    try:
        change = await set_channel_isolation(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            channel_id=channel,
            isolated=isolated,
            default=runtime.deployment_default,
            actor_account_id=auth.account_id,
            channel_label=label,
            fork=fork,
            fork_from=fork_from,
        )
    except DaimonError as exc:
        raise ToolError(f"{exc} Nothing was changed.") from exc
    return SetChannelIsolationResult(
        channel_id=change.channel_id,
        isolated=change.isolated,
        agent_name=change.agent_name,
        forked_from=change.forked_from,
        changed=change.changed,
        note=_NOTE if change.isolated else "The channel is open again.",
    )


def register_channel_isolation_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def set_channel_isolation(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        isolated: bool,
        fork_from: str | None = None,
    ) -> SetChannelIsolationResult:
        """Isolate one channel, or end its isolation. For example, give #team-alpha an
        agent nobody outside it can see or reach. Requires Manage Server (admin).

        An isolated channel needs an agent of its own: its default agent, answering
        nowhere else and not built in. If it has none, pass ``fork_from`` (an agent
        name, usually the one answering there now): that agent is copied under a name
        taken from the channel and becomes the channel's default. The copy carries no
        credentials, and an agent pinned to channels can't be copied. Without it the call
        is refused and says why. Repeating the call copies nothing again.

        While isolated, the channel's own agents can't be set as the default anywhere
        else, don't appear in agent, skill or routine lists outside it, and can't be
        handed tasks from elsewhere; inside it only they appear. Its messages are
        readable only from inside it. ``channel_id`` MUST be the parent channel's id,
        never a thread's.
        """
        return await _set_channel_isolation_impl(
            runtime, await _auth(ctx), channel_id=channel_id, isolated=isolated, fork_from=fork_from
        )

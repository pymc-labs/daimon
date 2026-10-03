"""Rule tools: who reads a channel and who posts there, and where an agent runs.

``register_channel_rule_tools(mcp, runtime)`` wires the ``@mcp.tool``
closures; they delegate to ``_set_channel_rule_impl`` and
``_set_agent_rule_impl``, which tests call without a FastMCP Context.
`authorize(SET_CHANNEL_RULE)` and `SET_AGENT_RULE` decide who may
(``daimon.core.channel_rules``): server admins and operator tokens with
``channels:write`` only, never a channel's admins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import discord
import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import parse_channel_target
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.slack._client import (
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.teams._directory import locate_channel, require_client
from daimon.core.access_policy import ChannelReaders, ChannelWriters
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL
from daimon.core.authz import Action
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_rules import ChannelRuleRefused, set_agent_rule, set_channel_rule
from daimon.core.errors import DaimonError
from daimon.core.permissions import RuleRefused, thread_rule_key
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError


@dataclass(frozen=True)
class SetChannelRuleResult:
    """Result returned from set_channel_rule."""

    channel_id: str
    readers: ChannelReaders
    writers: ChannelWriters
    own_agent: str | None
    """The agent this call made the channel's own."""
    copied_from: str | None
    """The agent copied to make it, when this call made one."""
    released_agents: list[str]
    changed: bool
    note: str


@dataclass(frozen=True)
class SetAgentRuleResult:
    """Result returned from set_agent_rule."""

    agent_name: str
    runs_in: list[str] | None
    changed: bool
    note: str


def _platform(auth: AuthIdentity) -> str:
    if auth.platform not in ("discord", "slack", "teams"):
        raise ToolError("Rules exist only on Discord, Slack and Teams.")
    return auth.platform


def _rule_target(auth: AuthIdentity, channel_id: str) -> str:
    """A channel, or a Slack thread (`channel:ts`), which keeps a rule of its own."""
    try:
        thread = thread_rule_key(_platform(auth), channel_id)
    except RuleRefused as exc:
        raise ToolError(f"{exc}. Nothing was changed.") from exc
    return thread or _channel(auth, channel_id)


def _channel(auth: AuthIdentity, channel_id: str) -> str:
    platform = _platform(auth)
    try:
        channel, _, _ = normalize_channel_admin_ids(
            platform,
            channel_id=parse_channel_target(platform, channel_id).channel_id,
            role_ids=(),
            user_ids=(),
        )
    except InvalidChannelAdminIds as exc:
        raise ToolError(f"{exc}. Nothing was changed.") from exc
    return channel


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
        if auth.platform == "teams":
            client, _ = require_client(runtime, auth)
            return (await locate_channel(runtime, auth, client, channel_id)).channel_name
        client = await slack_web_client(runtime, team_id=_require_team_id(auth))
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        name = cast("dict[str, object]", info["channel"]).get("name")
        return name if isinstance(name, str) else None
    except (ToolError, discord.HTTPException, SlackApiError, httpx.HTTPError, ValueError):
        return None


def _refused(exc: DaimonError, action: Action) -> ToolError:
    if isinstance(exc, ChannelRuleRefused) and exc.reason == "admin_required":
        record_authz_denial(action, exc.reason)
    return ToolError(f"{exc} Nothing was changed. Tell the caller.")


async def _set_channel_rule_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    readers: ChannelReaders | None = None,
    writers: ChannelWriters | None = None,
    copy_from: str | None = None,
    release_agents: bool = False,
) -> SetChannelRuleResult:
    require_scope(auth, "channels:write")
    channel = _rule_target(auth, channel_id)
    if readers is None and writers is None and not release_agents:
        raise ToolError("Pass readers, writers or release_agents. Nothing was changed.")
    label = await _channel_label(runtime, auth, channel) if copy_from is not None else None
    public_url = runtime.settings.mcp.public_url
    try:
        change = await set_channel_rule(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=_platform(auth),
            channel_id=channel,
            readers=readers,
            writers=writers,
            subject=mcp_subject(auth, is_admin=auth.is_admin),
            default=runtime.deployment_default,
            actor_account_id=auth.account_id,
            copy=copy_from is not None,
            copy_from=copy_from,
            channel_label=label,
            public_url=str(public_url) if public_url is not None else None,
            release_agents=release_agents,
        )
    except AccessPolicyUnreadable as exc:
        raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    except DaimonError as exc:
        raise _refused(exc, Action.SET_CHANNEL_RULE) from exc
    return SetChannelRuleResult(
        channel_id=change.channel_id,
        readers=change.rule.readers,
        writers=change.rule.writers,
        own_agent=change.agent_name,
        copied_from=change.copied_from,
        released_agents=list(change.released),
        changed=change.changed,
        note=" ".join(change.notes),
    )


async def _set_agent_rule_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    runs_in: list[str] | None,
) -> SetAgentRuleResult:
    require_scope(auth, "channels:write")
    channels = [_channel(auth, channel) for channel in runs_in] if runs_in is not None else None
    try:
        change = await set_agent_rule(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=_platform(auth),
            agent_name=agent_name.strip(),
            runs_in=channels,
            subject=mcp_subject(auth, is_admin=auth.is_admin),
            default=runtime.deployment_default,
        )
    except AccessPolicyUnreadable as exc:
        raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    except DaimonError as exc:
        raise _refused(exc, Action.SET_AGENT_RULE) from exc
    return SetAgentRuleResult(
        agent_name=change.agent_name,
        runs_in=list(change.runs_in) if change.runs_in is not None else None,
        changed=change.changed,
        note=" ".join(change.notes),
    )


def register_channel_rule_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def set_channel_rule(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        readers: ChannelReaders | None = None,
        writers: ChannelWriters | None = None,
        copy_from: str | None = None,
        release_agents: bool = False,
    ) -> SetChannelRuleResult:
        """Set who can read a channel and who can post there. Requires a server or
        workspace admin. Leave one out to keep it.

        ``readers``: ``any``; ``inside``, only turns in this channel read its messages
        and conversations; or ``own``, which also keeps it to its own agents: only they
        run and show there, and they run and show nowhere else. ``writers``: ``any``;
        ``own`` (only with readers ``own``); or ``none``, nothing posts there, daimon
        included.

        To isolate a channel so nothing leaks in or out, set readers and writers
        ``own``. Its default agent becomes its own agent, by an agent rule naming the
        channel alone; it must be custom and answer nowhere else. Otherwise pass
        ``copy_from`` (an agent name, usually the one answering there) to copy that
        agent, without credentials, as the channel's default. To make agent X the only
        agent in a channel, set the channel's default to X first. ``release_agents``
        drops the rules of the agents kept to the channel, once readers aren't ``own``.
        ``channel_id`` is the channel's id, or a Slack thread's ``channel:ts`` (readers only);
        a Teams thread id names its channel.
        """
        return await _set_channel_rule_impl(
            runtime,
            await _auth(ctx),
            channel_id=channel_id,
            readers=readers,
            writers=writers,
            copy_from=copy_from,
            release_agents=release_agents,
        )

    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def set_agent_rule(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, agent_name: str, runs_in: list[str] | None
    ) -> SetAgentRuleResult:
        """Set where an agent runs: only in the ``runs_in`` channels and their threads,
        or, with null, wherever it is set to answer; an empty list runs it nowhere.
        Requires a server or workspace admin.

        Outside its rule its turns are refused. An agent with a rule messages only
        whoever asked, asks before publishing, and can't be copied. A channel's own
        agent keeps its rule until that channel's readers change (``set_channel_rule``).
        Naming a channel only its own agents read makes the agent one of them, so name
        that channel alone.
        """
        return await _set_agent_rule_impl(
            runtime, await _auth(ctx), agent_name=agent_name, runs_in=runs_in
        )

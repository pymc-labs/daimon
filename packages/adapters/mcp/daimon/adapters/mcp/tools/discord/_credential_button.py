"""Post the credential-request button to a Discord channel.

Posted from the MCP process (Cloud Run); dispatched later by the Discord bot
process (worker VM) via `CredentialRequestButton`, a `discord.ui.DynamicItem`
matching `CUSTOM_ID_TEMPLATE`. The two processes cannot import each other
(import-linter's independence contract), so this module imports only the
core wire-contract builders (`build_custom_id`, `build_button_label`) — never
the Discord adapter's dynamic-item class, and never a re-derived prefix or
label format. A divergent copy here would silently stop the button from
dispatching in the other process.

Reuses `_send_message_impl`'s exact require/resolve/permission-check order
(`tools/discord/_send.py`) rather than re-implementing any of it, so posting
a credential button carries the same channel-visibility discipline as
`send_message`.
"""

from __future__ import annotations

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_channel,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    _resolve_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.credential_requests import (
    CredentialRequestKind,
    build_button_label,
    build_custom_id,
)
from fastmcp.exceptions import ToolError

_KIND_NOUN: dict[CredentialRequestKind, str] = {
    "env": "an environment secret",
    "mcp": "an MCP server credential",
    "repo": "a repo binding",
    "skill_repo": "a GitHub token to import skills",
}


def _build_message_body(
    *,
    requester_platform_user_id: str,
    agent_name: str,
    kind: CredentialRequestKind,
    target: str,
    purpose: str,
) -> str:
    if kind == "repo":
        return (
            f"<@{requester_platform_user_id}> wants to bind a repo to **{agent_name}** "
            f"(`{target}`) — {purpose}\n"
            "Click the button below to open a private form confirming the branch, "
            "and — only if the repo isn't publicly readable — a GitHub token that "
            "never appears in this channel."
        )
    return (
        f"<@{requester_platform_user_id}> **{agent_name}** needs {_KIND_NOUN[kind]} "
        f"(`{target}`) — {purpose}\n"
        "Click the button below to enter the value privately in a Discord modal; "
        "it will never appear in this channel.\n"
        f"Once added, this credential becomes usable by everyone who talks to "
        f"**{agent_name}**."
    )


async def _post_credential_button_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    kind: CredentialRequestKind,
    target: str,
    token: str,
    agent_name: str,
    purpose: str,
) -> str:
    """Post a target-naming credential button. Returns the sent message id."""
    requester_id = _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    bot_token = _require_bot_token(runtime)

    button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
        style=discord.ButtonStyle.primary,
        label=build_button_label(kind, target),
        custom_id=build_custom_id(token),
    )
    view: discord.ui.View = discord.ui.View(timeout=None)
    view.add_item(button)

    content = _build_message_body(
        requester_platform_user_id=requester_id,
        agent_name=agent_name,
        kind=kind,
        target=target,
        purpose=purpose,
    )

    async with rest_client(bot_token) as c:
        _, member = await _resolve_member(c, guild_id, requester_id)
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)
        if isinstance(channel, discord.Thread):
            # Thread.permissions_for needs the parent in the guild cache;
            # the per-call REST client starts with an empty one.
            await _ensure_thread_parent_cached(channel)
        _check_send_permission(channel, member)
        if not isinstance(channel, discord.abc.Messageable):
            raise ToolError("channel does not support sending messages")
        sent = await channel.send(
            content=content,
            view=view,
            allowed_mentions=discord.AllowedMentions(
                users=True, everyone=False, roles=False, replied_user=False
            ),
        )
        return str(sent.id)

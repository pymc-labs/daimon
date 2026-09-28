"""Post the GitHub App install-page link as a Discord link button.

Posted from the MCP process (Cloud Run). Unlike the credential button
(`_credential_button.py`), this button carries no custom id and is never
dispatched by the Discord bot process (worker VM) — Discord opens the ``url``
directly when the button is clicked. That is the load-bearing difference
from the credential button: it is what stops a future reader from "completing"
this by adding a bot-process handler. Nothing here creates a request row, a
minted token, an expiry, or a dynamic item, and nothing should.

Reuses `_send_message_impl`'s exact require/resolve/permission-check order
(`tools/discord/_send.py`) rather than re-implementing any of it, so posting
this button carries the same channel-visibility discipline as `send_message`.

Imports the install-URL builder from `daimon.core.github_app_auth` — that is
the single place the URL is constructed. This module defines no URL template
and no local builder.
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
    _require_discord_channel_writable,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.github_app_auth import build_app_install_url
from fastmcp.exceptions import ToolError


def _build_message_body(*, requester_platform_user_id: str, purpose: str) -> str:
    return (
        f"<@{requester_platform_user_id}> wants to reach a private GitHub repository "
        f"({purpose}) — installing the GitHub App grants read access to the "
        "repositories you choose, so skill sync and repo cloning can reach them "
        "without anyone pasting a token.\n"
        "Click the button below to open the install page on GitHub."
    )


async def _post_app_install_button_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    slug: str,
    purpose: str,
) -> str:
    """Post the App install link as a Discord link button. Returns the sent message id."""
    requester_id = _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    bot_token = _require_bot_token(runtime)

    url = build_app_install_url(slug)
    button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
        style=discord.ButtonStyle.link,
        url=url,
    )
    view: discord.ui.View = discord.ui.View(timeout=None)
    view.add_item(button)

    content = _build_message_body(requester_platform_user_id=requester_id, purpose=purpose)

    async with rest_client(bot_token) as c:
        _, member = await _resolve_member(c, guild_id, requester_id)
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)
        if isinstance(channel, discord.Thread):
            # Thread.permissions_for needs the parent in the guild cache;
            # the per-call REST client starts with an empty one.
            await _ensure_thread_parent_cached(channel)
        _check_send_permission(channel, member)
        await _require_discord_channel_writable(runtime, auth, channel)
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

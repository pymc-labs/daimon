"""Plain-text mention tools: let a typed "@<bot name>" start a turn, workspace-wide.

On Teams mobile a real mention takes typing part of the name and tapping the
suggestion, and a pasted prompt that says "@Daimon" is only text. With this
switch on, the Discord, Slack and Teams adapters treat such text in a channel
or thread like a real mention (``daimon.core.name_mentions``).

``set_plain_text_mentions`` changes the whole tenant, so it is admin-tagged and
gated. ``get_plain_text_mentions`` stays readable by anyone, like
``get_thread_participation``, so a member who asks hears the answer and who
can change it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import StoreError
from daimon.core.stores.tenants import get_plain_text_mentions, set_plain_text_mentions
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_CHAT_PLATFORMS: Final = frozenset({"discord", "slack", "teams"})
_WRONG_PLATFORM = "Plain-text mentions aren't available here; nothing was changed."


@dataclass(frozen=True)
class PlainTextMentionsStatus:
    """Whether a typed "@<bot name>" starts a turn in this workspace."""

    enabled: bool
    note: str
    """One sentence to read back to the user."""


def _note(enabled: bool) -> str:
    if enabled:
        return (
            'Typing "@" followed by my name as plain text in a channel or thread now '
            "starts a reply, the same as picking me from the mention list. Direct "
            "messages are unchanged."
        )
    return (
        "Only a real mention (picking me from the mention list) or a direct message "
        'starts a reply; my name typed as plain text after "@" does not.'
    )


async def _set_plain_text_mentions_impl(
    runtime: McpRuntime, auth: AuthIdentity, enabled: bool
) -> PlainTextMentionsStatus:
    _require_admin(auth, action=DelegatedAction.CHANNEL_PERMISSIONS)
    if auth.platform not in _CHAT_PLATFORMS:
        raise ToolError(_WRONG_PLATFORM)
    async with runtime.session_factory.begin() as session:
        try:
            await set_plain_text_mentions(session, tenant_id=auth.tenant_id, enabled=enabled)
        except StoreError as exc:
            raise ToolError("internal: this chat space no longer exists") from exc
    return PlainTextMentionsStatus(enabled=enabled, note=_note(enabled))


async def _get_plain_text_mentions_impl(
    runtime: McpRuntime, auth: AuthIdentity
) -> PlainTextMentionsStatus:
    enabled = await get_plain_text_mentions(runtime.session_factory, tenant_id=auth.tenant_id)
    return PlainTextMentionsStatus(enabled=enabled, note=_note(enabled))


def register_plain_text_mentions_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def set_plain_text_mentions(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, enabled: bool
    ) -> PlainTextMentionsStatus:
        """Let a typed "@<your name>" in a channel or thread start a reply. Admin-only.

        For the whole workspace. When
        ``enabled`` is true, a person's message in a channel or thread that
        contains your name after "@" as plain text (any case, for example a
        pasted prompt or a mention typed on a phone without picking you from
        the list) is answered as if they had mentioned you. Messages from
        bots and webhooks never count, and direct messages are unchanged. So
        that suffixes and domains never match, a name followed by "(" or by a
        dot and a letter is ignored ("@Daimon (urgent)", "@Daimon.Thanks"). Off
        by default. Read the returned ``note`` back to the user.
        """
        return await _set_plain_text_mentions_impl(runtime, await _auth(ctx), enabled)

    @mcp.tool
    async def get_plain_text_mentions(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> PlainTextMentionsStatus:
        """Report whether a typed "@<your name>" in a channel or thread starts a reply here.

        Reading needs no admin; changing it does (``set_plain_text_mentions``,
        an admin). Read the returned ``note`` back to the
        user.
        """
        return await _get_plain_text_mentions_impl(runtime, await _auth(ctx))

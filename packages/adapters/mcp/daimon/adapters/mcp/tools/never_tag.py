"""Never-tag tool: a person asks that Daimon never pings them, on any platform.

The setting is the caller's own and nothing else: the tool takes no person
argument and acts on the account the call runs for. Once set, every mention of
the person in anything Daimon posts becomes a plain ``@Name`` and nobody is
notified (`daimon.core.outbound`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.stores import tag_opt_outs as store
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_CHAT_PLATFORMS: Final = frozenset({"discord", "slack", "teams"})
_PERSON_ONLY = "Only a person in a chat can change this for themselves; nothing was changed."
_SET_NOTE = "Done. Daimon will not tag you; your name shows as plain text instead."
_CLEARED_NOTE = "Done. Daimon may tag you again when it is useful."


@dataclass(frozen=True)
class NeverTagStatus:
    """Whether Daimon may tag the person who asked."""

    never_tag: bool
    note: str
    """One sentence to read back to the user."""


async def _set_never_tag_impl(
    runtime: McpRuntime, auth: AuthIdentity, never_tag: bool
) -> NeverTagStatus:
    if auth.platform not in _CHAT_PLATFORMS or not auth.platform_user_id or auth.routine_id:
        raise ToolError(_PERSON_ONLY)
    async with runtime.session_factory.begin() as session:
        if never_tag:
            await store.set_opt_out(session, account_id=auth.account_id, tenant_id=auth.tenant_id)
        else:
            await store.clear_opt_out(session, account_id=auth.account_id)
    return NeverTagStatus(never_tag=never_tag, note=_SET_NOTE if never_tag else _CLEARED_NOTE)


def register_never_tag_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def set_never_tag(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, never_tag: bool = True
    ) -> NeverTagStatus:
        """Stop or resume Daimon tagging the person who is asking, only for themselves.

        Call it when the person asks never to be tagged, pinged or @-mentioned
        (``never_tag`` true), or asks to be tagged again (false). It always acts
        for whoever sent this request and takes no one else's name; if someone asks
        for it on another person's behalf, decline and tell them that person
        can ask for themselves. It holds across every platform the person uses
        Daimon on. Read the returned ``note`` back to the user.
        """
        return await _set_never_tag_impl(runtime, await _auth(ctx), never_tag)

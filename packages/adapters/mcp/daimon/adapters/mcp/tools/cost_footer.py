"""Cost footer tools: show or hide the line under each finished answer, workspace-wide.

The line names the agent, the time taken, what the turn cost and the money
left. A workspace that does not want its members to see spend hides it; the
answer is unchanged and the billing panel still shows usage and credit.

Both are deprecated in favour of the Settings group (``change_setting`` with
``key="cost_footer"`` and ``get_settings``) and are removed in a later release.
``set_cost_footer`` writes through the same writer, so the change is recorded.
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
from daimon.adapters.mcp.tools.scope_settings import settings_actor
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.cost_footer import cost_footer_note
from daimon.core.errors import StoreError
from daimon.core.scope_settings import SettingsPlace, change_setting, read_settings
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_CHAT_PLATFORMS: Final = frozenset({"discord", "slack", "teams"})
_WRONG_PLATFORM = "The cost footer isn't available here; nothing was changed."


@dataclass(frozen=True)
class CostFooterStatus:
    """Whether finished answers in this workspace show their cost footer."""

    shown: bool
    note: str
    """One sentence to read back to the user."""


async def _set_cost_footer_impl(
    runtime: McpRuntime, auth: AuthIdentity, shown: bool
) -> CostFooterStatus:
    _require_admin(auth, action=DelegatedAction.AGENT_SETTINGS)
    if auth.platform not in _CHAT_PLATFORMS:
        raise ToolError(_WRONG_PLATFORM)
    try:
        await change_setting(
            runtime.session_factory,
            runtime.settings,
            settings_actor(auth),
            key="cost_footer",
            level="server",
            value="shown" if shown else "hidden",
            place=SettingsPlace(auth.platform or "", None),
        )
    except StoreError as exc:
        raise ToolError("internal: this chat space no longer exists") from exc
    except ChannelChangeRefused as exc:
        raise ToolError(str(exc)) from exc
    return CostFooterStatus(shown=shown, note=cost_footer_note(shown))


async def _get_cost_footer_impl(runtime: McpRuntime, auth: AuthIdentity) -> CostFooterStatus:
    (view,) = await read_settings(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        place=SettingsPlace(auth.platform or "", None),
        settings=runtime.settings,
        keys=("cost_footer",),
    )
    shown = view.effective == "shown"
    return CostFooterStatus(shown=shown, note=cost_footer_note(shown))


def register_cost_footer_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def set_cost_footer(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, shown: bool
    ) -> CostFooterStatus:
        """Deprecated: use ``change_setting`` with ``key="cost_footer"``. Show or hide the
        footer under each finished answer. Admin-only. Removed in a later release.

        For the whole workspace. The footer is one line naming the agent, the
        time taken, what the turn cost and the money left. With ``shown``
        false, answers end without it; the answer itself and the billing
        panel are unchanged. On by default. Read the returned ``note`` back to the user.
        """
        return await _set_cost_footer_impl(runtime, await _auth(ctx), shown)

    @mcp.tool
    async def get_cost_footer(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> CostFooterStatus:
        """Deprecated: use ``get_settings``. Report whether finished answers here end with
        the cost footer. Removed in a later release.

        Reading needs no admin; changing it does. Read the returned ``note`` back to the user.
        """
        return await _get_cost_footer_impl(runtime, await _auth(ctx))

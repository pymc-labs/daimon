"""Cost footer tools: show or hide the line under each finished answer, workspace-wide.

The line names the agent, the time taken, what the turn cost and the money
left. A workspace that does not want its members to see spend hides it; the
answer is unchanged and the billing panel still shows usage and credit.

``set_cost_footer`` changes the whole tenant, so it is admin-tagged and gated.
``get_cost_footer`` stays readable by anyone, like ``get_plain_text_mentions``,
so a member who asks hears the answer and who can change it. Discord admins
reach the same setting with ``/cost-footer``.
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
from daimon.core.cost_footer import cost_footer_note
from daimon.core.errors import StoreError
from daimon.core.stores.tenants import get_cost_footer, set_cost_footer
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
    async with runtime.session_factory.begin() as session:
        try:
            await set_cost_footer(session, tenant_id=auth.tenant_id, shown=shown)
        except StoreError as exc:
            raise ToolError("internal: this chat space no longer exists") from exc
    return CostFooterStatus(shown=shown, note=cost_footer_note(shown))


async def _get_cost_footer_impl(runtime: McpRuntime, auth: AuthIdentity) -> CostFooterStatus:
    async with runtime.session_factory() as session:
        shown = await get_cost_footer(session, tenant_id=auth.tenant_id)
    return CostFooterStatus(shown=shown, note=cost_footer_note(shown))


def register_cost_footer_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def set_cost_footer(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, shown: bool
    ) -> CostFooterStatus:
        """Show or hide the footer under each finished answer. Admin-only.

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
        """Report whether finished answers here end with the cost footer.

        Reading needs no admin; changing it does (``set_cost_footer``, an
        admin). Read the returned ``note`` back to the user.
        """
        return await _get_cost_footer_impl(runtime, await _auth(ctx))

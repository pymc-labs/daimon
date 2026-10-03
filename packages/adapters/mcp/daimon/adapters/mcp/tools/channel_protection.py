"""Channel protection tool: protect or seal one channel, or lift either.

``register_channel_protection_tools(mcp, runtime)`` wires the ``@mcp.tool``
closure; it delegates to ``_set_channel_protection_impl``, which tests call
without a FastMCP Context. `authorize(SET_CHANNEL_PROTECTION)` decides who
may (``daimon.core.channel_protection``): server admins and operator tokens
with ``channels:write`` only, never a channel's admins.
"""

from __future__ import annotations

from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import parse_channel_target
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL
from daimon.core.authz import Action
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_protection import ChannelProtectionRefused, set_channel_protection
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class SetChannelProtectionResult:
    """Result returned from set_channel_protection."""

    channel_id: str
    protected: bool
    """Whether daimon may not post in the channel, or any thread under it, after the change."""
    sealed: bool
    """Whether the channel's content is readable only from inside it after the change."""
    changed: bool
    note: str


async def _set_channel_protection_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    protected: bool | None = None,
    sealed: bool | None = None,
) -> SetChannelProtectionResult:
    require_scope(auth, "channels:write")
    if auth.platform not in ("discord", "slack", "teams"):
        raise ToolError("Channel protection exists only on Discord, Slack and Teams.")
    if protected is None and sealed is None:
        raise ToolError("Pass protected, sealed or both. Nothing was changed.")
    try:
        channel, _, _ = normalize_channel_admin_ids(
            auth.platform,
            channel_id=parse_channel_target(auth.platform, channel_id).channel_id,
            role_ids=(),
            user_ids=(),
        )
    except InvalidChannelAdminIds as exc:
        raise ToolError(f"{exc}. Nothing was changed.") from exc
    try:
        change = await set_channel_protection(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            channel_id=channel,
            protected=protected,
            sealed=sealed,
            subject=mcp_subject(auth, is_admin=auth.is_admin),
            default=runtime.deployment_default,
        )
    except AccessPolicyUnreadable as exc:
        raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    except ChannelProtectionRefused as exc:
        if exc.reason == "admin_required":
            record_authz_denial(Action.SET_CHANNEL_PROTECTION, exc.reason)
        raise ToolError(f"{exc} Nothing was changed. Tell the caller. Do not retry.") from exc
    state = [
        "daimon posts nothing there" if change.protected else "daimon may post there",
        "its content is readable only from inside it"
        if change.sealed
        else "its content is readable from elsewhere",
    ]
    note = f"{'; '.join(state).capitalize()}."
    return SetChannelProtectionResult(
        channel_id=change.channel_id,
        protected=change.protected,
        sealed=change.sealed,
        changed=change.changed,
        note=" ".join(filter(None, [note, change.network_warning])),
    )


def register_channel_protection_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def set_channel_protection(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        protected: bool | None = None,
        sealed: bool | None = None,
    ) -> SetChannelProtectionResult:
        """Protect or seal one channel, or lift either. For example, stop daimon posting
        in #announcements, or make #legal readable only from inside it. Leave a flag out
        to keep it as it is.

        ``protected`` stops every post into the channel and its threads, replies
        included. ``sealed`` makes its messages and conversations readable only from
        inside it. Requires a server admin; a channel's own admins can't change either.
        A confidential channel stays sealed until it is unmarked confidential
        (``set_channel_isolation``). ``channel_id`` is the channel's id; a Slack or
        Teams thread id names its channel.
        """
        return await _set_channel_protection_impl(
            runtime,
            await _auth(ctx),
            channel_id=channel_id,
            protected=protected,
            sealed=sealed,
        )

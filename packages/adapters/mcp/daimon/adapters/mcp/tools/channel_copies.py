"""Channel copy tool: retire the copy a closing channel was given as its own agent.

``register_channel_copy_tools(mcp, runtime)`` wires the ``@mcp.tool``
closure; it delegates to ``_archive_channel_copy_impl``, which tests call
without a FastMCP Context. Server admins and operator tokens with
``agents:archive`` only; which agents may go is
``daimon.core.channel_copies``'s.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import parse_channel_target
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL
from daimon.core.authz import Action
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_copies import ChannelCopyArchiveRefused, archive_channel_copy
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_NOTE = (
    "The agent is archived and its rule and default are gone. A channel it was closed "
    "with keeps its rule, so nothing answers there."
)


@dataclass(frozen=True)
class ArchiveChannelCopyResult:
    """Result returned from archive_channel_copy."""

    name: str
    ma_agent_id: str
    closed_channel_id: str | None
    note: str


def _channel(auth: AuthIdentity, channel_id: str) -> str:
    platform = auth.platform
    if platform not in ("discord", "slack", "teams"):
        raise ToolError("Channels exist only on Discord, Slack and Teams. Nothing was changed.")
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


async def _archive_channel_copy_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    name: str,
    channel_id: str | None = None,
    expected_ma_agent_id: str | None = None,
) -> ArchiveChannelCopyResult:
    require_scope(auth, "agents:archive")
    closing = _channel(auth, channel_id) if channel_id is not None else None
    try:
        archived = await archive_channel_copy(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            name=name.strip(),
            closing_channel_id=closing,
            subject=mcp_subject(auth, is_admin=auth.is_admin),
            default=runtime.deployment_default,
            expected_ma_agent_id=expected_ma_agent_id,
        )
    except AccessPolicyUnreadable as exc:
        raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    except anthropic.APIError as exc:
        raise ToolError("Archiving failed upstream. Nothing was changed; try again later.") from exc
    except ChannelCopyArchiveRefused as exc:
        if exc.reason == "admin_required":
            record_authz_denial(Action.ARCHIVE_CHANNEL_COPY, exc.reason)
        raise ToolError(f"{exc} Nothing was changed.") from exc
    return ArchiveChannelCopyResult(
        name=archived.name,
        ma_agent_id=archived.agent_id,
        closed_channel_id=archived.closed_channel_id,
        note=_NOTE,
    )


def register_channel_copy_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("agents:archive")})
    async def archive_channel_copy(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        channel_id: str | None = None,
        expected_ma_agent_id: str | None = None,
    ) -> ArchiveChannelCopyResult:
        """Archive the agent copied as a channel's own, when that channel closes. For
        example, retire #client-acme's agent once the engagement ends. Requires a server
        or workspace admin.

        Only an agent ``set_channel_rule`` copied for a channel can be archived here,
        never a built-in agent or a default. Pass ``channel_id``, the channel it was made
        for, to drop its rule and default there with it; while its rule or a default
        names anywhere else, the call is refused. The channel keeps its rule, so nothing
        answers there afterwards. ``expected_ma_agent_id`` guards against a namesake
        made since the agent was listed.
        """
        return await _archive_channel_copy_impl(
            runtime,
            await _auth(ctx),
            name=name,
            channel_id=channel_id,
            expected_ma_agent_id=expected_ma_agent_id,
        )

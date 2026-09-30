"""The tenant access policy as the channel tools see it.

One write guard for Discord and Slack: each platform resolves its target to a
channel id (plus parent channel and category where it has them) and calls
`require_channel_writable` after its own caller-permission check, so the
policy never reveals a channel the caller could not see anyway. Protection
applies to admins too.

Reads go through `ChannelReadPolicy`, which the channel dispatcher
(`tools/channels.py`) loads once per call and hands to the platform impl. A
sealed channel -- or a thread under one -- is readable only when the call
names the origin of a turn inside that same channel; with no origin, or a
foreign one, it is refused and its search hits are withheld. An isolated
channel reads the same way, except that its own agents are inside it
wherever they run (`tools/_isolation.py`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._isolation import load_caller_isolation
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    TenantAccessPolicy,
    is_write_protected,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.turn_origins import get_active_origin
from fastmcp.exceptions import ToolError

_PROTECTED_MSG = (
    "this channel is protected: the workspace does not let daimon post there. "
    "Tell the caller and offer to post somewhere else. Do not retry."
)
_UNREADABLE_MSG = (
    "this workspace's access policy could not be read, so daimon won't read or post channels"
)
_SEALED_MSG = (
    "this channel is sealed: it can only be read from a conversation inside it. "
    "If you are answering in that channel, pass this turn's origin_context_id; "
    "otherwise tell the caller. Do not retry."
)


async def load_channel_policy(runtime: McpRuntime, auth: AuthIdentity) -> TenantAccessPolicy:
    """Load the caller's tenant policy; an unreadable one refuses the call."""
    try:
        async with runtime.session_factory() as session:
            return await load_access_policy(session, tenant_id=auth.tenant_id)
    except AccessPolicyUnreadable as exc:
        raise ToolError(_UNREADABLE_MSG) from exc


async def require_channel_writable(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
) -> None:
    """Raise ToolError when the tenant policy protects the target from agent writes."""
    policy = await load_channel_policy(runtime, auth)
    if is_write_protected(
        policy,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
    ):
        raise ToolError(_PROTECTED_MSG)


_ISOLATED_MSG = (
    "this channel is isolated: only a conversation inside it, or its own agents, can read "
    "it. Tell the caller. Do not retry."
)


class SealedChannelError(ToolError):
    """A read of a sealed channel from outside it. Not an access problem the
    caller can fix by connecting an account, so no connect hint follows it."""


@dataclass(frozen=True)
class ChannelReadPolicy:
    """The tenant policy plus the channels the calling turn runs in."""

    policy: TenantAccessPolicy
    origin_channel_ids: frozenset[str] = frozenset()
    inside_channel_id: str | None = None
    """The isolated channel whose own agent executes the call, if any."""

    @property
    def restricts_any(self) -> bool:
        """Whether some channel is sealed or isolated, so counts may include withheld hits."""
        return bool(self.policy.sealed_channel_ids or self.policy.isolated_channel_ids)

    def _sealed_allows(self, channel_id: str, parent_channel_id: str | None) -> bool:
        sealed = self.policy.sealed_channel_ids
        if channel_id in sealed:
            return channel_id in self.origin_channel_ids
        if parent_channel_id is not None and parent_channel_id in sealed:
            return parent_channel_id in self.origin_channel_ids
        return True

    def _isolated_allows(self, channel_id: str, parent_channel_id: str | None) -> bool:
        for candidate in (channel_id, parent_channel_id):
            if candidate is not None and candidate in self.policy.isolated_channel_ids:
                return candidate == self.inside_channel_id or candidate in self.origin_channel_ids
        return True

    def allows(self, channel_id: str, parent_channel_id: str | None = None) -> bool:
        return self._sealed_allows(channel_id, parent_channel_id) and self._isolated_allows(
            channel_id, parent_channel_id
        )

    def require(self, channel_id: str, parent_channel_id: str | None = None) -> None:
        """Raise ToolError for a sealed or isolated target outside the calling turn.

        Call after the platform's caller-permission check."""
        if not self._sealed_allows(channel_id, parent_channel_id):
            raise SealedChannelError(_SEALED_MSG)
        if not self._isolated_allows(channel_id, parent_channel_id):
            raise SealedChannelError(_ISOLATED_MSG)


# What an impl reads with when no dispatcher loaded a policy (direct test calls).
OPEN_READ_POLICY = ChannelReadPolicy(policy=OPEN_ACCESS_POLICY)


async def load_read_policy(
    runtime: McpRuntime, auth: AuthIdentity, *, origin_context_id: str | None
) -> ChannelReadPolicy:
    """Load the tenant policy and, if one was named, the caller's active turn origin.

    The origin must belong to the caller's account and to the agent the token
    executes as: ``agent_id`` for agent-session tokens, ``chat_agent_id`` for
    ordinary chat. A token bound to neither can't claim an origin at all, so a
    sealed read with it is judged from outside. An origin that is malformed,
    expired, another account's or another responder's counts as none too.
    """
    policy = await load_channel_policy(runtime, auth)
    inside_channel_id = None
    if policy.isolated_channel_ids:
        inside_channel_id = (await load_caller_isolation(runtime, auth)).inside_channel_id
    outside = ChannelReadPolicy(policy=policy, inside_channel_id=inside_channel_id)
    executing_agent = auth.agent_id or auth.chat_agent_id
    if (
        not (policy.sealed_channel_ids or policy.isolated_channel_ids)
        or not origin_context_id
        or auth.platform is None
        or executing_agent is None
    ):
        return outside
    try:
        origin_id = uuid.UUID(origin_context_id)
    except ValueError:
        return outside
    async with runtime.session_factory() as session:
        origin = await get_active_origin(
            session,
            origin_id=origin_id,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=auth.platform,
            now=datetime.now(UTC),
        )
    if origin is None or executing_agent != derive_agent_uuid(
        tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id
    ):
        return outside
    inside = {origin.parent_channel_id, origin.thread_id}
    if auth.platform == "slack":
        # A Slack thread is sealed by its channel_id:thread_ts form.
        inside.add(f"{origin.parent_channel_id}:{origin.thread_id}")
    return ChannelReadPolicy(
        policy=policy, origin_channel_ids=frozenset(inside), inside_channel_id=inside_channel_id
    )

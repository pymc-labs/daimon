"""Shared session ownership and seal gates for account, agent-chat and hub tools."""

from __future__ import annotations

from collections.abc import Sequence

from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy, load_read_policy
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_PRIVATE_DM,
    MA_METADATA_KEY_SEALED,
    MA_METADATA_KEY_THREAD,
)
from daimon.core.stores.thread_sessions import thread_ids_for_sessions
from fastmcp.exceptions import ToolError

_SEALED_SESSION_MSG = (
    "this conversation ran in a sealed channel: its transcript can only be read "
    "or continued from a conversation inside that channel. Tell the caller. Do not retry."
)


def session_belongs_to_caller(session: BetaManagedAgentsSession, auth: AuthIdentity) -> bool:
    """Private transcripts require the exact verified execution grant, not just an account.

    The stamp also protects session mutation: another caller must not drive the
    private session while it holds an active read grant. Missing or malformed
    grants fail closed; admins have no exception. Discord private scopes carry
    no execution credential, so all MCP session introspection is denied there.
    """
    metadata = session.metadata or {}
    if metadata.get(MA_METADATA_KEY_ACCOUNT) != str(auth.account_id):
        return False
    if MA_METADATA_KEY_PRIVATE_DM not in metadata:
        return True
    return (
        auth.slack_turn_context_id is not None
        and auth.agent_id is None
        and metadata[MA_METADATA_KEY_PRIVATE_DM] == str(auth.slack_turn_context_id)
    )


def _seal_allows(
    session: BetaManagedAgentsSession, read: ChannelReadPolicy, legacy_thread_id: str | None
) -> bool:
    """Whether the calling turn may see this session under the channel seal.

    A stamped session is judged like a channel read of the channel and thread it
    ran in, against the current policy -- so sealing a channel later covers its
    old conversations -- and one stamped sealed at creation stays sealed after
    an unseal. An unstamped session a thread ran on predates the stamp and its
    parent channel is unknown: while the tenant seals anything it is shown only
    inside that same thread. An unstamped session no thread ran on is a
    headless one and carries no channel content.
    """
    metadata = session.metadata or {}
    channel = metadata.get(MA_METADATA_KEY_CHANNEL)
    if channel is not None:
        thread = metadata.get(MA_METADATA_KEY_THREAD)
        where = {channel} if thread is None else {channel, thread, f"{channel}:{thread}"}
        if metadata.get(MA_METADATA_KEY_SEALED) == "true" and not (where & read.origin_channel_ids):
            return False
        if thread is None:
            return read.allows(channel)
        # A Slack thread is sealed on its own as channel_id:thread_ts.
        return read.allows(thread, channel) and read.allows(f"{channel}:{thread}", channel)
    if legacy_thread_id is None or not read.policy.sealed_channel_ids:
        return True
    return legacy_thread_id in read.origin_channel_ids


async def _legacy_threads(
    runtime: McpRuntime,
    auth: AuthIdentity,
    read: ChannelReadPolicy,
    sessions: Sequence[BetaManagedAgentsSession],
) -> dict[str, str]:
    unstamped = [s.id for s in sessions if MA_METADATA_KEY_CHANNEL not in (s.metadata or {})]
    if not unstamped or not read.policy.sealed_channel_ids:
        return {}
    async with runtime.session_factory() as db:
        return await thread_ids_for_sessions(db, tenant_id=auth.tenant_id, ma_session_ids=unstamped)


async def sessions_outside_seals(
    runtime: McpRuntime,
    auth: AuthIdentity,
    sessions: Sequence[BetaManagedAgentsSession],
    *,
    origin_context_id: str | None = None,
) -> list[BetaManagedAgentsSession]:
    """Drop the sessions whose conversation is sealed away from the calling turn.

    ``origin_context_id`` is the calling turn's origin, checked as the channel
    read tools check it (`load_read_policy`); without one every sealed
    conversation is dropped. Call after the ownership filter.
    """
    if not sessions:
        return []
    read = await load_read_policy(runtime, auth, origin_context_id=origin_context_id)
    legacy = await _legacy_threads(runtime, auth, read, sessions)
    return [s for s in sessions if _seal_allows(s, read, legacy.get(s.id))]


async def require_session_outside_seals(
    runtime: McpRuntime,
    auth: AuthIdentity,
    session: BetaManagedAgentsSession,
    *,
    origin_context_id: str | None = None,
) -> None:
    """Refuse a session whose conversation is sealed away from the calling turn.

    Re-evaluated on every call, so a seal added -- or an origin that expired --
    since the conversation started is honoured on the next read or follow-up.
    """
    if not await sessions_outside_seals(
        runtime, auth, [session], origin_context_id=origin_context_id
    ):
        raise ToolError(_SEALED_SESSION_MSG)

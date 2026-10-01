"""Shared session ownership and seal gates for account, agent-chat and hub tools."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy, load_read_policy
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_PRIVATE_DM,
    MA_METADATA_KEY_THREAD,
)
from daimon.core.session_seal import seal_ids
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_sessions import thread_ids_for_sessions
from fastmcp.exceptions import ToolError

_SEALED_SESSION_MSG = (
    "this conversation ran in a sealed channel: its transcript can only be read "
    "or continued from a conversation inside that channel. Tell the caller. Do not retry."
)


# Set only by the hub, for a person's own login whose stored role is admin:
# the hub runs headless and its output reaches only that person, so an admin
# may read and continue their own sealed conversations there. Agent keys
# (agent chat), chat turns, routines and members never set it.
_SEALED_READS_ALLOWED: ContextVar[bool] = ContextVar("daimon_admin_sealed_reads", default=False)


async def caller_is_hub_admin(runtime: McpRuntime, auth: AuthIdentity) -> bool:
    """Whether a hub caller is an admin, by the account's stored role.

    The hub has no live platform role (it pins ``is_admin=False``); the stored
    role is refreshed from the platform on every turn the person takes in the
    workspace. A token with no platform user, or an agent-scoped key, is never
    an admin here.
    """
    if auth.platform_user_id is None or auth.chat_agent_id is not None:
        return False
    async with runtime.session_factory() as db:
        account = await get_account(db, auth.account_id)
    return account is not None and account.role is Role.ADMIN


@contextmanager
def admin_sealed_reads(enabled: bool) -> Iterator[None]:
    """Let the enclosed hub call read and continue the admin's sealed sessions."""
    token = _SEALED_READS_ALLOWED.set(enabled)
    try:
        yield
    finally:
        _SEALED_READS_ALLOWED.reset(token)


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
    old conversations -- and one that ran sealed stays inside every id that
    sealed it (`daimon.core.session_seal.seal_ids`) after an unseal: the
    channel, or only the thread when the thread was sealed on its own. An
    unstamped session a thread ran on predates the stamp and its parent channel
    is unknown: while the tenant seals anything it is shown only inside that
    same thread. An unstamped session no thread ran on is a
    headless one and carries no channel content.
    """
    metadata = session.metadata or {}
    channel = metadata.get(MA_METADATA_KEY_CHANNEL)
    if channel is not None:
        thread = metadata.get(MA_METADATA_KEY_THREAD)
        if not seal_ids(metadata) <= read.origin_channel_ids:
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
    conversation is dropped -- except inside `admin_sealed_reads`, where an
    admin's hub call keeps their own sealed sessions (ownership is still
    checked by the caller first). Only a chat turn's own credential can claim one:
    an agent key (``agent_id``) runs outside every channel. Call after the
    ownership filter.
    """
    if not sessions:
        return []
    if _SEALED_READS_ALLOWED.get():
        return list(sessions)
    if auth.agent_id is not None or auth.chat_agent_id is None:
        origin_context_id = None
    read = await load_read_policy(
        runtime, auth, origin_context_id=origin_context_id, resolve_without_seals=True
    )
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

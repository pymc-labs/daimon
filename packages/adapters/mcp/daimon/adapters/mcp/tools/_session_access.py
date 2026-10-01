"""Shared session ownership and seal gates for account, agent-chat and hub tools."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy, load_read_policy
from daimon.core.authz import Action, SessionFacts, Subject, authorize
from daimon.core.channel_admins import load_stored_subject
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_PRIVATE_DM,
    MA_METADATA_KEY_THREAD,
)
from daimon.core.session_seal import seal_ids
from daimon.core.stores.thread_sessions import thread_ids_for_sessions
from fastmcp.exceptions import ToolError

_SEALED_SESSION_MSG = (
    "this conversation ran in a sealed channel: its transcript can only be read "
    "or continued from a conversation inside that channel. Tell the caller. Do not retry."
)


@dataclass(frozen=True)
class _HubSealedAccess:
    mode: Literal["read", "continue"]
    # None for a server admin (every channel); a channel admin's channels otherwise.
    channel_ids: frozenset[str] | None


# Set only by the hub, for a workspace admin or a channel admin
# (`load_hub_subject`): the hub runs headless and its output reaches only that
# person, so an admin may READ any sealed conversation of the agent there,
# anyone's, and a channel admin those of the channels they administer. "read"
# admits sealed sessions (and other accounts' channel sessions) to
# list/get/events; "continue" never admits a sealed one -- continuing would add
# the admin's private text to a session its channel goes on reusing -- and only
# changes the refusal to say where to continue it. Agent keys (agent chat),
# chat turns, routines and members never set it.
_HUB_SEALED_ACCESS: ContextVar[_HubSealedAccess | None] = ContextVar(
    "daimon_hub_sealed_access", default=None
)

_ADMIN_CONTINUE_REFUSED = (
    "this conversation ran in a sealed channel. As an admin you can read it from "
    "here, but continue it in its channel: a follow-up from here would join the "
    "channel's own conversation. Tell the caller. Do not retry."
)


async def load_hub_subject(runtime: McpRuntime, auth: AuthIdentity) -> Subject:
    """A hub caller as their stored role and channel admin grants describe them.

    The hub has no live platform role (it pins ``is_admin=False``); the stored
    role and role ids are the ones each platform turn records from the
    adapter's live check, so a demotion or promotion takes effect on the
    person's next Discord, Slack or Teams turn in that workspace. Only a
    person's own hub login qualifies: an agent-scoped key, a chat-turn
    credential or a token with no platform user is never an admin here,
    whatever role its account holds.
    """
    if (
        auth.platform_user_id is None
        or auth.chat_agent_id is not None
        or auth.slack_turn_context_id is not None
    ):
        return Subject(platform_user_id=auth.platform_user_id)
    async with runtime.session_factory() as db:
        return await load_stored_subject(
            db,
            tenant_id=auth.tenant_id,
            platform=auth.platform,
            account_id=auth.account_id,
            platform_user_id=auth.platform_user_id,
        )


@contextmanager
def admin_sealed_access(subject: Subject, mode: Literal["read", "continue"]) -> Iterator[None]:
    """Run the enclosed hub call with an admin's or channel admin's sealed-session access.

    A server admin's covers every channel, a channel admin's the channels they
    administer; anyone else runs the call as a member.
    """
    access = None
    if subject.is_admin:
        access = _HubSealedAccess(mode=mode, channel_ids=None)
    elif subject.administered_channel_ids:
        access = _HubSealedAccess(mode=mode, channel_ids=subject.administered_channel_ids)
    token = _HUB_SEALED_ACCESS.set(access)
    try:
        yield
    finally:
        _HUB_SEALED_ACCESS.reset(token)


def _hub_access_covers(access: _HubSealedAccess, session: BetaManagedAgentsSession) -> bool:
    """Whether the session ran in a channel this hub access covers."""
    channel = (session.metadata or {}).get(MA_METADATA_KEY_CHANNEL)
    return access.channel_ids is None or channel in access.channel_ids


def _hub_read_opens(session: BetaManagedAgentsSession) -> bool:
    """Whether the hub read in progress opens this channel conversation, anyone's, sealed or not.

    Never a private DM, and never to continue it.
    """
    access = _HUB_SEALED_ACCESS.get()
    metadata = session.metadata or {}
    return (
        access is not None
        and access.mode == "read"
        and MA_METADATA_KEY_CHANNEL in metadata
        and MA_METADATA_KEY_PRIVATE_DM not in metadata
        and not _is_private_conversation(str(metadata[MA_METADATA_KEY_CHANNEL]))
        and not _is_private_conversation(str(metadata.get(MA_METADATA_KEY_THREAD) or ""))
        and _hub_access_covers(access, session)
    )


async def admin_readable_legacy_sessions(
    runtime: McpRuntime, auth: AuthIdentity, sessions: Sequence[BetaManagedAgentsSession]
) -> set[str]:
    """Other members' pre-stamp channel sessions an admin's hub read may open.

    Only inside `admin_sealed_access("read")`. A session from before the
    channel stamp carries no ``daimon_channel``; a durable thread mapping in
    the caller's tenant (`thread_ids_for_sessions`) shows it is a channel
    conversation all the same. Never a private DM, never a headless session,
    and never for a channel admin: with no channel stamp the session can't be
    placed in the channels they administer.
    """
    access = _HUB_SEALED_ACCESS.get()
    if access is None or access.mode != "read" or access.channel_ids is not None:
        return set()
    candidates = [
        s.id
        for s in sessions
        if (s.metadata or {}).get(MA_METADATA_KEY_ACCOUNT) != str(auth.account_id)
        and MA_METADATA_KEY_CHANNEL not in (s.metadata or {})
        and MA_METADATA_KEY_PRIVATE_DM not in (s.metadata or {})
    ]
    if not candidates:
        return set()
    async with runtime.session_factory() as db:
        mapped = await thread_ids_for_sessions(
            db, tenant_id=auth.tenant_id, ma_session_ids=candidates
        )
    # A DM scope ("dm:<uuid>") or a Teams personal chat ("a:…") is a private
    # conversation, never a channel one.
    return {sid for sid, thread in mapped.items() if not _is_private_conversation(thread)}


def _is_private_conversation(conversation_id: str) -> bool:
    """A DM scope, a Slack IM (``D…``) or a Teams personal chat (``a:…``).

    Sessions that ran there are private even without the private-DM stamp
    (sessions created before every DM turn was stamped).
    """
    # Discord ids are numeric and Teams channels start "19:", so a leading
    # "D" is only ever a Slack IM.
    return conversation_id.startswith(("dm:", "a:", "D"))


def session_belongs_to_caller(session: BetaManagedAgentsSession, auth: AuthIdentity) -> bool:
    """Private transcripts require the exact verified execution grant, not just an account.

    The stamp also protects session mutation: another caller must not drive the
    private session while it holds an active read grant. Missing or malformed
    grants fail closed; admins have no exception for them. Inside a hub
    admin read (`admin_sealed_access(..., "read")`) another account's channel
    conversation is admitted too, in a channel the access covers. Discord private scopes carry
    no execution credential, so all MCP session introspection is denied there.
    """
    metadata = session.metadata or {}
    if metadata.get(MA_METADATA_KEY_ACCOUNT) != str(auth.account_id):
        return _hub_read_opens(session)
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
    return bool(
        authorize(
            read.policy,
            subject=Subject(),
            action=Action.READ_SESSION,
            origin_channel_ids=read.origin_channel_ids,
            session=SessionFacts(
                channel=channel,
                thread=metadata.get(MA_METADATA_KEY_THREAD) if channel is not None else None,
                seal_ids=frozenset(seal_ids(metadata)),
                legacy_thread_id=legacy_thread_id,
            ),
        )
    )


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
    conversation is dropped -- except inside `admin_sealed_access(..., "read")`,
    where an admin's hub read keeps sealed channel sessions, anyone's, and a
    channel admin's those in the channels they administer (ownership is still
    checked by the caller first). Only a chat turn's own credential can claim one:
    an agent key (``agent_id``) runs outside every channel, unless it was minted
    in one (`token_channel_id`), which then counts as its origin. Call after the
    ownership filter.
    """
    if not sessions:
        return []
    access = _HUB_SEALED_ACCESS.get()
    if access is not None and access.mode == "read" and access.channel_ids is None:
        return list(sessions)
    if auth.agent_id is not None or auth.chat_agent_id is None:
        origin_context_id = None
    read = await load_read_policy(
        runtime, auth, origin_context_id=origin_context_id, resolve_without_seals=True
    )
    legacy = await _legacy_threads(runtime, auth, read, sessions)
    return [s for s in sessions if _hub_read_opens(s) or _seal_allows(s, read, legacy.get(s.id))]


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
        access = _HUB_SEALED_ACCESS.get()
        if access is not None and access.mode == "continue" and _hub_access_covers(access, session):
            raise ToolError(_ADMIN_CONTINUE_REFUSED)
        raise ToolError(_SEALED_SESSION_MSG)

"""Shared session ownership and seal gates for account, agent-chat and hub tools."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy, load_read_policy
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, SessionFacts, Subject, Surface, authorize, build_subject
from daimon.core.channel_admins import GroupMembers, confirm_stored_subject, read_stored_admin
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_PRIVATE_DM,
    is_private_routine_stamp,
)
from daimon.core.session_seal import session_facts
from daimon.core.stores.thread_sessions import thread_ids_for_sessions
from fastmcp.exceptions import ToolError

_SEALED_SESSION_MSG = (
    "this conversation ran in a sealed channel: its transcript can only be read "
    "or continued from a conversation inside that channel. Tell the caller. Do not retry."
)


@dataclass(frozen=True)
class _HubAccess:
    subject: Subject
    mode: Literal["read", "continue"]


# Set only by the hub, for every caller (`load_hub_subject`): who is calling
# and which hub action is running -- "read" (list/get/events) or "continue" --
# so the session checks below ask `authorize` on the hub surface. The decision
# itself is `authorize(READ_SESSION / CONTINUE_SESSION)`: an admin may read any
# channel conversation of the agent from the hub, a channel admin those of the
# channels they administer, never a private DM, never to continue it. Agent
# keys (agent chat), chat turns and routines never set it.
_HUB_ACCESS: ContextVar[_HubAccess | None] = ContextVar("daimon_hub_access", default=None)

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
    whatever role its account holds. A stored Slack group or Teams team
    counts only while a live lookup, run after the session closes, still
    admits the person.
    """
    if (
        auth.platform_user_id is None
        or auth.chat_agent_id is not None
        or auth.slack_turn_context_id is not None
    ):
        return build_subject(is_admin=False, platform_user_id=auth.platform_user_id)
    async with runtime.session_factory() as db:
        stored = await read_stored_admin(
            db,
            tenant_id=auth.tenant_id,
            platform=auth.platform,
            account_id=auth.account_id,
            platform_user_id=auth.platform_user_id,
        )
    return await confirm_stored_subject(
        stored, stored_group_members(runtime, auth.platform, auth.external_id)
    )


def stored_group_members(
    runtime: McpRuntime, platform: str | None, workspace_id: str | None
) -> GroupMembers | None:
    """The live re-check for a stored Slack group or Teams team; None checks none."""
    if runtime.group_lookups is None or platform is None or workspace_id is None:
        return None
    return runtime.group_lookups.members(platform, workspace_id)


def _hub_request(auth: AuthIdentity) -> tuple[Subject, Surface, Action]:
    """The caller and surface the session checks ask `authorize` about."""
    access = _HUB_ACCESS.get()
    if access is None:
        return mcp_subject(auth), Surface.AGENT_CHAT, Action.READ_SESSION
    action = Action.READ_SESSION if access.mode == "read" else Action.CONTINUE_SESSION
    # The hub calls through a person's own login (it mounts their agent
    # identity), described by `load_hub_subject`.
    return access.subject, Surface.HUB, action


def _session_facts(
    metadata: Mapping[str, str],
    *,
    owned: bool,
    legacy_thread_id: str | None = None,
) -> SessionFacts:
    return session_facts(metadata, owned=owned, legacy_thread_id=legacy_thread_id)


def _hub_reads_as_anyone(
    subject: Subject, metadata: Mapping[str, str], legacy_thread_id: str | None = None
) -> bool:
    """Whether `authorize`'s admin or channel admin hub read covers this session."""
    return bool(
        authorize(
            TenantAccessPolicy(),
            subject=subject,
            action=Action.READ_SESSION,
            surface=Surface.HUB,
            session=_session_facts(metadata, owned=False, legacy_thread_id=legacy_thread_id),
        )
    )


def _admin_may_read_other(metadata: Mapping[str, str], legacy_thread_id: str | None = None) -> bool:
    """Whether the hub read in progress may open another account's session."""
    access = _HUB_ACCESS.get()
    return (
        access is not None
        and access.mode == "read"
        and _hub_reads_as_anyone(access.subject, metadata, legacy_thread_id)
    )


@contextmanager
def hub_session_access(subject: Subject, mode: Literal["read", "continue"]) -> Iterator[None]:
    """Run the enclosed hub call as `subject` (`load_hub_subject`) on the hub surface."""
    token = _HUB_ACCESS.set(_HubAccess(subject=subject, mode=mode))
    try:
        yield
    finally:
        _HUB_ACCESS.reset(token)


async def admin_readable_legacy_sessions(
    runtime: McpRuntime, auth: AuthIdentity, sessions: Sequence[BetaManagedAgentsSession]
) -> set[str]:
    """Other members' pre-stamp channel sessions an admin's hub read may open.

    Only inside `hub_session_access(..., "read")`. A session from before the
    channel stamp carries no ``daimon_channel``; a durable thread mapping in
    the caller's tenant (`thread_ids_for_sessions`) shows it is a channel
    conversation all the same. Never a private DM, never a headless session,
    and never for a channel admin: it can't be placed in their channels.
    """
    access = _HUB_ACCESS.get()
    # Only a server admin's hub read opens one (`authorize`); skip the lookup otherwise.
    if access is None or access.mode != "read" or not access.subject.is_admin:
        return set()
    by_id = {
        s.id: (s.metadata or {})
        for s in sessions
        if (s.metadata or {}).get(MA_METADATA_KEY_ACCOUNT) != str(auth.account_id)
        and MA_METADATA_KEY_CHANNEL not in (s.metadata or {})
    }
    if not by_id:
        return set()
    async with runtime.session_factory() as db:
        mapped = await thread_ids_for_sessions(
            db, tenant_id=auth.tenant_id, ma_session_ids=list(by_id)
        )
    # A DM scope ("dm:<uuid>") or a Teams personal chat ("a:…") is a private
    # conversation, never a channel one: `authorize` refuses it.
    return {
        sid
        for sid, thread in mapped.items()
        if _admin_may_read_other(by_id[sid], legacy_thread_id=thread)
    }


def session_belongs_to_caller(session: BetaManagedAgentsSession, auth: AuthIdentity) -> bool:
    """Private transcripts require the exact verified execution grant, not just an account.

    The stamp also protects session mutation: another caller must not drive the
    private session while it holds an active read grant. Missing or malformed
    grants fail closed; admins have no exception for them. Inside a hub read
    (`hub_session_access(..., "read")`) an admin's or channel admin's
    `authorize` read admits another account's channel conversation too.
    Discord private scopes carry
    no execution credential, so all MCP session introspection is denied there.
    A routine's private stamp is its owner's own: no grant is needed, and no
    one else's hub read opens it.
    """
    metadata = session.metadata or {}
    if metadata.get(MA_METADATA_KEY_ACCOUNT) != str(auth.account_id):
        # Another account's session: only `authorize`'s admin or channel admin
        # hub read opens it (never a private DM, never to continue).
        return _admin_may_read_other(metadata)
    private = metadata.get(MA_METADATA_KEY_PRIVATE_DM)
    if private is None or is_private_routine_stamp(private):
        # A routine's owner reads its transcript like any of theirs; the seal still applies.
        return True
    return (
        auth.slack_turn_context_id is not None
        and auth.agent_id is None
        and metadata[MA_METADATA_KEY_PRIVATE_DM] == str(auth.slack_turn_context_id)
    )


def _seal_allows(
    session: BetaManagedAgentsSession,
    read: ChannelReadPolicy,
    legacy_thread_id: str | None,
    hub: tuple[Subject, Surface, Action],
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
    headless one and carries no channel content. While a channel is isolated,
    its sessions are shown only to its own agents (`read.agent`), and a call
    held to it is shown only its sessions.
    """
    subject, surface, action = hub
    return bool(
        authorize(
            read.policy,
            subject=subject,
            action=action,
            surface=surface,
            # Only an isolated channel's own agents read its sessions, and a
            # call held to one reads only the sessions that ran there.
            agent=read.agent,
            origin_channel_ids=read.origin_channel_ids,
            origin=read.origin_place,
            session=_session_facts(
                session.metadata or {}, owned=True, legacy_thread_id=legacy_thread_id
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
    conversation is dropped -- except inside `hub_session_access(..., "read")`,
    where an admin's hub read keeps sealed channel sessions, anyone's, and a
    channel admin's those of their channels (ownership is still checked by the
    caller first). Only a chat turn's own credential can claim one:
    an agent key (``agent_id``) runs outside every channel, unless it was minted
    in one (`token_channel_id`), which then counts as its origin. Call after the
    ownership filter.
    """
    if not sessions:
        return []
    hub = _hub_request(auth)
    if auth.agent_id is not None or auth.chat_agent_id is None:
        origin_context_id = None
    read = await load_read_policy(
        runtime, auth, origin_context_id=origin_context_id, resolve_without_seals=True
    )
    legacy = await _legacy_threads(runtime, auth, read, sessions)
    return [s for s in sessions if _seal_allows(s, read, legacy.get(s.id), hub)]


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
        access = _HUB_ACCESS.get()
        if (
            access is not None
            and access.mode == "continue"
            and _hub_reads_as_anyone(access.subject, session.metadata or {})
        ):
            raise ToolError(_ADMIN_CONTINUE_REFUSED)
        raise ToolError(_SEALED_SESSION_MSG)

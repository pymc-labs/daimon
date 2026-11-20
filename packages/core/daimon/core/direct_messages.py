"""Private conversations using the ordinary admission, scope and turn pipeline."""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    nullcontext,
    suppress,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import anthropic
import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsImageBlockParam
from daimon.core.access_policy import (
    CHANNEL_SCOPE_PREFIX,
    DM_SCOPE_PREFIX,
    TenantAccessPolicy,
)
from daimon.core.authz import Action, Place, Subject, Surface, authorize, build_subject
from daimon.core.confirmation import ConfirmationHook
from daimon.core.context_prompt import TurnContext
from daimon.core.dm_access import DmAgent, dm_agent_of, permits_dm
from daimon.core.errors import UserFacingError
from daimon.core.features.model_input import DEFAULT_DM_HISTORY, DmHistoryCap
from daimon.core.handoff_context import TranscriptTurn, render_previous_session
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.membership_subject import resolve_subject
from daimon.core.monitoring import MonitoringNotice
from daimon.core.mux_backend import resource_scope
from daimon.core.output_runtime import OutputRuntime
from daimon.core.permissions import dm_source_limited, limited_ids
from daimon.core.platform_words import words_for
from daimon.core.routine_sandbox import RoutineSandbox
from daimon.core.scope import ChannelScopeRef
from daimon.core.session_fence_retry import FenceUnavailable, retry_fences, try_fence
from daimon.core.setup_conversations import BoundAgentUnavailable
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.direct_messages import (
    COLD_DM_ORIGIN,
    DirectMessageRow,
    OwnerDmFence,
    claim_message,
    dm_enabled,
    fence_held,
    finish_message,
    get_conversation,
    get_dm_agent_name,
    holds_claim,
    lock_route,
    owner_dm_fence,
    quarantine_conversation,
    start_conversation,
)
from daimon.core.stores.dm_access import set_pick
from daimon.core.stores.dm_carry_overs import take_carry_over
from daimon.core.stores.domain import ChatPlatform, Role, TaskContinuationRow, TenantRow
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    delete_slack_turn_context,
)
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_agent_bindings import create_binding, get_binding
from daimon.core.stores.thread_sessions import (
    get_live_thread_session,
    mark_dead,
    retire_dm_scope,
    update_watermark,
)
from daimon.core.turn.admission import Admission, AdmissionGrant, DmSource, admit
from daimon.core.turn.ceiling import LONGEST_TURN_CEILING_S, TURN_CEILING_S, turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied, DmSourceSealedError, SessionBusyError
from daimon.core.turn.io import turn_io
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import PreparedTurn, bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.turn.termination import TerminationReason
from daimon.core.turn.unattended import dm_routine_sandbox
from daimon.core.turn_hold import RequesterOrg
from daimon.core.turn_origin import render_turn_origin, turn_origin
from mux.profiles import MANAGED_AGENTS
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

_log = structlog.get_logger(__name__)


#: A person this agent's DM grant, or a lock, does not admit.
DM_AGENT_REFUSED = (
    "You can't have a private conversation with this agent here.\n\n"
    "Ask an admin if you think you should have access."
)


class DirectMessageAccessRevoked(UserFacingError):
    """Private access failed closed; Discord suppresses the reply and logs the reason."""

    def __init__(self, reason: str, *, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class DirectMessageClaimLost(UserFacingError):
    """The turn's owner fence dropped, so a later message may own the conversation."""

    def __init__(self) -> None:
        super().__init__("This reply was interrupted.\n\nSend your message again.")


class DirectMessageMembershipUnknown(DirectMessageAccessRevoked):
    """The recipient's membership lookup failed; nothing proved their access gone."""

    def __init__(self) -> None:
        super().__init__("delivery_membership_unknown", message="Private access unverified.")


@dataclass(frozen=True)
class DirectMessageReply:
    """Text and the exact agent authority used for this private model turn.

    ``outputs`` is the session whose saved files the adapter delivers after
    the answer; None when the turn has none (self-hosted). A reply under way
    carries the claimed ``conversation``, so a later claim stops its output;
    a finished reply's has its claim released.
    """

    text: str
    conversation: DirectMessageRow
    grant: AdmissionGrant
    outputs: OutputRuntime | None = None


async def load_dm_access_policy(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> TenantAccessPolicy:
    """Private-route policy failures are typed denials, never setup instructions."""
    try:
        return await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable as error:
        raise DirectMessageAccessRevoked("policy_unreadable", message=str(error)) from error


def tenant_ready(tenant: TenantRow | None, *, platform: str | None = None) -> bool:
    """A provisioned, unarchived tenant, on `platform` when one is named."""
    return (
        tenant is not None
        and (platform is None or tenant.platform == platform)
        and tenant.archived_at is None
        and tenant.provision_status == "ready"
    )


async def require_dm_delivery(deps: TurnDeps, reply: DirectMessageReply) -> None:
    """Recheck recipient and exact answering agent immediately before private output.

    Agent pins and isolation apply without an admin exemption. A move's source
    provenance still seals its context independently of the answering identity.
    A failed membership lookup is decided on the roles and admin standing the
    turn was admitted with: any refusal on those is proved revocation, and
    only when everything else passes does it raise `DirectMessageMembershipUnknown`.
    Both refuse this output, as does a claim the conversation no longer holds.
    """
    conversation = reply.conversation
    subject = await resolve_subject(
        deps.sessionmaker,
        deps.membership_resolver,
        tenant_id=conversation.tenant_id,
        user_id=conversation.external_user_id,
        require_fresh=True,
    )
    async with deps.sessionmaker() as session:
        policy = await load_dm_access_policy(session, tenant_id=conversation.tenant_id)
        tenant = await get_tenant(session, conversation.tenant_id)
        enabled = await dm_enabled(session, tenant_id=conversation.tenant_id)
        current = await get_conversation(
            session,
            platform=conversation.platform,
            route_key=conversation.route_key,
            external_user_id=conversation.external_user_id,
        )
    if (
        not enabled
        or not tenant_ready(tenant)
        or current is None
        or current.scope_id != conversation.scope_id
        or current.account_id != conversation.account_id
        or subject.verified_membership is False
    ):
        raise DirectMessageAccessRevoked(
            "delivery_access_revoked", message="Private access revoked."
        )
    if conversation.claimed_at is not None and current.claimed_at != conversation.claimed_at:
        raise DirectMessageAccessRevoked("claim_superseded", message="This reply was replaced.")
    membership_unknown = subject.verified_membership is None
    if membership_unknown:
        admitted = reply.grant.subject
        subject = replace(
            subject,
            is_admin=admitted.is_admin,
            role_ids=admitted.role_ids,
            verified_membership=True,
        )
    if not authorize(policy, subject=subject, action=Action.START_TURN):
        raise DirectMessageAccessRevoked(
            "delivery_access_revoked", message="Private access revoked."
        )
    if _source_is_sealed(policy, conversation):
        await _quarantine(deps, conversation)
    agent_subject = replace(subject, is_admin=False)
    for action in (Action.DIRECT_MESSAGE, Action.RUN_AGENT):
        decision = authorize(
            policy,
            subject=agent_subject,
            action=action,
            surface=Surface.DM,
            agent=reply.grant.agent,
            place=Place(),
            recipient_id=conversation.external_user_id,
        )
        if not decision:
            raise DirectMessageAccessRevoked(
                f"delivery_{decision.reason}", message="Private agent access revoked."
            )
    await _require_dm_grant(deps, reply, subject=agent_subject, policy=policy)
    if membership_unknown:
        raise DirectMessageMembershipUnknown()


async def _require_dm_grant(
    deps: TurnDeps, reply: DirectMessageReply, *, subject: Subject, policy: TenantAccessPolicy
) -> None:
    """The bound agent must still admit this person: DMs on for it, its grant, and any lock.

    The roles are the ones the rest of the check used, so a refusal here is proved revocation.
    """
    conversation = reply.conversation
    names = reply.grant.agent.names
    async with deps.sessionmaker() as session:
        binding = await get_binding(
            session,
            tenant_id=conversation.tenant_id,
            platform=conversation.platform,
            parent_channel_id=conversation.channel_id,
            thread_id=conversation.scope_id,
        )
        if binding is None or not names or names[0] is None:
            raise DirectMessageAccessRevoked(
                "delivery_agent_unbound", message="Private agent access revoked."
            )
        permitted = await permits_dm(
            session,
            deps.settings,
            policy,
            tenant_id=conversation.tenant_id,
            platform=conversation.platform,
            agent=DmAgent(names[0], binding.responder_ma_agent_id, reply.grant.agent),
            subject=subject,
            groups=frozenset(subject.role_ids),  # a Discord member's roles are their groups
        )
    if not permitted:
        raise DirectMessageAccessRevoked(
            "delivery_dm_grant", message="Private agent access revoked."
        )


async def require_dm_turn_grant(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_id: str,
    groups: frozenset[str] | None,
) -> None:
    """The admitted agent must still admit this person; unknown groups fail closed.

    An admin keeps their exemption from the agent's pins, except in a cold DM, as before.
    """
    async with deps.sessionmaker() as session:
        permitted = await permits_dm(
            session,
            deps.settings,
            await load_dm_access_policy(session, tenant_id=tenant_id),
            tenant_id=tenant_id,
            platform=platform,
            agent=dm_agent_of(admission.agent, admission.config.agent_name),
            subject=admission.grant.subject
            if admission.grant is not None
            else build_subject(is_admin=False, platform_user_id=user_id),
            groups=groups,
            admin_exempt=admission.grant is not None and not admission.grant.cold_dm,
        )
    if not permitted:
        raise DirectMessageAccessRevoked("dm_grant", message=DM_AGENT_REFUSED)


#: How many people one process remembers having told that private chats are off.
COLD_DM_NOTICE_LIMIT = 10_000


class ColdDmNotices:
    """People already answered in this process, so a cold DM gets one reply, not a stream.

    In memory and bounded: the oldest person is forgotten first, and a restart
    forgets everyone, which at worst repeats the answer once."""

    def __init__(self, limit: int = COLD_DM_NOTICE_LIMIT) -> None:
        self._limit = limit
        self._told: OrderedDict[str, None] = OrderedDict()

    def first_time(self, person: str) -> bool:
        """True the first time `person` is asked about; remembers them."""
        if person in self._told:
            self._told.move_to_end(person)
            return False
        self._told[person] = None
        if len(self._told) > self._limit:
            self._told.popitem(last=False)
        return True

    def forget(self, person: str) -> None:
        """Let `person` be answered again, e.g. after the reply could not be sent."""
        self._told.pop(person, None)


def cold_dm_text(platform: ChatPlatform, *, dms_on: bool | None) -> str:
    """The two-line answer to a private message that has no conversation to continue.

    `dms_on` is whether the person's space has private chats on; None when the
    platform cannot tell which space they mean (a Discord DM names no server)."""
    words = words_for(platform)
    command = words.commands["dm_me"].word
    if dms_on:
        return (
            "I answer in channels first.\n"
            f"Mention me in a channel, or run `{command}` in one to continue here."
        )
    if dms_on is None:
        return (
            "I can't start a private chat from here.\n"
            f"Mention me in a {words.space} channel, or run `{command}` there. "
            f"If it says private chats are off, ask {words.admin} to run `{command} enable`."
        )
    return (
        "Private chats with Daimon are off here.\n"
        f"Mention me in a channel, or ask {words.admin} to run `{command} enable`."
    )


def dm_permission_text(platform: ChatPlatform) -> str:
    """The answer to a member whose server refused them private chats for lack of access.

    Sent only after their membership was verified, so it tells them nothing about
    a server they are not in."""
    words = words_for(platform)
    return (
        "You don't have permission to DM Daimon.\n"
        f"Ask {words.admin} for access if you think you should have it."
    )


def bounded_turns(
    turns: Sequence[TranscriptTurn], cap: DmHistoryCap = DEFAULT_DM_HISTORY
) -> list[TranscriptTurn]:
    """Keep a bounded text-only context, newest selected, oldest rendered first."""
    remaining = cap.chars
    selected: list[TranscriptTurn] = []
    for turn in reversed(turns[-cap.turns :]):
        if remaining <= 0:
            break
        text = turn.text[:remaining]
        selected.append(TranscriptTurn(role=turn.role, text=text))
        remaining -= len(text)
    return list(reversed(selected))


SEALED_SOURCE_MESSAGE = (
    "Only conversations inside this channel can read it, so this one can't move to a DM. "
    "Keep working here."
)


SHARED_SOURCE_MESSAGE = (
    "This channel is shared with another organisation, so it can't move to a DM. Keep working here."
)


SEALED_SINCE_MESSAGE = (
    "This DM was closed because its channel no longer lets its conversation move to a DM."
    "\n\nKeep working in that channel."
)


async def sealed_channel_ids(deps: TurnDeps, *, tenant_id: uuid.UUID) -> frozenset[str]:
    """The tenant's current seal list, for filtering history a /dm-me move copies."""
    async with deps.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    return limited_ids(policy)


def is_sealed_slack_message(
    sealed: frozenset[str], *, channel_id: str, message: Mapping[str, object]
) -> bool:
    """A sealed Slack thread's root or broadcast reply seen in its channel's history.

    Both carry the thread's ``thread_ts`` (a root's is its own ts), the same
    predicate the MCP read tools use to withhold them.
    """
    thread_ts = message.get("thread_ts") or message.get("ts")
    return f"{channel_id}:{thread_ts}" in sealed


def _source_is_sealed(policy: TenantAccessPolicy, conversation: DirectMessageRow) -> bool:
    """Whether any recorded source of this conversation is sealed now (fails closed)."""
    return not conversation.is_cold and dm_source_limited(
        policy,
        source_channel_id=conversation.source_channel_id,
        source_thread_id=conversation.source_thread_id,
        source_thread_keys=tuple(conversation.source_thread_keys or ()),
    )


async def _quarantine(deps: TurnDeps, conversation: DirectMessageRow) -> None:
    """Delete the conversation and retire its sessions, then refuse with the sealed copy."""
    async with deps.sessionmaker.begin() as session:
        retired = await quarantine_conversation(session, conversation=conversation)
    for session_id in retired:
        with suppress(anthropic.APIError):
            scope = resource_scope(
                tenant_id=str(conversation.tenant_id),
                account_id=str(conversation.account_id),
                authorization_id="sealed-dm-retirement",
            )
            ref = (
                deps.backend_session_ref(session_id, scope)
                if deps.backend_session_ref is not None
                else None
            )
            await turn_io(
                deps.anthropic,
                session_id,
                backend=deps.backend,
                scope=scope,
                session_ref=ref,
            ).archive()
    raise DirectMessageAccessRevoked("source_sealed", message=SEALED_SINCE_MESSAGE)


async def _require_source_still_unsealed(deps: TurnDeps, conversation: DirectMessageRow) -> None:
    """Quarantine the conversation when its source has been sealed since.

    Deletes the row and retires the scope's sessions; a fresh /dm-me is required.
    """
    async with deps.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=conversation.tenant_id)
    if not _source_is_sealed(policy, conversation):
        return
    await _quarantine(deps, conversation)


def require_unsealed_source(admission: Admission) -> None:
    """Refuse /dm-me from a sealed channel or thread before any history is read.

    A DM sits outside the seal: copying the channel's recent messages into it
    would carry sealed content into a conversation that can reach unsealed
    channels. A channel shared with another organisation is refused the same way.
    """
    if admission.source_sealed:
        raise UserFacingError(SEALED_SOURCE_MESSAGE)
    if admission.source_shared:
        raise UserFacingError(SHARED_SOURCE_MESSAGE)


async def start_dm(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    workspace_id: str,
    route_key: str,
    channel_id: str,
    external_user_id: str,
    source_url: str,
    source_channel_id: str,
    source_thread_id: str | None,
    context: Sequence[TranscriptTurn],
    source_thread_keys: Sequence[str] = (),
    groups: frozenset[str] | None = None,
) -> DirectMessageRow:
    """Select a workspace explicitly and give the DM a new thread-like scope.

    Caller admits the source (is_dm=True, `dm_source_channel_id`) and calls
    `require_unsealed_source` before reading history. The DM bills
    `source_channel_id`; each move resets the scope and replays no prior history.
    `groups` are the person's looked-up groups (`lookup_dm_groups`), if the platform has them.
    """
    require_unsealed_source(admission)
    await require_dm_enabled(deps, tenant_id=tenant_id)
    async with deps.sessionmaker() as session:
        permitted = await permits_dm(
            session,
            deps.settings,
            await load_dm_access_policy(session, tenant_id=tenant_id),
            tenant_id=tenant_id,
            platform=platform,
            agent=dm_agent_of(admission.agent, admission.config.agent_name),
            subject=admission.grant.subject
            if admission.grant is not None
            else build_subject(is_admin=False, platform_user_id=external_user_id),
            groups=groups,
        )
    if not permitted:
        raise UserFacingError(DM_AGENT_REFUSED)
    scope_id = f"{DM_SCOPE_PREFIX}{uuid.uuid4()}"
    conversation = DirectMessageRow(
        platform=platform,
        route_key=route_key,
        external_user_id=external_user_id,
        tenant_id=tenant_id,
        account_id=admission.account_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        scope_id=scope_id,
        source_url=source_url,
        source_channel_id=source_channel_id,
        source_thread_id=source_thread_id,
        source_thread_keys=sorted(set(source_thread_keys)),
        context=render_previous_session(
            [
                TranscriptTurn(role="user", text=f"Source: {source_url}"),
                *bounded_turns(context, admission.dm_history),
            ],
            from_agent_name="source conversation",
        ),
        memory_read_only=admission.memory_read_only,
        history=[],
        recent_message_ids=[],
        claimed_at=None,
    )
    async with deps.sessionmaker.begin() as session:
        await start_conversation(session, conversation=conversation)
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
            tenant_id=tenant_id,
            agent_name=admission.config.agent_name,
            environment_name=admission.config.environment_name,
        )
        await create_binding(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=channel_id,
            thread_id=scope_id,
            responder_ma_agent_id=admission.agent.id,
            responder_name=admission.agent.name,
            creator_account_id=admission.account_id,
            kind="handoff",
        )
    return conversation


async def admit_cold_dm(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    external_user_id: str,
    channel_id: str,
    role: Role,
    role_ids: tuple[str, ...],
    verified_membership: bool | None,
    agent_name: str | None = None,
    platform: str = "discord",
    requester_org: RequesterOrg = RequesterOrg.HOST,
) -> Admission:
    """Check a cold agent without creating one or changing channel routing.

    `agent_name` is the agent routing chose; without one the workspace default answers.
    """
    await require_dm_enabled(deps, tenant_id=tenant_id)
    async with deps.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
        name = agent_name or await get_dm_agent_name(session, tenant_id=tenant_id)
    if not tenant_ready(tenant, platform=platform) or name is None:
        raise AdmissionDenied(reason="invoker_not_allowed")
    return await admit(
        deps,
        origin="chat",
        tenant_id=tenant_id,
        requester_org=requester_org,
        platform=platform,
        external_user_id=external_user_id,
        channel_id=channel_id,
        now=datetime.now(UTC),
        role=role,
        role_ids=role_ids,
        verified_membership=verified_membership,
        is_dm=True,
        dm_agent_name=name,
    )


async def start_cold_dm(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    workspace_id: str,
    route_key: str,
    external_user_id: str,
    expected_scope_id: str | None = None,
    remember_pick: bool = False,
    platform: str = "discord",
    groups: frozenset[str] | None = None,
    channel_id: str | None = None,
) -> DirectMessageRow | None:
    """Create a source-free private scope, compare-and-setting the active route.

    A concurrent first message must reuse the winning conversation. An explicit
    switch names the scope it replaces, so a stale control cannot replace a
    newer selection. No configuration is written to the physical DM channel.
    The agent must still admit the person when the route is set; `remember_pick`
    saves it as their agent in this workspace (they chose it from a list). `groups` are
    the person's looked-up groups (`lookup_dm_groups`); None takes them from the admission.
    `channel_id` is the platform's DM channel when the route key is not it (Slack).
    """
    conversation = DirectMessageRow(
        platform=platform,
        route_key=route_key,
        external_user_id=external_user_id,
        tenant_id=tenant_id,
        account_id=admission.account_id,
        workspace_id=workspace_id,
        channel_id=channel_id or route_key,
        scope_id=f"{DM_SCOPE_PREFIX}{uuid.uuid4()}",
        source_url=COLD_DM_ORIGIN,
        context="",
        memory_read_only=admission.memory_read_only,
        history=[],
        recent_message_ids=[],
        claimed_at=None,
    )
    async with deps.sessionmaker.begin() as session:
        await lock_route(
            session, platform=platform, route_key=route_key, external_user_id=external_user_id
        )
        current = await get_conversation(
            session, platform=platform, route_key=route_key, external_user_id=external_user_id
        )
        if (current.scope_id if current else None) != expected_scope_id:
            return None
        dm_agent = dm_agent_of(admission.agent, admission.config.agent_name)
        if not await dm_enabled(session, tenant_id=tenant_id) or not await permits_dm(
            session,
            deps.settings,
            await load_dm_access_policy(session, tenant_id=tenant_id),
            tenant_id=tenant_id,
            platform=platform,
            agent=dm_agent,
            subject=admission.grant.subject
            if admission.grant is not None
            else build_subject(is_admin=False, platform_user_id=external_user_id),
            groups=groups,
        ):
            raise AdmissionDenied(reason="invoker_not_allowed")
        # An imported earlier conversation is used once, by the first private start on
        # the agent it was imported for: with several DM agents, a start on another
        # agent must not use it up.
        carried = await take_carry_over(
            session,
            tenant_id=tenant_id,
            platform=conversation.platform,
            external_user_id=external_user_id,
            agent_name=dm_agent.name,
        )
        if carried is not None:
            conversation = conversation.model_copy(
                update={"history": carried.history, "context": carried.context}
            )
        if remember_pick:
            await set_pick(
                session,
                tenant_id=tenant_id,
                account_id=admission.account_id,
                agent_name=dm_agent.name,
                now=datetime.now(UTC),
            )
        await start_conversation(session, conversation=conversation)
        if current is not None:
            # Retired mappings can never be reused by a later private turn.
            await retire_dm_scope(
                session,
                tenant_id=current.tenant_id,
                platform=current.platform,
                scope_id=current.scope_id,
            )
        await create_binding(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=conversation.channel_id,
            thread_id=conversation.scope_id,
            responder_ma_agent_id=admission.agent.id,
            responder_name=admission.agent.name,
            creator_account_id=admission.account_id,
            kind="handoff",
        )
    return conversation


#: Builds the progress card for an admitted DM turn from its admission and the
#: turn's cancel event (the card's Stop button sets it).
DmTurnCard = Callable[[Admission, asyncio.Event], TurnLifecycle]


#: Shows the monitoring notice on a DM path that renders no card (`card_for`).
TellMonitored = Callable[[MonitoringNotice], Awaitable[None]]


class _CollectReply(TurnLifecycle):
    def __init__(self, tell: TellMonitored | None = None) -> None:
        self._tell = tell

    async def on_monitoring_notice(self, notice: MonitoringNotice) -> bool:
        if self._tell is None:
            return False
        await self._tell(notice)
        return True

    async def on_render(self, state: TurnState) -> None:
        pass

    async def on_terminal_success(self, state: TurnState) -> None:
        pass

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        pass


@asynccontextmanager
async def _dm_destination(
    deps: TurnDeps, conversation: DirectMessageRow, context_id: uuid.UUID | None
) -> AsyncIterator[None]:
    """Register the physical Slack destination; delete only this turn's row.

    Only the isolated session credential bearing this signed row ID can use it.
    Other account credentials have no grant, regardless of concurrent callers.
    """
    if conversation.platform != "slack":
        yield
        return
    async with deps.sessionmaker.begin() as session:
        context = await create_slack_turn_context(
            session,
            tenant_id=conversation.tenant_id,
            account_id=conversation.account_id,
            channel_id=conversation.channel_id,
            thread_ts=conversation.scope_id,
            started_at=datetime.now(UTC),
            id=context_id,
        )
    try:
        yield
    finally:
        # Match ordinary Slack turns: stale rows fail closed and age out by TTL.
        with suppress(SQLAlchemyError):
            async with deps.sessionmaker.begin() as session:
                await delete_slack_turn_context(session, id=context.id)


async def _reseed_prompt(context: str, prefix: Callable[[], Awaitable[str]], text: str) -> str:
    part = await prefix()
    return f"{context}\n\n{part}\n{text}" if part else f"{context}\n\n{text}"


@dataclass(frozen=True)
class _DmTurn:
    """A finished private turn: its text, and the session and mapping that answered."""

    text: str
    session_id: str
    mapping_id: uuid.UUID | None
    outputs: OutputRuntime | None


async def _run_admitted_dm_turn(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    scope_id: str,
    external_user_id: str,
    role: Role,
    prompt: str,
    deadline: datetime,
    on_sealed: Callable[[], Awaitable[None]],
    destination: AbstractAsyncContextManager[None] | None = None,
    origin: TurnContext = "chat",
    watch_row: TaskContinuationRow | None = None,
    confirm_write: ConfirmationHook | None = None,
    card_for: DmTurnCard | None = None,
    request_text: str | None = None,
    request_ref: str | None = None,
    platform_channel_id: str | None = None,
    platform_message_id: str | None = None,
    tell: TellMonitored | None = None,
    image_blocks: Sequence[BetaManagedAgentsImageBlockParam] | None = None,
    reseed_prompt: Callable[[], Awaitable[str]] | None = None,
) -> _DmTurn:
    """Run one admitted, bound private turn and return its final text and session.

    Shared by live DM replies, DM routines and DM watch reports (`watch_row`,
    which runs under watch posture). `on_sealed` runs when the DM's source was
    sealed during the turn (it may raise its own refusal). Without `card_for`
    nothing is rendered while the turn runs and the caller delivers the answer;
    with it, the adapter's lifecycle shows progress and may deliver the answer
    itself. A recovery after a dead session renders nothing either way. Without
    `confirm_write` a write that needs approval is refused, as on any surface with
    no card. `request_text` is the person's own words, for the turn's MCP context, and
    `request_ref` their message's id; `platform_channel_id` and `platform_message_id`
    are recorded on the turn's origin.
    """
    async with (
        destination if destination is not None else nullcontext(),
        turn_origin(
            deps.sessionmaker,
            tenant_id=tenant_id,
            account_id=prepared.admission.account_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=scope_id,
            responder_ma_agent_id=prepared.admission.agent.id,
            responder_name=prepared.admission.agent.name,
            role=Role.ADMIN if prepared.admission.is_platform_admin else Role.USER,
            attended=origin != "routine",
            sender_foreign=prepared.admission.hold is not None and prepared.admission.hold.client,
            shared_channel=prepared.admission.shared_channel,
            platform_channel_id=platform_channel_id,
            platform_message_id=platform_message_id,
        ) as turn_origin_row,
    ):
        controls = render_turn_origin(
            turn_origin_row,
            effective_is_admin=prepared.admission.is_admin,
            web=prepared.admission.web,
            features=prepared.admission.features,
        )
        user_message = f"{controls}\n{prompt}"

        async def reseed() -> str:
            # A recovery rebuilds the prompt, without what ended the dead session.
            return f"{controls}\n{await reseed_prompt()}" if reseed_prompt else user_message

        cancel = asyncio.Event()
        lifecycle = (
            card_for(prepared.admission, cancel) if card_for is not None else _CollectReply(tell)
        )
        try:
            outcome = await run_prepared_turn(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=scope_id,
                external_user_id=external_user_id,
                user_message=user_message,
                request_text=None if watch_row is not None else request_text,
                request_ref=request_ref,
                continues=watch_row,
                image_blocks=image_blocks,
                lifecycle=lifecycle,
                cancel=cancel,
                reseed_user_message=reseed,
                recovery_lifecycle=lambda _: _CollectReply(),
                origin=origin,
                deadline=deadline,
                attended=False if watch_row is not None else None,
                watch_row=watch_row,
                confirm_write=confirm_write,
            )
        except DmSourceSealedError:
            # Decided again during a dead-session recovery.
            await on_sealed()
            raise
    if isinstance(outcome.state.error, DmSourceSealedError):
        await on_sealed()
    if outcome.state.error is not None:
        raise outcome.state.error
    if origin == "routine" and outcome.state.termination != TerminationReason.COMPLETED:
        raise DirectMessageAccessRevoked(
            "execution_incomplete", message="The run did not complete."
        )
    answer = extract_final_response(outcome.state.content) or "The reply finished without text."
    return _DmTurn(answer, outcome.ma_session_id, outcome.mapping_id, outcome.outputs)


async def reply_to_dm(
    deps: TurnDeps,
    *,
    platform: str,
    route_key: str,
    external_user_id: str,
    message_id: str,
    expected_scope_id: str,
    text: str,
    role: Role,
    role_ids: tuple[str, ...] = (),
    merged_message_ids: Sequence[str] = (),
    verified_membership: bool | None = None,
    on_agent: Callable[[str], Awaitable[None]] | None = None,
    platform_channel_id: str | None = None,
    tell: TellMonitored | None = None,
    groups: frozenset[str] | None = None,
    requester_org: RequesterOrg = RequesterOrg.HOST,
) -> str | None:
    """Compatibility text API for adapters that do not consume a delivery grant."""
    reply = await reply_to_dm_result(
        deps,
        requester_org=requester_org,
        platform=platform,
        route_key=route_key,
        external_user_id=external_user_id,
        message_id=message_id,
        merged_message_ids=merged_message_ids,
        expected_scope_id=expected_scope_id,
        text=text,
        role=role,
        role_ids=role_ids,
        verified_membership=verified_membership,
        on_agent=on_agent,
        platform_channel_id=platform_channel_id,
        tell=tell,
        groups=groups,
    )
    return reply.text if reply is not None else None


async def reply_to_dm_result(
    deps: TurnDeps,
    *,
    platform: str,
    route_key: str,
    external_user_id: str,
    message_id: str,
    expected_scope_id: str,
    text: str,
    role: Role,
    role_ids: tuple[str, ...] = (),
    merged_message_ids: Sequence[str] = (),
    prefix: str = "",
    reseed_prefix: Callable[[], Awaitable[str]] | None = None,
    image_blocks: Sequence[BetaManagedAgentsImageBlockParam] | None = None,
    history_notes: Sequence[str] = (),
    verified_membership: bool | None = None,
    on_agent: Callable[[str], Awaitable[None]] | None = None,
    confirmation_for: Callable[[Admission], ConfirmationHook] | None = None,
    card_for: Callable[[DirectMessageRow], DmTurnCard] | None = None,
    platform_channel_id: str | None = None,
    tell: TellMonitored | None = None,
    groups: frozenset[str] | None = None,
    requester_org: RequesterOrg = RequesterOrg.HOST,
) -> DirectMessageReply | None:
    """Admit every DM, serialize its scope, run a billed turn, retain bounded history.

    `platform_channel_id` is the DM channel as the platform's API names it, recorded
    on the turn's origin with `message_id`. A turn that merges several messages
    names the others in `merged_message_ids`.

    `text` is the person's own words. `prefix` is what the adapter adds before it
    (attachment lines, quoted or forwarded content) and `image_blocks` are inline
    images; both go to this turn only. The stored history keeps `text` and
    `history_notes` (such as `[attachment: name]`), never the prefix, whose
    signed links expire. `reseed_prefix` builds the prefix again when a dead
    session is rebuilt, so it can leave out what ended that session (an
    oversized image's link).

    A duplicate delivery returns None. The adapter verifies live membership
    and supplies the current role before entering; stored admin roles never
    authorize a private turn. Platform API errors must fail that check closed.
    `confirmation_for` builds the approval-card hook for the admitted turn; an
    adapter that cannot show a card in a DM omits it and approvals are refused.
    `card_for` builds the turn's progress card for the claimed conversation; the
    card must recheck delivery for it before each write, as the adapter does before
    sending the answer. A turn whose fence drops raises `DirectMessageClaimLost`.
    Off Discord, the bound agent's DM grant and locks are rechecked here with
    `groups` (`lookup_dm_groups`); Discord rechecks them in `require_dm_delivery`.
    Without a card, `tell` shows the monitoring notice
    (`daimon.core.monitoring.must_tell`).
    """
    async with owner_dm_turn(deps, platform=platform, owner=external_user_id, wait=True) as fence:
        now = datetime.now(UTC)
        async with deps.sessionmaker.begin() as session:
            conversation = await claim_message(
                session,
                fence=fence,
                route_key=route_key,
                message_ids=(message_id, *merged_message_ids),
                expected_scope_id=expected_scope_id,
                # Old images read this as a deadline: keep it live for them, and unique per claim.
                claimed_at=now + timedelta(seconds=LONGEST_TURN_CEILING_S + 60),
            )
        if conversation is None:
            return None
        history: list[dict[str, str]] | None = None
        answered: uuid.UUID | None = None
        bound = False
        try:
            async with deps.sessionmaker() as session:
                if not await dm_enabled(session, tenant_id=conversation.tenant_id):
                    raise DirectMessageAccessRevoked(
                        "dm_disabled",
                        message="Daimon DMs are off here.\n\nAsk an admin to run /dm-me enable.",
                    )
            async with deps.sessionmaker() as session:
                tenant = await get_tenant(session, conversation.tenant_id)
            if not tenant_ready(tenant):
                raise DirectMessageAccessRevoked(
                    "tenant_unavailable",
                    message="This workspace is not available. No DM turn was started.",
                )
            await _require_source_still_unsealed(deps, conversation)
            admission = await admit(
                deps,
                tenant_id=conversation.tenant_id,
                requester_org=requester_org,
                platform=platform,
                external_user_id=external_user_id,
                origin="chat",
                channel_id=conversation.channel_id,
                thread_id=conversation.scope_id,
                role=role,
                role_ids=role_ids,
                verified_membership=verified_membership,
                is_dm=True,
                dm_source_channel_id=conversation.source_channel_id,
                cold_dm=conversation.is_cold,
                now=now,
            )
            if admission.account_id != conversation.account_id:
                raise DirectMessageAccessRevoked(
                    "account_changed",
                    message=(
                        "Your account changed, so this DM can't continue.\n\n"
                        "Run /dm-me again in the channel you came from."
                    ),
                )
            if platform != "discord":
                await require_dm_turn_grant(
                    deps,
                    admission,
                    tenant_id=conversation.tenant_id,
                    platform=conversation.platform,
                    user_id=conversation.external_user_id,
                    groups=groups,
                )
            if on_agent is not None:
                await on_agent(admission.agent.name)
            execution_id = uuid.uuid4() if platform == "slack" else None
            admission = replace(
                admission,
                memory_read_only=admission.memory_read_only or conversation.memory_read_only,
                slack_turn_context_id=execution_id,
                private_dm_id=str(execution_id) if execution_id else conversation.scope_id,
                # Re-asked by `reauthorize` right before the DM's session is built.
                grant=replace(
                    admission.grant,
                    dm_source=None
                    if conversation.is_cold
                    else DmSource(
                        channel_id=conversation.source_channel_id,
                        thread_id=conversation.source_thread_id,
                        thread_keys=tuple(conversation.source_thread_keys or ()),
                    ),
                )
                if admission.grant is not None
                else None,
            )
            deadline = turn_deadline(now=now, ceiling_s=admission.turn_ceiling_s)
            try:
                prepared = await bind_session(
                    deps,
                    admission,
                    tenant_id=conversation.tenant_id,
                    platform=platform,
                    external_user_id=external_user_id,
                    thread_id=conversation.scope_id,
                    session_account_id=admission.account_id,
                    # Never attach a later Slack turn to a prior turn's bearer credential.
                    reuse_existing=platform != "slack",
                    deadline=deadline,
                )
            except DmSourceSealedError:
                # The source was sealed after admission: same quarantine as above.
                await _quarantine(deps, conversation)
                raise
            bound = True
            previous = [
                TranscriptTurn(
                    role="user" if item["role"] == "user" else "agent", text=item["text"]
                )
                for item in conversation.history
            ]
            context = (
                conversation.context
                + "\n"
                + render_previous_session(
                    bounded_turns(previous, prepared.admission.dm_history),
                    from_agent_name="private conversation",
                )
            )
            whole = f"{context}\n\n{prefix}\n{text}" if prefix else f"{context}\n\n{text}"
            # A session that answered a turn here (its watermark) holds the earlier turns;
            # a new or replaced one, or one whose first turn never got through, has seen
            # none, and a recovery rebuilds `whole` for the same reason.
            holds_history = (
                prepared.reused
                and prepared.watermark is not None
                and prepared.continuity.state in ("continued", "updated")
            )
            sent = (f"{prefix}\n{text}" if prefix else text) if holds_history else whole

            async def reseed_whole() -> str:
                return whole

            done = await _run_admitted_dm_turn(
                deps,
                prepared,
                tenant_id=conversation.tenant_id,
                platform=platform,
                parent_channel_id=conversation.channel_id,
                scope_id=conversation.scope_id,
                external_user_id=external_user_id,
                role=role,
                prompt=sent,
                deadline=deadline,
                destination=_dm_destination(deps, conversation, admission.slack_turn_context_id),
                on_sealed=lambda: _quarantine(deps, conversation),
                confirm_write=(
                    confirmation_for(prepared.admission) if confirmation_for is not None else None
                ),
                tell=tell,
                card_for=card_for(conversation) if card_for is not None else None,
                request_text=text,
                request_ref=message_id,
                platform_channel_id=platform_channel_id,
                platform_message_id=message_id if platform_channel_id is not None else None,
                image_blocks=image_blocks,
                reseed_prompt=(
                    reseed_whole
                    if reseed_prefix is None
                    else lambda: _reseed_prompt(context, reseed_prefix, text)
                ),
            )
            turns = bounded_turns(
                [
                    *previous,
                    TranscriptTurn(role="user", text="\n".join([text, *history_notes]).strip()),
                    TranscriptTurn(role="agent", text=done.text),
                ],
                prepared.admission.dm_history,
            )
            history = [{"role": turn.role, "text": turn.text} for turn in turns]
            answered = done.mapping_id
            grant = prepared.admission.grant
            if grant is None:
                raise DirectMessageAccessRevoked(
                    "authorization_missing", message="Private authority unavailable."
                )
            revision = prepared.admission.backend_revision
            reply = DirectMessageReply(
                text=done.text,
                conversation=conversation.model_copy(update={"claimed_at": None}),
                grant=grant,
                outputs=None
                if revision is not None and revision.backend == "selfhosted"
                else done.outputs,
            )
        except (BoundAgentUnavailable, AccessPolicyUnreadable, MAResolverMissError) as error:
            raise _revoked(error) from error
        except asyncio.CancelledError:
            if bound and (deps.running_turns.is_drain_cancel() or fence.lost.is_set()):
                await _settle_cancelled_dm(deps, conversation)
            raise
        finally:
            async with deps.sessionmaker.begin() as session:
                finished = await finish_message(session, conversation=conversation, history=history)
                if finished and history is not None and answered is not None:
                    # The session now holds this conversation; its next turn sends only new words.
                    await update_watermark(session, id=answered, watermark_message_id=message_id)
        if not finished:
            # A later message took the claim over: this turn's fence dropped mid-turn.
            raise DirectMessageClaimLost()
        return reply


@dataclass(frozen=True)
class DmRoutineReply:
    """A DM routine's final text, the session and the agent that produced it."""

    text: str
    session_id: str
    producing_agent_id: str


#: Recorded on the routine when its agent runs on another provider.
MANAGED_AGENTS_ONLY = "DM routines need a Managed Agents agent"


async def run_dm_routine(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    owner_user_id: str,
    agent_name: str,
    prompt: str,
    source: DmSource | None,
    budget_channel_id: str | None,
    sandbox: RoutineSandbox | None = None,
    requester_org: RequesterOrg = RequesterOrg.HOST,
) -> DmRoutineReply:
    """Run a DM routine's fire as a fresh, unattended private DM turn of its owner.

    Ordinary cold-DM admission and session (personal memory included), in a new
    scope with no history. Every session is gated, so a call needing Approve is
    refused; only Managed Agents agents run (`MANAGED_AGENTS_ONLY`). `source` is
    rechecked for a seal whenever the session is built; `budget_channel_id` only
    bills. Nothing is posted here: the caller queues the final output. Built-ins stay
    off unless `sandbox` is an operator's agent-level opt-in the session matches.
    """
    token = dm_routine_sandbox.set(sandbox)
    try:
        return await _run_dm_fire(
            deps,
            requester_org=requester_org,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            agent_name=agent_name,
            prompt=prompt,
            source=source,
            budget_channel_id=budget_channel_id,
        )
    finally:
        dm_routine_sandbox.reset(token)


async def run_dm_watch(
    deps: TurnDeps,
    row: TaskContinuationRow,
    *,
    owner_user_id: str,
    prompt: str,
    source: DmSource,
    budget_channel_id: str,
    requester_org: RequesterOrg = RequesterOrg.HOST,
) -> DmRoutineReply:
    """Run a DM watch's fire as a fresh private DM turn of its owner, under watch posture.

    The same cold-DM admission, `dm_enabled` and Managed-Agents-only rules as a DM
    routine, serialised per owner the same way. The turn reads and reports only
    (`watch_turn.watch_posture`), never routine posture. The fresh session is
    recorded on the firing before anything is sent, and each send checks it.
    `source` is the place the watch was made; nothing is posted here. While the
    owner has a DM turn running, which may be waiting on an approval card, it
    raises `SessionBusyError` so the report is offered again later.
    """
    if row.reason != "watch_report" or row.requester_external_user_id != owner_user_id:
        raise DirectMessageAccessRevoked("invoker_not_allowed", message="Watch owner changed.")
    try:
        return await _run_dm_fire(
            deps,
            requester_org=requester_org,
            tenant_id=row.tenant_id,
            owner_user_id=owner_user_id,
            agent_name=row.target_name,
            prompt=prompt,
            source=source,
            budget_channel_id=budget_channel_id,
            watch_row=row,
        )
    except DirectMessageAccessRevoked as error:
        if error.reason != "owner_busy":
            raise
        raise SessionBusyError(
            pending_reasons=("watch_report",), retry_after=datetime.now(UTC)
        ) from error


async def run_dm_timer(
    deps: TurnDeps,
    row: TaskContinuationRow,
    *,
    prompt: str,
    requester_org: RequesterOrg = RequesterOrg.HOST,
) -> DmRoutineReply:
    """Run a due DM timer as a fresh private DM turn of its owner.

    The same admission and Managed-Agents-only rules as a DM routine, in a new
    scope with no history. Nothing is posted here. While the owner has a DM turn
    running it raises `SessionBusyError` so the timer is offered again later.
    """
    if row.reason != "timer":
        raise DirectMessageAccessRevoked("invoker_not_allowed", message="Not a timer.")
    try:
        return await _run_dm_fire(
            deps,
            requester_org=requester_org,
            tenant_id=row.tenant_id,
            owner_user_id=row.requester_external_user_id,
            agent_name=row.target_name,
            prompt=prompt,
            source=None,
            budget_channel_id=None,
            timer_row=row,
        )
    except DirectMessageAccessRevoked as error:
        if error.reason != "owner_busy":
            raise
        raise SessionBusyError(pending_reasons=("timer",), retry_after=datetime.now(UTC)) from error


async def run_channel_timer(
    deps: TurnDeps,
    row: TaskContinuationRow,
    *,
    prompt: str,
    role: Role,
    role_ids: tuple[str, ...] = (),
    category_id: str | None = None,
    category_unresolved: bool = False,
    requester_org: RequesterOrg = RequesterOrg.HOST,
) -> DmRoutineReply:
    """Run a due channel timer as a fresh, unattended turn of its owner in that channel.

    The row's parent is the channel; its thread is only a queue scope. The caller has
    proved over REST that the owner still sees the channel and the agent may post there,
    and passes the owner's role. Admission asks again, so a changed agent, a protected
    channel or a lost allowance refuses it. Nothing is posted here.
    """
    if row.reason != "timer":
        raise DirectMessageAccessRevoked("invoker_not_allowed", message="Not a timer.")
    channel_id = row.parent_channel_id
    scope_id = f"{CHANNEL_SCOPE_PREFIX}{uuid.uuid4()}"
    owner = row.requester_external_user_id
    now = datetime.now(UTC)
    async with deps.sessionmaker() as session:
        tenant = await get_tenant(session, row.tenant_id)
    if not tenant_ready(tenant, platform="discord"):
        raise DirectMessageAccessRevoked(
            "tenant_unavailable", message="This workspace is not available."
        )
    try:
        admission = await admit(
            deps,
            tenant_id=row.tenant_id,
            requester_org=requester_org,
            platform="discord",
            external_user_id=owner,
            channel_id=channel_id,
            role=role,
            role_ids=role_ids,
            verified_membership=True,
            origin="routine",
            attended=False,
            now=now,
            category_id=category_id,
            category_unresolved=category_unresolved,
        )
        if admission.agent.id != row.target_ma_agent_id:
            raise DirectMessageAccessRevoked(
                "invoker_not_allowed", message="The timer's agent changed."
            )
        if (
            admission.backend_revision is not None
            and admission.backend_revision.profile != MANAGED_AGENTS.profile_id
        ):
            raise DirectMessageAccessRevoked(MANAGED_AGENTS_ONLY, message=MANAGED_AGENTS_ONLY)
        deadline = turn_deadline(now=now, ceiling_s=admission.turn_ceiling_s)
        prepared = await bind_session(
            deps,
            admission,
            tenant_id=row.tenant_id,
            platform="discord",
            external_user_id=owner,
            thread_id=scope_id,
            session_account_id=admission.account_id,
            reuse_existing=False,
            deadline=deadline,
        )
    except (BoundAgentUnavailable, AccessPolicyUnreadable, MAResolverMissError) as error:
        raise _revoked(error) from error
    if prepared.backend is not None:
        raise DirectMessageAccessRevoked(MANAGED_AGENTS_ONLY, message=MANAGED_AGENTS_ONLY)
    return await _run_unattended_turn(
        deps,
        prepared,
        tenant_id=row.tenant_id,
        parent_channel_id=channel_id,
        scope_id=scope_id,
        owner=owner,
        prompt=prompt,
        deadline=deadline,
        account_id=admission.account_id,
    )


async def _run_dm_fire(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    owner_user_id: str,
    agent_name: str,
    prompt: str,
    source: DmSource | None,
    budget_channel_id: str | None,
    watch_row: TaskContinuationRow | None = None,
    timer_row: TaskContinuationRow | None = None,
    requester_org: RequesterOrg = RequesterOrg.HOST,
) -> DmRoutineReply:
    async with owner_dm_turn(deps, platform="discord", owner=owner_user_id, wait=False) as fence:
        now = datetime.now(UTC)
        async with deps.sessionmaker() as session:
            enabled = await dm_enabled(session, tenant_id=tenant_id)
            tenant = await get_tenant(session, tenant_id)
        if not enabled:
            raise DirectMessageAccessRevoked("dm_disabled", message="Daimon DMs are off here.")
        if not tenant_ready(tenant, platform="discord"):
            raise DirectMessageAccessRevoked(
                "tenant_unavailable", message="This workspace is not available."
            )
        scope_id = f"{DM_SCOPE_PREFIX}{uuid.uuid4()}"
        try:
            admission = await admit(
                deps,
                tenant_id=tenant_id,
                requester_org=requester_org,
                platform="discord",
                external_user_id=owner_user_id,
                origin="routine" if watch_row is None else watch_row.reason,
                attended=False,
                channel_id=scope_id,
                now=now,
                is_dm=True,
                dm_source_channel_id=budget_channel_id,
                dm_agent_name=agent_name,
                cold_dm=True,
            )
            if (
                admission.backend_revision is not None
                and admission.backend_revision.profile != MANAGED_AGENTS.profile_id
            ):
                raise DirectMessageAccessRevoked(MANAGED_AGENTS_ONLY, message=MANAGED_AGENTS_ONLY)
            if admission.grant is None:
                raise DirectMessageAccessRevoked(
                    "authorization_missing", message="Private authority unavailable."
                )
            admission = replace(
                admission,
                private_dm_id=scope_id,
                # Re-asked by `reauthorize` right before the session is built.
                grant=replace(admission.grant, dm_source=source, act_for_creator=True),
            )
            if deps.backend is not None:
                from mux.drivers.anthropic import (
                    SESSION_READS_NAMESPACE,
                    SessionReads,
                )
                from mux.errors import UnsupportedCapability

                try:
                    deps.backend.extension(
                        SessionReads, namespace=SESSION_READS_NAMESPACE, version=1
                    )
                except UnsupportedCapability as error:
                    raise DirectMessageAccessRevoked(
                        MANAGED_AGENTS_ONLY,
                        message="DM routines require a gateable Managed Agents runtime.",
                    ) from error
            deadline = turn_deadline(now=now, ceiling_s=admission.turn_ceiling_s)
            prepared = await bind_session(
                deps,
                admission,
                tenant_id=tenant_id,
                platform="discord",
                external_user_id=owner_user_id,
                thread_id=scope_id,
                session_account_id=admission.account_id,
                reuse_existing=False,
                deadline=deadline,
            )
        except DmSourceSealedError as error:
            raise DirectMessageAccessRevoked(
                "source_sealed", message=SEALED_SINCE_MESSAGE
            ) from error
        except (BoundAgentUnavailable, AccessPolicyUnreadable, MAResolverMissError) as error:
            raise _revoked(error) from error
        grant = prepared.admission.grant
        if prepared.backend is not None or grant is None:
            raise DirectMessageAccessRevoked(MANAGED_AGENTS_ONLY, message=MANAGED_AGENTS_ONLY)
        if timer_row is not None and prepared.admission.agent.id != timer_row.target_ma_agent_id:
            raise DirectMessageAccessRevoked(
                "invoker_not_allowed", message="The timer's agent changed."
            )
        if watch_row is not None:
            from daimon.core.stores.watches import record_delivery_session

            if prepared.admission.agent.id != watch_row.target_ma_agent_id:
                raise DirectMessageAccessRevoked(
                    "invoker_not_allowed", message="The watch's agent changed."
                )
            async with deps.sessionmaker.begin() as session:
                recorded = await record_delivery_session(
                    session, watch_row.idempotency_key, prepared.ma_session_id
                )
            if not recorded:
                raise DirectMessageAccessRevoked(
                    "invoker_not_allowed", message="This report already ran."
                )

        return await _run_unattended_turn(
            deps,
            prepared,
            tenant_id=tenant_id,
            parent_channel_id=scope_id,
            scope_id=scope_id,
            owner=owner_user_id,
            prompt=prompt,
            deadline=deadline,
            account_id=admission.account_id,
            watch_row=watch_row,
            fence=fence,
        )


async def _run_unattended_turn(
    deps: TurnDeps,
    prepared: PreparedTurn,
    *,
    tenant_id: uuid.UUID,
    parent_channel_id: str,
    scope_id: str,
    owner: str,
    prompt: str,
    deadline: datetime,
    account_id: uuid.UUID,
    watch_row: TaskContinuationRow | None = None,
    fence: OwnerDmFence | None = None,
) -> DmRoutineReply:
    """Run an admitted unattended fire in its fresh scope; a drain or lost fence settles it."""

    async def sealed() -> None:
        raise DirectMessageAccessRevoked("source_sealed", message=SEALED_SINCE_MESSAGE)

    try:
        done = await _run_admitted_dm_turn(
            deps,
            prepared,
            tenant_id=tenant_id,
            platform="discord",
            parent_channel_id=parent_channel_id,
            scope_id=scope_id,
            external_user_id=owner,
            role=Role.ADMIN if prepared.admission.is_platform_admin else Role.USER,
            prompt=prompt,
            deadline=deadline,
            on_sealed=sealed,
            origin="routine",
            watch_row=watch_row,
        )
    except asyncio.CancelledError:
        if deps.running_turns.is_drain_cancel() or (fence is not None and fence.lost.is_set()):
            await _settle_cancelled_dm_scope(
                deps,
                tenant_id=tenant_id,
                platform="discord",
                scope_id=scope_id,
                account_id=account_id,
            )
        raise
    return DmRoutineReply(
        text=done.text,
        session_id=done.session_id,
        producing_agent_id=prepared.admission.agent.id,
    )


def _revoked(
    error: BoundAgentUnavailable | AccessPolicyUnreadable | MAResolverMissError,
) -> DirectMessageAccessRevoked:
    reason = (
        "policy_unreadable" if isinstance(error, AccessPolicyUnreadable) else "agent_unavailable"
    )
    return DirectMessageAccessRevoked(reason, message=str(error))


#: A shutdown drain gives each cancelled turn's cleanup 5 s in total.
_SHUTDOWN_INTERRUPT_TIMEOUT_S = 3.0


async def _settle_cancelled_dm(deps: TurnDeps, conversation: DirectMessageRow) -> None:
    """Interrupt the MA turn a shutdown or a lost fence cancelled, marking it dead if unconfirmed.

    Only while this delivery still holds the conversation's claim, so the live
    mapping is this turn's own. Best effort: a database failure is only logged.
    """
    await _settle_cancelled_dm_scope(
        deps,
        tenant_id=conversation.tenant_id,
        platform=conversation.platform,
        scope_id=conversation.scope_id,
        account_id=conversation.account_id,
        conversation=conversation,
    )


async def _settle_cancelled_dm_scope(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope_id: str,
    account_id: uuid.UUID,
    conversation: DirectMessageRow | None = None,
) -> None:
    """Best-effort settle of a cancelled DM turn, scoped to the claim/owner guard's own mapping."""
    try:
        async with deps.sessionmaker.begin() as session:
            if conversation is not None and not await holds_claim(
                session, conversation=conversation
            ):
                return
            mapping = await get_live_thread_session(
                session,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=scope_id,
                account_id=account_id,
            )
            if mapping is None:
                return
            interrupted = await interrupt_orphaned_session(
                deps.anthropic,
                session_id=mapping.ma_session_id,
                scope=resource_scope(
                    tenant_id=str(tenant_id),
                    account_id=str(account_id),
                    authorization_id="dm-cancelled-turn",
                ),
                timeout_s=_SHUTDOWN_INTERRUPT_TIMEOUT_S,
            )
            if not interrupted:
                await mark_dead(session, id=mapping.id)
            _log.info(
                "dm.cancelled_turn_settled",
                session_id=mapping.ma_session_id,
                interrupted=interrupted,
            )
    except SQLAlchemyError as err:
        _log.warning("dm.cancelled_settle_failed", error_type=type(err).__name__)


async def require_dm_enabled(deps: TurnDeps, *, tenant_id: uuid.UUID) -> None:
    async with deps.sessionmaker() as session:
        enabled = await dm_enabled(session, tenant_id=tenant_id)
    if not enabled:
        raise UserFacingError("Daimon DMs are off here.\n\nAsk an admin to run /dm-me enable.")


#: How often a held owner fence is checked: a turn whose fence dropped stops within it.
_FENCE_CHECK_S = 15.0


async def _watch_fence(deps: TurnDeps, fence: OwnerDmFence, stop: asyncio.Timeout) -> None:
    """Stop the holder's turn once its fence is gone, so a waiting turn never runs beside it."""
    failed_pings = 0
    while True:
        await asyncio.sleep(_FENCE_CHECK_S)
        if fence.db is not None:
            # The fence connection idles in its transaction all turn, so an infrastructure
            # idle drop would end it; a ping keeps it alive and proves it is still up.
            try:
                async with asyncio.timeout(_FENCE_CHECK_S):
                    await fence.db.execute(text("SELECT 1"))
                failed_pings = 0
            except Exception as err:
                # Boundary: a driver reports a dropped connection in many types. One slow or
                # failed ping under load proves little; two in a row mean the hold is unusable.
                failed_pings += 1
                _log.warning(
                    "dm.owner_fence_ping_failed", error_type=type(err).__name__, count=failed_pings
                )
                if failed_pings >= 2:
                    fence.lost.set()
                    stop.reschedule(asyncio.get_running_loop().time())
                    return
        try:
            async with deps.sessionmaker() as session:
                held = await fence_held(session, fence)
        except (SQLAlchemyError, OSError) as err:
            # An unread lock table proves nothing; the turn's own writes need the database too.
            _log.warning("dm.owner_fence_check_failed", error_type=type(err).__name__)
            continue
        if not held:
            _log.warning("dm.owner_fence_lost", platform=fence.platform)
            fence.lost.set()
            stop.reschedule(asyncio.get_running_loop().time())
            return


@asynccontextmanager
async def owner_dm_turn(
    deps: TurnDeps, *, platform: str, owner: str, wait: bool
) -> AsyncIterator[OwnerDmFence]:
    """Serialize local owner turns across tenants and adapter processes; yield the held fence.

    An existing transaction advisory lock is sufficient: no persistent turn
    outcome or promise about remote activity after process loss is introduced.
    Failed tries release their connection before live-DM queue retries. If the
    fence drops while held (its connection lost), the body is cancelled and
    `DirectMessageClaimLost` raised: a waiting turn may already own the owner's DMs.
    """

    async def acquire() -> tuple[AsyncExitStack, OwnerDmFence]:
        stack = AsyncExitStack()
        try:
            bind = deps.sessionmaker.kw.get("bind")
            engine = bind.engine if isinstance(bind, AsyncConnection) else bind
            if not isinstance(engine, AsyncEngine):
                raise RuntimeError("DM owner exclusion requires an async database engine")
            connection = await stack.enter_async_context(engine.connect())
            db = await stack.enter_async_context(AsyncSession(bind=connection))
            await stack.enter_async_context(db.begin())
            # The holder may run for hours; a sender waits at most the standard window.
            await try_fence(
                db,
                owner_dm_fence(platform, owner),
                schema_scoped=True,
                timeout_s=TURN_CEILING_S,
            )
            pid: int = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            # Detach so the lock does not hold a work-pool slot; closing the
            # detached connection releases the lock on every local exit.
            await connection.run_sync(lambda sync: sync.detach())
        except BaseException:
            await stack.aclose()
            raise
        return stack, OwnerDmFence(platform, owner, pid, db)

    if wait:
        stack, fence = await retry_fences(acquire)
    else:
        try:
            stack, fence = await acquire()
        except FenceUnavailable as error:
            raise DirectMessageAccessRevoked(
                "owner_busy",
                message="The owner already has a DM turn running; this slot was skipped.",
            ) from error
    try:
        async with asyncio.timeout(None) as stop:
            watch = asyncio.create_task(_watch_fence(deps, fence, stop), name="dm.owner_fence")
            try:
                yield fence
            finally:
                watch.cancel()
                await asyncio.wait({watch})
    except TimeoutError:
        if not fence.lost.is_set():
            raise
    finally:
        if fence.lost.is_set():
            # Cleanup boundary (guideline:architecture): the connection is already gone, and
            # closing it fails in driver-specific ways or never answers; none of it matters.
            with suppress(Exception):
                async with asyncio.timeout(_FENCE_CHECK_S):
                    await stack.aclose()
        else:
            await stack.aclose()
    if fence.lost.is_set():
        raise DirectMessageClaimLost()

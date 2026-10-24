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
from daimon.core.access_policy import (
    DM_SCOPE_PREFIX,
    TenantAccessPolicy,
)
from daimon.core.authz import Action, Place, Surface, authorize
from daimon.core.config import load_turn_settings
from daimon.core.context_prompt import TurnContext
from daimon.core.errors import UserFacingError
from daimon.core.handoff_context import TranscriptTurn, render_previous_session
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.membership_subject import resolve_subject
from daimon.core.mux_backend import resource_scope
from daimon.core.permissions import dm_source_limited, limited_ids
from daimon.core.platform_words import words_for
from daimon.core.scope import ChannelScopeRef
from daimon.core.session_fence_retry import FenceUnavailable, retry_fences, try_fence
from daimon.core.setup_conversations import BoundAgentUnavailable
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.direct_messages import (
    COLD_DM_ORIGIN,
    DirectMessageRow,
    claim_message,
    dm_enabled,
    finish_message,
    get_conversation,
    get_dm_agent_name,
    holds_claim,
    lock_route,
    quarantine_conversation,
    start_conversation,
)
from daimon.core.stores.domain import ChatPlatform, Role, TenantRow
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    delete_slack_turn_context,
)
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.stores.thread_sessions import (
    get_live_thread_session,
    mark_dead,
    retire_dm_scope,
)
from daimon.core.turn.admission import Admission, AdmissionGrant, DmSource, admit
from daimon.core.turn.ceiling import TURN_CEILING_S, turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied, DmSourceSealedError
from daimon.core.turn.io import turn_io
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import PreparedTurn, bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.turn.termination import TerminationReason
from daimon.core.turn_hold import RequesterOrg
from daimon.core.turn_origin import render_turn_origin, turn_origin
from mux.profiles import MANAGED_AGENTS
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

_log = structlog.get_logger(__name__)


class DirectMessageAccessRevoked(UserFacingError):
    """Private access failed closed; Discord suppresses the reply and logs the reason."""

    def __init__(self, reason: str, *, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DirectMessageReply:
    """Text and the exact agent authority used for this private model turn."""

    text: str
    conversation: DirectMessageRow
    grant: AdmissionGrant


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
        or subject.verified_membership is not True
        or not authorize(policy, subject=subject, action=Action.START_TURN)
    ):
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
            f"Mention me in a channel, or run `{command}` in one to continue privately here."
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


def bounded_turns(turns: Sequence[TranscriptTurn]) -> list[TranscriptTurn]:
    """Keep a small text-only context, newest selected, oldest rendered first."""
    remaining = 16_000
    selected: list[TranscriptTurn] = []
    for turn in reversed(turns[-12:]):
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
            selected = deps.turn_path or load_turn_settings().path
            scope = resource_scope(
                tenant_id=str(conversation.tenant_id),
                account_id=str(conversation.account_id),
                authorization_id="sealed-dm-retirement",
            )
            ref = (
                deps.backend_session_ref(session_id, scope)
                if selected == "mux" and deps.backend_session_ref is not None
                else None
            )
            await turn_io(
                deps.anthropic,
                session_id,
                path=selected,
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
) -> DirectMessageRow:
    """Select a workspace explicitly and give the DM a new thread-like scope.

    Caller admits the source (is_dm=True, `dm_source_channel_id`) and calls
    `require_unsealed_source` before reading history. The DM bills
    `source_channel_id`; each move resets the scope and replays no prior history.
    """
    require_unsealed_source(admission)
    await require_dm_enabled(deps, tenant_id=tenant_id)
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
            [TranscriptTurn(role="user", text=f"Source: {source_url}"), *bounded_turns(context)],
            from_agent_name="source conversation",
        ),
        memory_read_only=admission.memory_read_only,
        history=[],
        recent_message_ids=[],
        active_until=None,
    )
    async with deps.sessionmaker.begin() as session:
        await start_conversation(session, conversation=conversation, now=datetime.now(UTC))
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
) -> Admission:
    """Check a configured cold agent without creating one or changing channel routing."""
    await require_dm_enabled(deps, tenant_id=tenant_id)
    async with deps.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
        name = await get_dm_agent_name(session, tenant_id=tenant_id)
    if not tenant_ready(tenant, platform="discord") or name is None:
        raise AdmissionDenied(reason="invoker_not_allowed")
    return await admit(
        deps,
        tenant_id=tenant_id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
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
) -> DirectMessageRow | None:
    """Create a source-free private scope, compare-and-setting the active route.

    A concurrent first message must reuse the winning conversation. An explicit
    switch names the scope it replaces, so a stale control cannot replace a
    newer selection. No configuration is written to the physical DM channel.
    """
    conversation = DirectMessageRow(
        platform="discord",
        route_key=route_key,
        external_user_id=external_user_id,
        tenant_id=tenant_id,
        account_id=admission.account_id,
        workspace_id=workspace_id,
        channel_id=route_key,
        scope_id=f"{DM_SCOPE_PREFIX}{uuid.uuid4()}",
        source_url=COLD_DM_ORIGIN,
        context="",
        memory_read_only=admission.memory_read_only,
        history=[],
        recent_message_ids=[],
        active_until=None,
    )
    async with deps.sessionmaker.begin() as session:
        await lock_route(
            session, platform="discord", route_key=route_key, external_user_id=external_user_id
        )
        current = await get_conversation(
            session, platform="discord", route_key=route_key, external_user_id=external_user_id
        )
        if (current.scope_id if current else None) != expected_scope_id:
            return None
        if (
            not await dm_enabled(session, tenant_id=tenant_id)
            or await get_dm_agent_name(session, tenant_id=tenant_id) != admission.config.agent_name
        ):
            raise AdmissionDenied(reason="invoker_not_allowed")
        await start_conversation(session, conversation=conversation, now=datetime.now(UTC))
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
            platform="discord",
            parent_channel_id=route_key,
            thread_id=conversation.scope_id,
            responder_ma_agent_id=admission.agent.id,
            responder_name=admission.agent.name,
            creator_account_id=admission.account_id,
            kind="handoff",
        )
    return conversation


class _CollectReply(TurnLifecycle):
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
) -> tuple[str, str]:
    """Run one admitted, bound private turn and return its final text and session.

    Shared by live DM replies and DM routines. `on_sealed` runs when the DM's
    source was sealed during the turn (it may raise its own refusal). Nothing
    is rendered while the turn runs; the caller delivers the answer.
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
        ) as turn_origin_row,
    ):
        controls = render_turn_origin(
            turn_origin_row, effective_is_admin=prepared.admission.is_admin
        )
        user_message = f"{controls}\n{prompt}"

        async def reseed() -> str:
            return user_message

        try:
            outcome = await run_prepared_turn(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=scope_id,
                external_user_id=external_user_id,
                user_message=user_message,
                lifecycle=_CollectReply(),
                cancel=asyncio.Event(),
                reseed_user_message=reseed,
                recovery_lifecycle=lambda _: _CollectReply(),
                origin=origin,
                deadline=deadline,
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
    return answer, outcome.ma_session_id


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
    verified_membership: bool | None = None,
    on_agent: Callable[[str], Awaitable[None]] | None = None,
) -> str | None:
    """Compatibility text API for adapters that do not consume a delivery grant."""
    reply = await reply_to_dm_result(
        deps,
        platform=platform,
        route_key=route_key,
        external_user_id=external_user_id,
        message_id=message_id,
        expected_scope_id=expected_scope_id,
        text=text,
        role=role,
        role_ids=role_ids,
        verified_membership=verified_membership,
        on_agent=on_agent,
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
    verified_membership: bool | None = None,
    on_agent: Callable[[str], Awaitable[None]] | None = None,
) -> DirectMessageReply | None:
    """Admit every DM, serialize its scope, run a billed turn, retain bounded history.

    A duplicate delivery returns None. The adapter verifies live membership
    and supplies the current role before entering; stored admin roles never
    authorize a private turn. Platform API errors must fail that check closed.
    """
    async with owner_dm_turn(deps, platform=platform, owner=external_user_id, wait=True):
        now = datetime.now(UTC)
        async with deps.sessionmaker.begin() as session:
            conversation = await claim_message(
                session,
                platform=platform,
                route_key=route_key,
                external_user_id=external_user_id,
                message_id=message_id,
                expected_scope_id=expected_scope_id,
                now=now,
                active_until=now + timedelta(seconds=TURN_CEILING_S + 60),
            )
        if conversation is None:
            return None
        history: list[dict[str, str]] | None = None
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
                requester_org=RequesterOrg.HOST,
                platform=platform,
                external_user_id=external_user_id,
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
            deadline = turn_deadline(now=now)
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
                    bounded_turns(previous), from_agent_name="private conversation"
                )
            )
            answer, _ = await _run_admitted_dm_turn(
                deps,
                prepared,
                tenant_id=conversation.tenant_id,
                platform=platform,
                parent_channel_id=conversation.channel_id,
                scope_id=conversation.scope_id,
                external_user_id=external_user_id,
                role=role,
                prompt=f"{context}\n\n{text}",
                deadline=deadline,
                destination=_dm_destination(deps, conversation, admission.slack_turn_context_id),
                on_sealed=lambda: _quarantine(deps, conversation),
            )
            turns = bounded_turns(
                [
                    *previous,
                    TranscriptTurn(role="user", text=text),
                    TranscriptTurn(role="agent", text=answer),
                ]
            )
            history = [{"role": turn.role, "text": turn.text} for turn in turns]
            grant = prepared.admission.grant
            if grant is None:
                raise DirectMessageAccessRevoked(
                    "authorization_missing", message="Private authority unavailable."
                )
            return DirectMessageReply(text=answer, conversation=conversation, grant=grant)
        except (BoundAgentUnavailable, AccessPolicyUnreadable, MAResolverMissError) as error:
            raise _revoked(error) from error
        except asyncio.CancelledError:
            if bound and deps.running_turns.is_drain_cancel():
                await _settle_shutdown_cancelled_dm(deps, conversation)
            raise
        finally:
            async with deps.sessionmaker.begin() as session:
                await finish_message(session, conversation=conversation, history=history)


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
) -> DmRoutineReply:
    """Run a DM routine's fire as a fresh, unattended private DM turn of its owner.

    Ordinary cold-DM admission and session (personal memory included), in a new
    scope with no history. Every session is gated, so a call needing Approve is
    refused; only Managed Agents agents run (`MANAGED_AGENTS_ONLY`). `source` is
    rechecked for a seal whenever the session is built; `budget_channel_id` only
    bills. Nothing is posted here: the caller queues the final output.
    """
    async with owner_dm_turn(deps, platform="discord", owner=owner_user_id, wait=False):
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
                requester_org=RequesterOrg.HOST,
                platform="discord",
                external_user_id=owner_user_id,
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
            if deps.backend is not None and (deps.turn_path or load_turn_settings().path) == "mux":
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
            deadline = turn_deadline(now=now)
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

        async def sealed() -> None:
            raise DirectMessageAccessRevoked("source_sealed", message=SEALED_SINCE_MESSAGE)

        try:
            text, session_id = await _run_admitted_dm_turn(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform="discord",
                parent_channel_id=scope_id,
                scope_id=scope_id,
                external_user_id=owner_user_id,
                role=Role.ADMIN if prepared.admission.is_platform_admin else Role.USER,
                prompt=prompt,
                deadline=deadline,
                on_sealed=sealed,
                origin="routine",
            )
        except asyncio.CancelledError:
            if deps.running_turns.is_drain_cancel():
                await _settle_shutdown_dm_scope(
                    deps,
                    tenant_id=tenant_id,
                    platform="discord",
                    scope_id=scope_id,
                    account_id=admission.account_id,
                )
            raise
        return DmRoutineReply(
            text=text,
            session_id=session_id,
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


async def _settle_shutdown_cancelled_dm(deps: TurnDeps, conversation: DirectMessageRow) -> None:
    """Interrupt the MA turn a shutdown cancelled, marking it dead if unconfirmed.

    Only while this delivery still holds the conversation's claim, so the live
    mapping is this turn's own. Best effort: a database failure is only logged.
    """
    await _settle_shutdown_dm_scope(
        deps,
        tenant_id=conversation.tenant_id,
        platform=conversation.platform,
        scope_id=conversation.scope_id,
        account_id=conversation.account_id,
        conversation=conversation,
    )


async def _settle_shutdown_dm_scope(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    scope_id: str,
    account_id: uuid.UUID,
    conversation: DirectMessageRow | None = None,
) -> None:
    """Live-DM best-effort drain, scoped to the claim/owner guard's own mapping."""
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
                    authorization_id="dm-shutdown-drain",
                ),
                timeout_s=_SHUTDOWN_INTERRUPT_TIMEOUT_S,
            )
            if not interrupted:
                await mark_dead(session, id=mapping.id)
            _log.info(
                "dm.shutdown_turn_settled",
                session_id=mapping.ma_session_id,
                interrupted=interrupted,
            )
    except SQLAlchemyError as err:
        _log.warning("dm.shutdown_settle_failed", error_type=type(err).__name__)


async def require_dm_enabled(deps: TurnDeps, *, tenant_id: uuid.UUID) -> None:
    async with deps.sessionmaker() as session:
        enabled = await dm_enabled(session, tenant_id=tenant_id)
    if not enabled:
        raise UserFacingError("Daimon DMs are off here.\n\nAsk an admin to run /dm-me enable.")


@asynccontextmanager
async def owner_dm_turn(
    deps: TurnDeps, *, platform: str, owner: str, wait: bool
) -> AsyncIterator[None]:
    """Serialize local owner turns across tenants and adapter processes.

    An existing transaction advisory lock is sufficient: no persistent turn
    outcome or promise about remote activity after process loss is introduced.
    Failed tries release their connection before live-DM queue retries.
    """

    async def acquire() -> AsyncExitStack:
        stack = AsyncExitStack()
        try:
            bind = deps.sessionmaker.kw.get("bind")
            engine = bind.engine if isinstance(bind, AsyncConnection) else bind
            if not isinstance(engine, AsyncEngine):
                raise RuntimeError("DM owner exclusion requires an async database engine")
            connection = await stack.enter_async_context(engine.connect())
            db = await stack.enter_async_context(AsyncSession(bind=connection))
            await stack.enter_async_context(db.begin())
            await try_fence(db, f"owner_dm:{platform}:{owner}", schema_scoped=True)
            # Detach so the lock does not hold a work-pool slot; closing the
            # detached connection releases the lock on every local exit.
            await connection.run_sync(lambda sync: sync.detach())
        except BaseException:
            await stack.aclose()
            raise
        return stack

    if wait:
        stack = await retry_fences(acquire)
    else:
        try:
            stack = await acquire()
        except FenceUnavailable as error:
            raise DirectMessageAccessRevoked(
                "owner_busy",
                message="The owner already has a DM turn running; this slot was skipped.",
            ) from error
    async with stack:
        yield

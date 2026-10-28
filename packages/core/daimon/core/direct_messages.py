"""Private conversations using the ordinary admission, scope and turn pipeline."""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import anthropic
import structlog
from daimon.core.access_policy import (
    DM_SCOPE_PREFIX,
    TenantAccessPolicy,
)
from daimon.core.config import load_turn_settings
from daimon.core.errors import UserFacingError
from daimon.core.handoff_context import TranscriptTurn, render_previous_session
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.mux_backend import resource_scope
from daimon.core.permissions import dm_source_limited, limited_ids
from daimon.core.platform_words import space_for, words_for
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import (
    DirectMessageRow,
    claim_message,
    dm_enabled,
    finish_message,
    holds_claim,
    quarantine_conversation,
    start_conversation,
)
from daimon.core.stores.domain import ChatPlatform, Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    delete_slack_turn_context,
)
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.stores.thread_sessions import get_live_thread_session, mark_dead
from daimon.core.turn.admission import Admission, DmSource, admit
from daimon.core.turn.ceiling import TURN_CEILING_S, turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import DmSourceSealedError
from daimon.core.turn.io import turn_io
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.turn_hold import RequesterOrg
from daimon.core.turn_origin import render_turn_origin, turn_origin
from sqlalchemy.exc import SQLAlchemyError

_log = structlog.get_logger(__name__)


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
            f"To talk here, run `{command}` in a channel first and I'll continue with you here.\n"
            "Or mention me in any channel."
        )
    if dms_on is None:
        return (
            f"To talk here, run `{command}` in a {words.space} channel first "
            "and I'll continue with you here.\n"
            "Or mention me in any channel. If private chats are off there, "
            f"ask {words.admin} to run `{command} enable`."
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
    return dm_source_limited(
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
    raise UserFacingError(SEALED_SINCE_MESSAGE)


async def _require_source_still_unsealed(deps: TurnDeps, conversation: DirectMessageRow) -> None:
    """Quarantine the conversation when its source has been sealed since.

    The row (copied context and private history) is deleted and the scope's
    provider sessions retired, so nothing supplied earlier survives into a
    later turn; a fresh /dm-me is required. `reauthorize` asks the same question
    again right before the DM's session is built (`DmSourceSealedError`).
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

    Caller admits the source with is_dm=True and `dm_source_channel_id`, and
    calls `require_unsealed_source`, before reading history or opening the DM.
    `source_channel_id` is the parent channel `/dm-me` ran in; the DM's turns
    count toward its budget. Each move resets the private scope; a prior
    workspace's physical DM history is never replayed into this one.
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
    on_agent: Callable[[str], Awaitable[None]] | None = None,
    platform_channel_id: str | None = None,
) -> str | None:
    """Admit every DM, serialize its scope, run a billed turn, retain bounded history.

    `platform_channel_id` is the DM channel as the platform's API names it, recorded
    on the turn's origin with `message_id`.

    A duplicate delivery returns None. The adapter verifies live membership
    and supplies the current role before entering; stored admin roles never
    authorize a private turn. Platform API errors must fail that check closed.
    """
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
        await require_dm_enabled(deps, tenant_id=conversation.tenant_id)
        async with deps.sessionmaker() as session:
            tenant = await get_tenant(session, conversation.tenant_id)
        if tenant is None or tenant.archived_at is not None or tenant.provision_status != "ready":
            raise UserFacingError(
                f"This {space_for(platform)} isn't available, so no DM was started. Ask an admin."
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
            is_dm=True,
            dm_source_channel_id=conversation.source_channel_id,
            now=now,
        )
        if admission.account_id != conversation.account_id:
            raise UserFacingError(
                "Your account changed, so this DM can't continue.\n\n"
                "Run /dm-me again in the channel you came from."
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
                dm_source=DmSource(
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
            TranscriptTurn(role="user" if item["role"] == "user" else "agent", text=item["text"])
            for item in conversation.history
        ]
        context = (
            conversation.context
            + "\n"
            + render_previous_session(
                bounded_turns(previous), from_agent_name="private conversation"
            )
        )
        async with (
            _dm_destination(deps, conversation, admission.slack_turn_context_id),
            turn_origin(
                deps.sessionmaker,
                tenant_id=conversation.tenant_id,
                account_id=admission.account_id,
                platform=platform,
                parent_channel_id=conversation.channel_id,
                thread_id=conversation.scope_id,
                responder_ma_agent_id=admission.agent.id,
                responder_name=admission.agent.name,
                role=role,
                sender_foreign=admission.hold is not None and admission.hold.client,
                shared_channel=admission.shared_channel,
                platform_channel_id=platform_channel_id,
                platform_message_id=message_id if platform_channel_id is not None else None,
            ) as origin,
        ):
            user_message = f"{render_turn_origin(origin)}\n{context}\n\n{text}"

            async def reseed() -> str:
                return user_message

            try:
                outcome = await run_prepared_turn(
                    deps,
                    prepared,
                    tenant_id=conversation.tenant_id,
                    platform=platform,
                    thread_id=conversation.scope_id,
                    external_user_id=external_user_id,
                    user_message=user_message,
                    request_text=text,
                    lifecycle=_CollectReply(),
                    cancel=asyncio.Event(),
                    reseed_user_message=reseed,
                    recovery_lifecycle=lambda _: _CollectReply(),
                    deadline=deadline,
                )
            except DmSourceSealedError:
                # Decided again during a dead-session recovery: same quarantine.
                await _quarantine(deps, conversation)
                raise
        if isinstance(outcome.state.error, DmSourceSealedError):
            await _quarantine(deps, conversation)
        if outcome.state.error is not None:
            raise outcome.state.error
        answer = (
            extract_final_response(outcome.state.content)
            or "The agent finished without a text reply."
        )
        turns = bounded_turns(
            [
                *previous,
                TranscriptTurn(role="user", text=text),
                TranscriptTurn(role="agent", text=answer),
            ]
        )
        history = [{"role": turn.role, "text": turn.text} for turn in turns]
        return answer
    except asyncio.CancelledError:
        if bound and deps.running_turns.is_drain_cancel():
            await _settle_shutdown_cancelled_dm(deps, conversation)
        raise
    finally:
        async with deps.sessionmaker.begin() as session:
            await finish_message(session, conversation=conversation, history=history)


#: A shutdown drain gives each cancelled turn's cleanup 5 s in total.
_SHUTDOWN_INTERRUPT_TIMEOUT_S = 3.0


async def _settle_shutdown_cancelled_dm(deps: TurnDeps, conversation: DirectMessageRow) -> None:
    """Stop the MA turn a shutdown cancelled; a DM has no marker for recovery.

    Nothing will read the session again, so it is interrupted, and marked dead
    when the interrupt is unconfirmed, so the next DM starts a fresh session
    instead of sending into a busy one. Only while this delivery still holds
    the conversation's claim (`active_until`, as `finish_message` compares):
    the claim serializes the scope, so the live mapping is this turn's own,
    including a replacement made by dead-session recovery. Best effort: a
    database failure is logged and the claim expires on its own.
    """
    try:
        async with deps.sessionmaker.begin() as session:
            if not await holds_claim(session, conversation=conversation):
                return
            mapping = await get_live_thread_session(
                session,
                tenant_id=conversation.tenant_id,
                platform=conversation.platform,
                thread_id=conversation.scope_id,
                account_id=conversation.account_id,
            )
            if mapping is None:
                return
            interrupted = await interrupt_orphaned_session(
                deps.anthropic,
                session_id=mapping.ma_session_id,
                scope=resource_scope(
                    tenant_id=str(conversation.tenant_id),
                    account_id=str(conversation.account_id),
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

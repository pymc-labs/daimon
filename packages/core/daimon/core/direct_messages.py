"""Private conversations using the ordinary admission, scope and turn pipeline."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import anthropic
from daimon.core.access_policy import DM_SCOPE_PREFIX, TenantAccessPolicy, is_sealed_source
from daimon.core.errors import DaimonError
from daimon.core.handoff_context import TranscriptTurn, render_previous_session
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import (
    DirectMessageRow,
    claim_message,
    dm_enabled,
    finish_message,
    quarantine_conversation,
    start_conversation,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    delete_slack_turn_context,
)
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.ceiling import TURN_CEILING_S, turn_deadline
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import TurnState, extract_final_response
from daimon.core.turn_origin import render_turn_origin, turn_origin
from sqlalchemy.exc import SQLAlchemyError


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
    "This channel is sealed, so its conversation can't be moved to a DM. Keep working here instead."
)


SEALED_SINCE_MESSAGE = (
    "Where this conversation came from is now sealed, so it has ended and its copied "
    "context was removed. Run /dm in an unsealed channel to start a new one."
)


async def sealed_channel_ids(deps: TurnDeps, *, tenant_id: uuid.UUID) -> frozenset[str]:
    """The tenant's current seal list, for filtering history a /dm move copies."""
    async with deps.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    return frozenset(policy.sealed_channel_ids)


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
    """Whether any recorded source of this conversation is sealed now.

    Fails closed: a row without recorded provenance (written before it was
    stored) cannot prove its source unsealed, so any seal in the tenant
    counts. For a Discord thread the parent can't be recovered from the old
    source URL.
    """
    if not policy.sealed_channel_ids:
        return False
    if conversation.source_channel_id is None:
        return True
    if is_sealed_source(
        policy, channel_id=conversation.source_channel_id, thread_id=conversation.source_thread_id
    ):
        return True
    sealed = set(policy.sealed_channel_ids)
    return any(key in sealed for key in conversation.source_thread_keys or ())


async def _require_source_still_unsealed(deps: TurnDeps, conversation: DirectMessageRow) -> None:
    """Quarantine the conversation when its source has been sealed since.

    The row (copied context and private history) is deleted and the scope's
    provider sessions retired, so nothing supplied earlier survives into a
    later turn; a fresh /dm is required.
    """
    async with deps.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=conversation.tenant_id)
    if not _source_is_sealed(policy, conversation):
        return
    async with deps.sessionmaker.begin() as session:
        retired = await quarantine_conversation(session, conversation=conversation)
    for session_id in retired:
        with suppress(anthropic.APIError):
            await deps.anthropic.beta.sessions.archive(session_id)
    raise DaimonError(SEALED_SINCE_MESSAGE)


def require_unsealed_source(admission: Admission) -> None:
    """Refuse /dm from a sealed channel or thread before any history is read.

    A DM sits outside the seal: copying the channel's recent messages into it
    would carry sealed content into a conversation that can reach unsealed
    channels.
    """
    if admission.source_sealed:
        raise DaimonError(SEALED_SOURCE_MESSAGE)


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

    Caller admits the source with is_dm=True and calls `require_unsealed_source`
    before reading history or opening the DM. Each move resets the private
    scope; a prior workspace's physical DM history is never replayed into this
    one.
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
) -> str | None:
    """Admit every DM, serialize its scope, run a billed turn, retain bounded history.

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
    try:
        await require_dm_enabled(deps, tenant_id=conversation.tenant_id)
        async with deps.sessionmaker() as session:
            tenant = await get_tenant(session, conversation.tenant_id)
        if tenant is None or tenant.archived_at is not None or tenant.provision_status != "ready":
            raise DaimonError("This workspace is not available. No DM turn was started.")
        await _require_source_still_unsealed(deps, conversation)
        admission = await admit(
            deps,
            tenant_id=conversation.tenant_id,
            platform=platform,
            external_user_id=external_user_id,
            channel_id=conversation.channel_id,
            thread_id=conversation.scope_id,
            role=role,
            is_dm=True,
            now=now,
        )
        if admission.account_id != conversation.account_id:
            raise DaimonError("Your account changed. Run /dm again in the workspace channel.")
        execution_id = uuid.uuid4() if platform == "slack" else None
        admission = replace(
            admission,
            memory_read_only=admission.memory_read_only or conversation.memory_read_only,
            slack_turn_context_id=execution_id,
            private_dm_id=str(execution_id) if execution_id else conversation.scope_id,
        )
        deadline = turn_deadline(now=now)
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
            ) as origin,
        ):
            user_message = f"{render_turn_origin(origin)}\n{context}\n\n{text}"

            async def reseed() -> str:
                return user_message

            outcome = await run_prepared_turn(
                deps,
                prepared,
                tenant_id=conversation.tenant_id,
                platform=platform,
                thread_id=conversation.scope_id,
                external_user_id=external_user_id,
                user_message=user_message,
                lifecycle=_CollectReply(),
                cancel=asyncio.Event(),
                reseed_user_message=reseed,
                recovery_lifecycle=lambda _: _CollectReply(),
                deadline=deadline,
            )
        if outcome.state.error is not None:
            raise outcome.state.error
        answer = (
            extract_final_response(outcome.state.content)
            or "The turn completed without a text reply."
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
    finally:
        async with deps.sessionmaker.begin() as session:
            await finish_message(session, conversation=conversation, history=history)


async def require_dm_enabled(deps: TurnDeps, *, tenant_id: uuid.UUID) -> None:
    async with deps.sessionmaker() as session:
        enabled = await dm_enabled(session, tenant_id=tenant_id)
    if not enabled:
        raise DaimonError("DM conversations are disabled here. Ask an admin to run /dm enable.")

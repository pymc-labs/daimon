"""Answer the tool confirmations of agent-chat turns, which nobody watches.

The process that sent a turn answers its pauses: `watch_turn` starts a
bounded worker for that one turn boundary, right after an authorized
`start_turn` or `continue_turn` send, and `record_turn` notes in the database
that agent chat started that turn and under which channel's tool rules. Each
pause is decided at call time by `unattended_posture` from the current
policy. A read (`turn_status`) only reports. After a restart the session
stays paused and gated until its owner calls `continue_turn`, which re-gates
it and starts a worker on the paused turn (`resume_paused`), only if agent
chat started that turn: a chat card is left to the person it asks.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Literal

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.resource_ports import (
    list_session_events,
    mcp_scope,
    retrieve_session,
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._session_access import hub_access_mode
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_AGENT_CHAT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_PRIVATE_DM,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_tool_annotations import agent_mcp_token_for, mcp_annotation_lookup
from daimon.core.stores import session_settle_state
from daimon.core.turn.approvals import (
    AnnotationLookup,
    build_decision_events,
    channel_for_tool_rules,
    pending_confirmation_ids,
    tool_calls_for,
)
from daimon.core.turn.ceiling import TURN_CEILING_S
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.io import LegacyTurnIO
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState
from daimon.core.turn.unattended import gate_session_snapshot, unattended_posture
from fastmcp.exceptions import ToolError

log = structlog.get_logger(__name__)

PAUSED_HINT = "paused: call continue_turn to resume"
"""What a read reports for an agent-chat turn waiting on confirmations nobody answers."""

AWAITING_CARD_HINT = "paused: waiting for approval where it was asked"
"""What a read reports for a turn paused on someone's confirmation card."""

NOT_RESUMABLE_MSG = (
    "this conversation is waiting for an approval in the chat where it was asked; "
    "agent chat can't answer it. Ask the person to approve or deny it there."
)

# Newest-first pages read back to a turn's boundary before giving up.
_MAX_TURN_PAGES = 20
_POLL_S = 1.0


@dataclass(frozen=True)
class _Worker:
    """This process's confirmation worker for a handle, and the turn it owns."""

    task: asyncio.Task[bool]
    turn_event_id: str

    def owns(self, turn_event_id: str | None) -> bool:
        return not self.task.done() and self.turn_event_id == turn_event_id


_workers: dict[tuple[int, str], _Worker] = {}


def single_attempt(runtime: McpRuntime) -> AsyncAnthropic:
    """The client for a send that changes a session: one attempt, no SDK retry.

    A retried message or confirmation would go out after its gate and its
    decision, under whatever policy or rule changed meanwhile. A failed or
    ambiguous attempt raises instead, and is never resent blindly.
    """
    return runtime.client.with_options(max_retries=0)


def require_session_driver(session: BetaManagedAgentsSession, auth: AuthIdentity) -> None:
    """Require the headless creation surface and owning account, outside a hub read.

    `_verify_agent_owns_session` already holds this for a continue; it is
    checked again where pauses get answered, so a read grant (an admin's hub
    read of a channel session) can never reach that code.
    """
    if (session.metadata or {}).get(MA_METADATA_KEY_AGENT_CHAT) != "true":
        raise ToolError(
            "Only conversations opened through agent chat can be driven here. "
            "Open a new conversation."
        )
    if (session.metadata or {}).get(MA_METADATA_KEY_ACCOUNT) != str(
        auth.account_id
    ) or hub_access_mode() == "read":
        raise ToolError("conversation not found")


async def gate(runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession) -> None:
    """Rewrite `session`'s tool config from the current policy before a send.

    A session the unattended gate cannot constrain (a coordinator agent, or
    a capability this release does not know) is refused as a tool error
    naming what was refused.
    """
    try:
        await gate_session_snapshot(
            single_attempt(runtime),
            session,
            runtime.settings.tool_safety,
            scope=mcp_scope(auth),
            public_url=_public_url(runtime),
            session_factory=runtime.session_factory,
            tenant_id=auth.tenant_id,
            entry_point="mcp_chat",
        )
    except AdmissionDenied as err:
        raise ToolError(unconstrained_message(err)) from err


def unconstrained_message(err: AdmissionDenied) -> str:
    """The refusal for a session the unattended gate cannot constrain, naming what."""
    what = err.detail or "capabilities this release has not reviewed"
    return f"This agent cannot run here: an agent chat nobody watches cannot limit {what}."


def rules_channel(auth: AuthIdentity, session: BetaManagedAgentsSession) -> str | None:
    """The channel whose tool rules a send by `auth` on `session` runs under.

    A channel-bound key's own channel, else the channel the session is a
    conversation in; never a DM's source channel, and a Slack thread's
    parent channel (`channel_for_tool_rules`).
    """
    metadata = session.metadata or {}
    return channel_for_tool_rules(
        platform=auth.platform or "",
        channel_id=token_channel_id(auth) or metadata.get(MA_METADATA_KEY_CHANNEL),
        private_dm_id=metadata.get(MA_METADATA_KEY_PRIVATE_DM),
    )


async def record_turn(
    runtime: McpRuntime, handle: str, *, turn_event_id: str, rules_channel_id: str | None
) -> None:
    """Note that agent chat started `turn_event_id`, after its send was accepted.

    Without this record nobody can resume the turn after a restart, which
    fails closed; the sending worker does not need it.
    """
    try:
        async with runtime.session_factory.begin() as db:
            await session_settle_state.record_unattended_turn(
                db,
                managed_session_id=handle,
                turn_event_id=turn_event_id,
                rules_channel_id=rules_channel_id,
            )
    except Exception:
        # Named boundary: the message was accepted; only a later resume is lost.
        log.exception("mcp.agent_chat.turn_record_failed", session_id=handle)


async def _recorded_turn(
    runtime: McpRuntime, handle: str
) -> session_settle_state.UnattendedTurn | None:
    async with runtime.session_factory() as db:
        return await session_settle_state.unattended_turn(db, managed_session_id=handle)


def _public_url(runtime: McpRuntime) -> str | None:
    url = runtime.settings.mcp.public_url
    return str(url) if url is not None else None


def _annotations_for(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> AnnotationLookup | None:
    """Read only trusted catalogues from this session, using its agent's credential."""
    policy = runtime.settings.tool_safety
    if not policy.enabled or not policy.trusted_annotations:
        return None
    return mcp_annotation_lookup(
        server_urls={
            s.name: s.url for s in session.agent.mcp_servers if s.name in policy.trusted_annotations
        },
        token_for=agent_mcp_token_for(
            runtime.session_factory,
            fernet=runtime.fernet,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=session.agent.id),
        ),
    )


@dataclass
class _Turn:
    boundary_id: str | None = None
    state: TurnState = field(default_factory=TurnState)
    confirmed: set[str] = field(default_factory=set[str])


async def _read_turn(runtime: McpRuntime, auth: AuthIdentity, handle: str) -> _Turn:
    """The newest turn, folded oldest-first from its `user.message` boundary.

    Reads newest-first back to the boundary, so a previous turn's end can
    never hide the current pause. Raises past the page ceiling: no evidence
    is no answer.
    """
    events: list[BetaManagedAgentsSessionEvent] = []
    turn = _Turn()
    page: str | None = None
    for _ in range(_MAX_TURN_PAGES):
        query: dict[str, object] = {"order": "desc", "limit": 100}
        if page is not None:
            query["page"] = page
        result = await list_session_events(
            runtime.client, handle, query=query, scope=mcp_scope(auth)
        )
        for event in result.data:
            if event.type == "user.message":
                turn.boundary_id = event.id
                break
            events.append(event)
        if turn.boundary_id is not None or result.next_page is None:
            break
        page = result.next_page
    else:
        raise RuntimeError("MCP turn history exceeds the confirmation page limit")
    for event in reversed(events):
        turn.state = apply(turn.state, event)
        if event.type == "user.tool_confirmation":
            turn.confirmed.add(event.tool_use_id)
    return turn


def watch_turn(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    *,
    turn_event_id: str,
    rules_channel_id: str | None,
) -> asyncio.Task[bool]:
    """Answer the pauses of the turn that starts at `turn_event_id`, and no other.

    One worker per handle: a newer turn's worker replaces the older one. The
    task runs in a fresh context, outside the request that started it.
    """
    key = (id(runtime), handle)
    if (previous := _workers.get(key)) is not None:
        previous.task.cancel()
    task = asyncio.create_task(
        _watch(
            runtime, auth, handle, turn_event_id=turn_event_id, rules_channel_id=rules_channel_id
        ),
        context=contextvars.Context(),
    )
    _workers[key] = _Worker(task=task, turn_event_id=turn_event_id)

    def finished(done: asyncio.Task[bool]) -> None:
        if (current := _workers.get(key)) is not None and current.task is done:
            _workers.pop(key, None)

    task.add_done_callback(finished)
    return task


async def _watch(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    *,
    turn_event_id: str,
    rules_channel_id: str | None,
) -> bool:
    """True once the turn ended; False if it was left paused (it stays gated)."""
    answered: set[str] = set()
    annotations_for: AnnotationLookup | None = None
    annotations_bound = False
    try:
        async with asyncio.timeout(TURN_CEILING_S):
            while True:
                session = await retrieve_session(runtime.client, handle, scope=mcp_scope(auth))
                if not annotations_bound:
                    annotations_for = _annotations_for(runtime, auth, session)
                    annotations_bound = True
                if session.status == "terminated" or session.archived_at is not None:
                    return True
                if session.status == "idle":
                    turn = await _read_turn(runtime, auth, handle)
                    if turn.boundary_id != turn_event_id:
                        # Not listed yet, or a newer turn whose sender answers it.
                        await asyncio.sleep(_POLL_S)
                        continue
                    stop = turn.state.stop_reason
                    ids = pending_confirmation_ids(stop, confirmed=turn.confirmed | answered)
                    if ids:
                        # The confirmation resumes the turn: gate it as a message
                        # would be, and decide on the snapshot that gate left.
                        await gate(runtime, auth, session)
                        session = await retrieve_session(
                            runtime.client, handle, scope=mcp_scope(auth)
                        )
                        await _answer(
                            runtime, auth, session, turn, ids, rules_channel_id, annotations_for
                        )
                        answered.update(ids)
                    elif stop is not None and stop.type != "requires_action":
                        if answered:
                            await _keep_settle_request(runtime, handle, turn_event_id)
                        return True
                await asyncio.sleep(_POLL_S)
    except Exception:
        # The session stays gated on any failure; no failure path allows a call.
        log.exception("mcp.agent_chat.confirmation_worker_failed", session_id=handle)
        return False


async def _answer(
    runtime: McpRuntime,
    auth: AuthIdentity,
    session: BetaManagedAgentsSession,
    turn: _Turn,
    ids: list[str],
    rules_channel_id: str | None,
    annotations_for: AnnotationLookup | None,
) -> None:
    posture = unattended_posture(
        runtime.settings.tool_safety,
        session=session,
        public_url=_public_url(runtime),
        session_factory=runtime.session_factory,
        tenant_id=auth.tenant_id,
        channel_id=rules_channel_id,
        annotations_for=annotations_for,
        entry_point="mcp_chat",
    )
    decisions = [
        (call.tool_use_id, await posture.decide(call)) for call in tool_calls_for(turn.state, ids)
    ]
    # The session port carries only messages and interrupts; confirmations go
    # through the turn transport the routine driver answers with.
    await LegacyTurnIO(single_attempt(runtime), session.id, scope=mcp_scope(auth)).send(
        build_decision_events(decisions)
    )


async def _keep_settle_request(runtime: McpRuntime, handle: str, turn_event_id: str) -> None:
    """Leave a settle request for a turn that ran on after a pause.

    A read or the drain may settle at a pause (an idle after the boundary)
    and clear the request; the calls made after it are billed by this one.
    A request already pending, this turn's or a newer send's, is left alone.
    """
    try:
        async with runtime.session_factory.begin() as db:
            if await session_settle_state.settle_request(db, managed_session_id=handle) is not None:
                return
            _, requested_at = await session_settle_state.request_settle(
                db, managed_session_id=handle
            )
            await session_settle_state.name_settle_turn(
                db,
                managed_session_id=handle,
                requested_at=requested_at,
                turn_event_id=turn_event_id,
            )
    except Exception:
        # Named boundary: the turn has ended; billing it never reopens it.
        log.exception("billing.settle_request_failed", managed_session_id=handle)


async def _paused(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> bool:
    if session.status != "idle":
        return False
    result = await list_session_events(
        runtime.client,
        session.id,
        scope=mcp_scope(auth),
        query={"order": "desc", "limit": 1, "types": ["session.status_idle"]},
    )
    event = result.data[0] if result.data else None
    return (
        event is not None
        and event.type == "session.status_idle"
        and event.stop_reason.type == "requires_action"
    )


async def turn_status(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> tuple[Literal["running", "paused"], str | None] | None:
    """What a read reports for a turn paused on confirmations; never answers one.

    `running` while this process's worker is answering it; otherwise `paused`
    with a hint: resume with `continue_turn` when agent chat started that
    turn, approve where it was asked when it did not. None for any session
    not paused, which reads as MA reports it.
    """
    if not await _paused(runtime, auth, session):
        return None
    turn = await _read_turn(runtime, auth, session.id)
    worker = _workers.get((id(runtime), session.id))
    if worker is not None and worker.owns(turn.boundary_id):
        return ("running", None)
    recorded = await _recorded_turn(runtime, session.id)
    ours = recorded is not None and recorded.turn_event_id == turn.boundary_id
    return ("paused", PAUSED_HINT if ours else AWAITING_CARD_HINT)


@dataclass(frozen=True)
class Resumed:
    worker: asyncio.Task[bool]
    turn_event_id: str


async def resume_paused(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> Resumed | None:
    """Answer `session`'s paused turn from the current policy; None if not paused.

    Only for the owner's `continue_turn`, after `gate`, inside its fence, and
    only for a turn agent chat started (`record_turn`): its tool rules are
    that send's channel's. A pause on any other turn is refused.
    """
    require_session_driver(session, auth)
    if not await _paused(runtime, auth, session):
        return None
    turn = await _read_turn(runtime, auth, session.id)
    recorded = await _recorded_turn(runtime, session.id)
    if turn.boundary_id is None or recorded is None or recorded.turn_event_id != turn.boundary_id:
        raise ToolError(NOT_RESUMABLE_MSG)
    worker = _workers.get((id(runtime), session.id))
    # Reuse only the worker that owns this very turn; one left watching an
    # older turn is replaced (`watch_turn` cancels it).
    task = (
        worker.task
        if worker is not None and worker.owns(turn.boundary_id)
        else watch_turn(
            runtime,
            auth,
            session.id,
            turn_event_id=turn.boundary_id,
            rules_channel_id=recorded.rules_channel_id,
        )
    )
    return Resumed(worker=task, turn_event_id=turn.boundary_id)

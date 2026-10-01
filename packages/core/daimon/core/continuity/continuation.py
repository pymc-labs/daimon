"""The contract for dispatching a queued first turn to an agent that just took over.

A handoff that carried work to continue owes the person exactly one turn from
the new agent. That turn is billed and posted into a thread people are reading,
so the whole surface here is built around never running it twice and never
running it when it would be wrong:

- `record_continuation` queues the request. Writing it dispatches nothing.
- `claim_continuation` is the at-most-once gate — one conditional UPDATE in the
  store, so the database picks the winner across restarts and both adapter
  processes.
- `decide_continuation` answers *whether* to run it, from facts the caller
  already has plus a re-resolution of the concrete destination.
- `settle_continuation` closes the row out, delivered or skipped.

What is deliberately NOT here: preparing the session and running the turn.
`daimon.core.session_preparation` owns the first and the adapter's dispatcher
owns the second, because running a turn needs the platform's lifecycle hooks.
This module decides; the adapter acts.

The private-input completion flow adds one producer —
`build_input_continuation`, which turns a consumed credential-request row into
a `reason='private_input_applied'` request — and reuses every function below
unchanged. That is the entire contract between the two flows.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal

from anthropic import AsyncAnthropic
from daimon.core.continuity.messages import (
    render_current_work_must_finish,
    render_timer_seed,
    render_timer_target_changed,
    render_wake_target_changed,
)
from daimon.core.errors import DaimonError
from daimon.core.setup_conversations import get_setup_agent
from daimon.core.stores.credential_requests import get_credential_request_by_idempotency_key
from daimon.core.stores.domain import (
    ChatPlatform,
    ContinuationReason,
    CredentialRequestRow,
    TaskContinuationRow,
)
from daimon.core.stores.task_continuations import (
    claim_continuation as _claim_continuation_row,
)
from daimon.core.stores.task_continuations import (
    get_continuation as _get_continuation_row,
)
from daimon.core.stores.task_continuations import (
    record_continuation as _record_continuation_row,
)
from daimon.core.stores.task_continuations import (
    settle_continuation as _settle_continuation_row,
)
from daimon.core.stores.thread_sessions import get_thread_session_at
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "MAX_REQUESTED_WORK",
    "MIN_REQUESTED_WORK",
    "ContinuationAction",
    "ContinuationDecision",
    "ContinuationRequest",
    "ResponderChanged",
    "build_input_continuation",
    "check_wake_responder",
    "claim_continuation",
    "decide_continuation",
    "load_asking_agent_id",
    "record_continuation",
    "record_input_continuation",
    "sanitize_requested_work",
    "settle_continuation",
]

#: Below this length requested work is too short to describe work — almost
#: always a one-word restatement of the switch itself rather than a request.
MIN_REQUESTED_WORK: Final[int] = 8

#: The longest continuation we will carry into a seed message. The person's own
#: words, bounded — a continuation is a sentence about what to do next, not a
#: document, and it is untrusted text that ends up inside turn controls.
MAX_REQUESTED_WORK: Final[int] = 500


def sanitize_requested_work(text: str | None, *, echoes: Sequence[str]) -> str | None:
    """Null out text that cannot be a real request to carry on work.

    Empty, shorter than `MIN_REQUESTED_WORK`, or equal (case-folded, stripped)
    to one of `echoes` — the names a model is most likely to restate instead of
    describing work. Not truncation: callers slice to `MAX_REQUESTED_WORK`.
    """
    if text is None:
        return None
    normalized = text.strip().lower()
    if not normalized or len(normalized) < MIN_REQUESTED_WORK:
        return None
    if any(normalized == echo.strip().lower() for echo in echoes):
        return None
    return text


ContinuationAction = Literal[
    "dispatch",
    "skip_save_only",
    "skip_superseded",
    "skip_target_changed",
    "skip_turn_running",
    "blocked_preparation_failed",
]


class ContinuationRequest(BaseModel):
    """One queued continuation, fully addressed: who, where, which agent, what work.

    `target_ma_agent_id` is concrete. A name would resolve to whatever carries
    it at dispatch time, and a recreated namesake must never inherit somebody
    else's pending work.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    platform: ChatPlatform
    parent_channel_id: str
    thread_id: str
    requester_account_id: uuid.UUID
    requester_external_user_id: str
    target_ma_agent_id: str
    target_name: str
    requested_work: str | None
    reason: ContinuationReason
    idempotency_key: uuid.UUID


class ContinuationDecision(BaseModel):
    """What to do with a claimed continuation, and what to say about it.

    `message` is person-facing copy for the skip the caller should post, or None
    when the skip is silent (nothing was ever promised). `seed_user_message` is
    the user message a dispatch runs with; the adapter prepends its turn
    controls to it.
    """

    model_config = ConfigDict(frozen=True)

    action: ContinuationAction
    message: str | None = None
    seed_user_message: str | None = None


class ResponderChanged(DaimonError):
    """A wake is due, but a different agent answers its thread now.

    Raised by an adapter after `admit()` and before anything is posted or
    bound, so a wake never runs under an agent it was not queued for.
    `message` is the person-facing notice to post in the thread.
    """

    def __init__(
        self, *, target_name: str, current_name: str, reason: ContinuationReason = "timer"
    ) -> None:
        render = render_timer_target_changed if reason == "timer" else render_wake_target_changed
        self.message = render(target_name, current_name)
        super().__init__(self.message)


def check_wake_responder(
    *,
    reason: ContinuationReason,
    target_ma_agent_id: str,
    target_name: str,
    admitted_ma_agent_id: str,
    admitted_name: str,
    asking_ma_agent_id: str | None = None,
) -> None:
    """Refuse a wake whose thread is now answered by an agent it was not queued for.

    A timer can wait up to 90 days while a thread's routing changes, and its
    note is the old agent's own brief; running it as whoever answers now
    would hand one agent another's work. Agent reach also counts a wake
    against the agent it names, so a handoff runs only as its destination.
    A private input resumes the agent that asked, which in a setup
    conversation or for another agent's key is not the key's agent, so it
    may also run as `asking_ma_agent_id` (`load_asking_agent_id`): the agent
    that asked, while the requester's live session is still with it.
    """
    if admitted_ma_agent_id == target_ma_agent_id:
        return
    if reason == "private_input_applied" and admitted_ma_agent_id == asking_ma_agent_id:
        return
    raise ResponderChanged(target_name=target_name, current_name=admitted_name, reason=reason)


async def load_asking_agent_id(
    session: AsyncSession, row: TaskContinuationRow, *, live_ma_agent_id: str | None
) -> str | None:
    """The agent an applied private input may resume besides its target, or None.

    The asking agent is the one the requester's session in the thread ran
    when the input was asked for, and it counts only while their live
    session (`live_ma_agent_id`) is still with it. A thread rerouted or handed
    off since then moved them to another agent, which never asked; a request
    row erased since leaves nothing to tell, so neither resumes.
    """
    if row.reason != "private_input_applied" or live_ma_agent_id is None:
        return None
    request = await get_credential_request_by_idempotency_key(
        session, idempotency_key=row.idempotency_key
    )
    if request is None:
        return None
    asked = await get_thread_session_at(
        session,
        tenant_id=row.tenant_id,
        platform=row.platform,
        thread_id=row.thread_id,
        account_id=row.requester_account_id,
        at=request.created_at,
    )
    if asked is None or asked.ma_agent_id != live_ma_agent_id:
        return None
    return live_ma_agent_id


def build_input_continuation(
    row: CredentialRequestRow, *, platform: ChatPlatform
) -> ContinuationRequest | None:
    """The continuation a consumed private-input request owes, or None.

    None when the row predates the frozen-target columns or carries no origin
    thread: a delayed form must never resume against an agent re-resolved by
    name.
    """
    if row.origin_thread_id is None:
        return None
    if row.target_ma_agent_id is None or row.target_name is None:
        return None
    return ContinuationRequest(
        tenant_id=row.tenant_id,
        platform=platform,
        parent_channel_id=row.parent_channel_id or row.channel_id,
        thread_id=row.origin_thread_id,
        requester_account_id=row.account_id,
        requester_external_user_id=row.requester_platform_user_id,
        target_ma_agent_id=row.target_ma_agent_id,
        target_name=row.target_name,
        requested_work=row.requested_work,
        reason="private_input_applied",
        idempotency_key=row.idempotency_key,
    )


def _render_target_changed(target_name: str) -> str:
    """Person-facing copy for a destination that is gone or no longer this tenant's.

    Lives here rather than in `continuity.messages` because it is the one
    string only this decision can produce; if a second caller ever needs it,
    move it there.
    """
    return "\n".join(
        [
            f"{target_name} is no longer the agent this was set up for.",
            "Nothing was lost.",
            "Ask me again and I'll use the current one.",
        ]
    )


async def record_continuation(
    sessionmaker: async_sessionmaker[AsyncSession],
    request: ContinuationRequest,
) -> None:
    """Queue `request` as pending. Dispatches nothing; commits its own transaction."""
    async with sessionmaker.begin() as session:
        await _record_continuation_row(
            session,
            tenant_id=request.tenant_id,
            platform=request.platform,
            parent_channel_id=request.parent_channel_id,
            thread_id=request.thread_id,
            requester_account_id=request.requester_account_id,
            requester_external_user_id=request.requester_external_user_id,
            target_ma_agent_id=request.target_ma_agent_id,
            target_name=request.target_name,
            reason=request.reason,
            idempotency_key=request.idempotency_key,
            requested_work=request.requested_work,
        )


async def record_input_continuation(
    session: AsyncSession,
    row: CredentialRequestRow,
    *,
    platform: ChatPlatform,
    carries_work: bool = True,
) -> bool:
    """Queue the turn a spent private-input request owes, in the caller's transaction.

    The continuation commits with the write it belongs to, so a value never
    lands without its follow-up nor a follow-up without its value. False when
    the row can address no continuation (`build_input_continuation` is None).
    `carries_work=False` records the row for the trail alone: a partial write
    must not resume work, and `decide_continuation` skips a row with no work.
    """
    request = build_input_continuation(row, platform=platform)
    if request is None:
        return False
    await _record_continuation_row(
        session,
        tenant_id=request.tenant_id,
        platform=request.platform,
        parent_channel_id=request.parent_channel_id,
        thread_id=request.thread_id,
        requester_account_id=request.requester_account_id,
        requester_external_user_id=request.requester_external_user_id,
        target_ma_agent_id=request.target_ma_agent_id,
        target_name=request.target_name,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        requested_work=request.requested_work if carries_work else None,
    )
    return True


async def claim_continuation(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    idempotency_key: uuid.UUID,
    now: datetime,
) -> bool:
    """Take exclusive responsibility for this continuation; True at most once.

    Each caller gets its own transaction, which is what makes the conditional
    UPDATE underneath a real race: the loser re-reads the winner's committed
    `claimed` status and matches nothing.
    """
    async with sessionmaker.begin() as session:
        return await _claim_continuation_row(session, idempotency_key=idempotency_key, now=now)


async def settle_continuation(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    idempotency_key: uuid.UUID,
    status: Literal["delivered", "skipped"],
    now: datetime,
    skip_reason: str | None = None,
) -> None:
    """Close out a claimed continuation. Call on every terminal path, skips included."""
    async with sessionmaker.begin() as session:
        await _settle_continuation_row(
            session,
            idempotency_key=idempotency_key,
            status=status,
            now=now,
            skip_reason=skip_reason,
        )


async def decide_continuation(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    request: ContinuationRequest,
    now: datetime,
    latest_user_message_at: datetime | None,
    active_turn: bool,
) -> ContinuationDecision:
    """Decide whether this claimed continuation should still run a turn.

    The rules, in order, each one a reason not to spend a billed turn:

    1. No work was requested — the handoff only changed who answers. Nothing
       was ever promised, so the skip is silent.
    2. The destination is gone, archived, or no longer this tenant's. Never
       substitute a namesake; say the target changed and stop.
    3. The person has spoken again since. Their newer message supersedes the
       queued work; running it would answer a stale request out of order.
       Not for a timer, which is meant to fire whatever was said since.
    4. A turn is already running in this thread. The continuation waits for it
       rather than racing it, and the person is told so.

    Otherwise: dispatch, seeded with the person's own words.

    `now` is accepted for symmetry with the rest of the surface and for the
    caller's clock to be the only clock; the rules compare recorded timestamps.
    `blocked_preparation_failed` is part of `ContinuationAction` but is never
    produced here — it is the adapter's outcome when session preparation fails
    after this decision said dispatch.
    """
    if request.requested_work is None:
        return ContinuationDecision(action="skip_save_only")

    async with sessionmaker() as session:
        row = await _get_continuation_row(session, idempotency_key=request.idempotency_key)
    if row is None:
        raise DaimonError(
            "Cannot decide a continuation that was never recorded; "
            "call record_continuation before claiming it."
        )

    try:
        await get_setup_agent(
            anthropic, tenant_id=request.tenant_id, ma_agent_id=request.target_ma_agent_id
        )
    except DaimonError:
        # `get_setup_agent` already distinguishes "gone" from "not yours" and
        # raises for both; either way this continuation has no destination.
        return ContinuationDecision(
            action="skip_target_changed", message=_render_target_changed(request.target_name)
        )

    # A timer is the agent's own appointment: the person talking in the
    # meantime is expected, not a newer request that replaces it.
    if (
        request.reason != "timer"
        and latest_user_message_at is not None
        and latest_user_message_at > row.created_at
    ):
        return ContinuationDecision(action="skip_superseded")

    if active_turn:
        return ContinuationDecision(
            action="skip_turn_running",
            message=render_current_work_must_finish(
                request.target_name, handoff=request.reason == "task_handoff"
            ),
        )

    if request.reason == "timer":
        return ContinuationDecision(
            action="dispatch",
            seed_user_message=render_timer_seed(request.requested_work, set_at=row.created_at),
        )
    return ContinuationDecision(action="dispatch", seed_user_message=request.requested_work)

"""Discord I/O ports for the shared continuation and wake dispatcher."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.adapters.discord.watch_access import require_watch_thread_access
from daimon.core.agent_identity import AgentIdentity, resolve_agent_identity
from daimon.core.continuity.continuation import ContinuationDecision
from daimon.core.continuity.dispatch import (
    ContinuationContextUnavailable,
    WorkerFinishedDecision,
)
from daimon.core.continuity.dispatch import (
    dispatch_pending_continuations as dispatch_core,
)
from daimon.core.neutral_resources import WorkerSpecRefused
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.output import OutputAuthorization, output_authorization
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)
__all__ = ["RunFollowUp", "dispatch_pending_continuations"]
RunFollowUp = Callable[[TaskContinuationRow, ContinuationDecision], Awaitable[None]]
MayPost = Callable[[], Awaitable[bool]]
_HISTORY_PAGE_SIZE = 100
_HISTORY_MAX_PAGES = 20


async def _latest_human_message_at(thread: discord.Thread, *, after: datetime) -> datetime | None:
    """Newest human post after `after`; defer if the history read fails.

    Reaching the page cap is not retried: the same scan would hit it again on
    every retry. The work is treated as superseded instead, so a stale handoff
    never runs and the row doesn't wait forever.
    """
    before: discord.Message | None = None
    try:
        for _ in range(_HISTORY_MAX_PAGES):
            page = [
                message async for message in thread.history(limit=_HISTORY_PAGE_SIZE, before=before)
            ]
            for message in page:
                if message.created_at <= after:
                    return None
                if not message.author.bot:
                    return message.created_at
            if len(page) < _HISTORY_PAGE_SIZE:
                return None
            before = page[-1]
    except Exception as exc:
        raise ContinuationContextUnavailable("Discord history read failed") from exc
    assert before is not None
    log.warning("continuation.history_cap_assumed_superseded", thread_id=thread.id)
    return before.created_at


async def dispatch_pending_continuations(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    thread: discord.Thread,
    run_follow_up: RunFollowUp,
    may_post: MayPost,
    client: discord.Client | None = None,
    public_base_url: str | None = None,
    identity_enabled: bool = False,
    worker_finished_decision: WorkerFinishedDecision | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Dispatch under the caller's existing thread guard."""

    async def context(row: TaskContinuationRow) -> tuple[datetime | None, bool]:
        # A timer is never superseded by newer messages, so it needs no history.
        latest = (
            None
            if row.reason in ("timer", "watch_fired", "job_finished")
            else await _latest_human_message_at(thread, after=row.created_at)
        )
        async with sessionmaker() as session:
            live = await get_live_thread_session(
                session,
                tenant_id=row.tenant_id,
                platform="discord",
                thread_id=row.thread_id,
                account_id=row.requester_account_id,
            )
        return latest, live is not None and live.active_turn_message_id is not None

    async def post(row: TaskContinuationRow, text: str, reason: str) -> None:
        authorization: OutputAuthorization | None = None
        if row.reason == "watch_fired":
            if client is None:
                log.info("continuation.notice_withheld", thread_id=row.thread_id, reason=reason)
                return

            async def check_watch_notice() -> None:
                assert client is not None
                await require_watch_thread_access(sessionmaker, row, client)

            authorization = OutputAuthorization(check_watch_notice)
            try:
                await authorization.check()
            except AdmissionDenied:
                log.info("continuation.notice_withheld", thread_id=row.thread_id, reason=reason)
                return
            allowed = True
        else:
            allowed = await may_post()
        if allowed:
            if client is None:
                await thread.send(text)
            else:
                try:
                    async with sessionmaker.begin() as session:
                        identity = await resolve_agent_identity(
                            session,
                            tenant_id=tenant_id,
                            agent_name=row.target_name,
                            is_builtin=row.target_name.casefold() == "daimon",
                            public_base_url=public_base_url,
                            enabled=identity_enabled,
                            background_sessionmaker=sessionmaker,
                            wait_for_face=True,
                        )
                except Exception as exc:
                    log.warning(
                        "continuation.identity_resolution_failed", error_type=type(exc).__name__
                    )
                    identity = AgentIdentity(name=row.target_name, avatar_url=None, builtin=True)
                transport = DiscordPostTransport(
                    client,
                    thread,
                    name=identity.name,
                    avatar_url=identity.avatar_url,
                    builtin=identity.builtin,
                    identity_enabled=identity_enabled,
                )
                if authorization is None:
                    await transport.send(text)
                else:
                    try:
                        with output_authorization(authorization):
                            await transport.send(text)
                    except AdmissionDenied:
                        log.info(
                            "continuation.notice_withheld", thread_id=row.thread_id, reason=reason
                        )
        else:
            log.info("continuation.notice_withheld", thread_id=row.thread_id, reason=reason)

    await dispatch_core(
        sessionmaker,
        anthropic,
        tenant_id=tenant_id,
        platform="discord",
        thread_id=str(thread.id),
        run_decision=run_follow_up,
        decision_context=context,
        notice=post,
        settle_before_notice=False,
        # A refused fleet wake settles its row; its card is ended by the turn's own surface.
        dispatch_errors=(discord.HTTPException, WorkerSpecRefused),
        now=now,
        worker_finished_decision=worker_finished_decision,
    )

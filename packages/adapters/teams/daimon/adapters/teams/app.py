"""Personal-chat turn dispatch for the Teams adapter.

``VerifiedTeamsTurnResolver`` verifies and resolves the inbound activity;
the dispatcher owns what happens next. ``DirectCoreTurnDispatcher`` runs
the turn in this process as an ``asyncio.create_task`` background task
through core's admit → bind → ``run_prepared_turn`` pipeline — the same
orchestration shape the Slack adapter uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from daimon.adapters.teams.lifecycle import FAILURE_MESSAGE
from daimon.adapters.teams.turn_lifecycle import TeamsTurnLifecycle
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    mark_turn_active,
    update_watermark,
)
from daimon.core.turn import turn_deadline
from daimon.core.turn.admission import AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import (
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from microsoft_teams.api import MessageActivity  # pyright: ignore[reportMissingTypeStubs]
from microsoft_teams.apps.routing import (  # pyright: ignore[reportMissingTypeStubs]
    ActivityContext,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# In-process duplicate-activity suppression. The Bot Framework retries a
# delivery when the endpoint is slow to answer; each retry carries the same
# activity id, so a bounded per-process set is enough to drop them. This is
# deliberately not durable: a replay that lands after a restart starts a
# second turn, the same trade the rest of the adapter's restart semantics make.
_SEEN_ACTIVITY_CAP = 10_000

_CONFIG_MISSING_MESSAGE = (
    "No {missing} configured for this conversation. "
    "Ask an administrator to finish setup before messaging again."
)
_RESOLVER_MISS_MESSAGE = (
    "The configured agent or environment no longer exists. "
    "Ask an administrator to configure a replacement."
)
_BALANCE_DEPLETED_MESSAGE = (
    "This deployment's credit is depleted. An administrator can top it up to keep going."
)
_CAP_EXCEEDED_MESSAGE = "The monthly usage cap for this deployment has been reached."
_PREPARATION_FAILED_MESSAGE = (
    "A pending configuration change could not be applied, so no turn ran. "
    "It will be retried at your next message."
)
_SESSION_BUSY_MESSAGE = (
    "The previous turn in this conversation is still finishing. Please try again once it completes."
)
_RESPONDER_CHANGED_MESSAGE = (
    "The agent answering this conversation changed, so this turn did not run. "
    "Please send your message again."
)


@dataclass(frozen=True)
class AuthorizedTeamsActivity:
    """Verified personal-chat facts — the only payload crossing the seam.

    ``external_user_id`` is the sender's Entra (AAD) object id; ``message``
    is the user's text, already normalized and length-bounded by the
    resolver. Message content is never persisted by the adapter.
    """

    tenant_id: uuid.UUID
    external_user_id: str
    conversation_id: str
    activity_id: str
    message: str


@dataclass
class DirectCoreTurnDispatcher:
    """Dispatcher: admit → bind → run against core in a background task.

    Mirrors ``SlackApp._spawn`` + ``_run_thread_turn``: each accepted
    activity becomes a tracked ``asyncio.Task`` running the full turn body;
    the SDK handler returns as soon as the task is spawned.
    """

    turn_deps: TurnDeps
    sessionmaker: async_sessionmaker[AsyncSession]
    _tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    _seen_activities: OrderedDict[str, None] = field(default_factory=OrderedDict[str, None])

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    async def dispatch(
        self,
        ctx: ActivityContext[MessageActivity],
        activity: AuthorizedTeamsActivity,
    ) -> None:
        if activity.activity_id in self._seen_activities:
            log.info("teams.activity.duplicate", activity_id=activity.activity_id)
            return
        self._seen_activities[activity.activity_id] = None
        while len(self._seen_activities) > _SEEN_ACTIVITY_CAP:
            self._seen_activities.popitem(last=False)
        self._spawn(self._run_turn(ctx, activity))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Strong-reference a background task (``SlackApp._spawn`` model)."""
        task = asyncio.create_task(coro, name="teams.turn")
        self._tasks.add(task)
        task.add_done_callback(self._turn_done)
        return task

    def _turn_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("teams.turn.crashed", exc_info=exc)

    async def drain(self, timeout: float = 30.0) -> None:
        """Let in-flight turns finish, then cancel stragglers (lifespan shutdown)."""
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_turn(
        self,
        ctx: ActivityContext[MessageActivity],
        activity: AuthorizedTeamsActivity,
    ) -> None:
        """Turn body: admission → progress message → bind → marker → turn → watermark.

        A trimmed ``SlackApp._run_thread_turn``: personal chat has no thread
        history to replay and no continuations to flush, so the body is the
        core pipeline plus the orphan marker/watermark bookkeeping.
        """
        try:
            admission = await admit(
                self.turn_deps,
                tenant_id=activity.tenant_id,
                platform="teams",
                external_user_id=activity.external_user_id,
                channel_id=activity.conversation_id,
                thread_id=activity.conversation_id,
                now=datetime.now(UTC),
            )
        except MissingTurnConfigError as err:
            log.info("teams.missing_config", missing=list(err.missing))
            await ctx.send(_CONFIG_MISSING_MESSAGE.format(missing=" or ".join(err.missing)))
            return
        except MAResolverMissError as err:
            log.warning("teams.resolver.miss", kind=err.kind, daimon_tag=err.daimon_tag)
            await ctx.send(_RESOLVER_MISS_MESSAGE)
            return
        except AdmissionDenied as err:
            log.info("teams.turn.denied", reason=err.reason)
            await ctx.send(
                _BALANCE_DEPLETED_MESSAGE
                if err.reason == "balance_depleted"
                else _CAP_EXCEEDED_MESSAGE
            )
            return

        cancel = asyncio.Event()
        lifecycle = TeamsTurnLifecycle(stream=ctx.stream)
        # Post the progress message BEFORE bind_session — MA sessions.create
        # can hold for minutes and the user must see something first.
        await lifecycle.post_initial()

        # The marker names the progress message so a boot sweep can edit it
        # if this process dies mid-turn. A Teams message is addressed by
        # (service_url, conversation_id, message_id): the conversation id is
        # the row's thread_id, and the service_url — the region-sharded
        # endpoint the conversation lives on — rides the marker's channel
        # column the same way Slack's channel id does.
        service_url = ctx.conversation_ref.service_url
        marker_mapping_ids: set[uuid.UUID] = set()
        lifecycle_holder: list[TeamsTurnLifecycle] = [lifecycle]
        cancelled = False

        try:
            deadline = turn_deadline(now=datetime.now(UTC))
            try:
                prepared = await bind_session(
                    self.turn_deps,
                    admission,
                    tenant_id=activity.tenant_id,
                    platform="teams",
                    external_user_id=activity.external_user_id,
                    thread_id=activity.conversation_id,
                    session_account_id=admission.account_id,
                    reuse_existing=True,
                    deadline=deadline,
                )
            except SessionPreparationFailed:
                await lifecycle.close_with_text(_PREPARATION_FAILED_MESSAGE)
                return
            except SessionBusyError:
                await lifecycle.close_with_text(_SESSION_BUSY_MESSAGE)
                return
            except SessionAgentMismatch:
                await lifecycle.close_with_text(_RESPONDER_CHANGED_MESSAGE)
                return

            if prepared.mapping_id is not None and lifecycle.message_id is not None:
                async with self.sessionmaker() as marker_session:
                    await mark_turn_active(
                        marker_session,
                        id=prepared.mapping_id,
                        active_turn_message_id=lifecycle.message_id,
                        active_turn_channel_id=service_url,
                        now=datetime.now(UTC),
                    )
                    await marker_session.commit()
                marker_mapping_ids.add(prepared.mapping_id)

            def _recovery_lifecycle(fresh_cancel: asyncio.Event) -> TurnLifecycle:
                # The first attempt's terminal-failure render is held while
                # recovery runs, so the stream was never closed — adopting it
                # keeps the recovered turn's progress and terminal card on the
                # SAME Teams message id (Slack's adopt_status_ts parity), and
                # the marker written above stays valid for the orphan sweep.
                adopted = TeamsTurnLifecycle(stream=ctx.stream, message_id=lifecycle.message_id)
                lifecycle_holder[0] = adopted
                return adopted

            async def _reseed_user_message() -> str:
                return activity.message

            outcome = await run_prepared_turn(
                self.turn_deps,
                prepared,
                tenant_id=activity.tenant_id,
                platform="teams",
                thread_id=activity.conversation_id,
                external_user_id=activity.external_user_id,
                user_message=activity.message,
                lifecycle=lifecycle,
                cancel=cancel,
                reseed_user_message=_reseed_user_message,
                recovery_lifecycle=_recovery_lifecycle,
                deadline=deadline,
            )
            if outcome.mapping_id is not None:
                marker_mapping_ids.add(outcome.mapping_id)

            final_lifecycle = lifecycle_holder[0]
            if outcome.mapping_id is not None and final_lifecycle.final_message_id is not None:
                async with self.sessionmaker() as watermark_session:
                    await update_watermark(
                        watermark_session,
                        id=outcome.mapping_id,
                        watermark_message_id=final_lifecycle.final_message_id,
                    )
                    await watermark_session.commit()
        except asyncio.CancelledError:
            # Classify by WHO cancelled, not by the exception type. Only a
            # cancel() request on THIS dispatcher task — drain() reaping
            # stragglers at shutdown (http_service lifespan) — means the
            # process is going away: the card stays frozen mid-render and
            # the marker MUST survive so the next boot's sweep can edit it.
            # A CancelledError that merely propagated out of a dependency
            # (a timeout, an inner task, a library) while this task's
            # cancellation count is zero is an ordinary failure in a
            # still-running process — render the failure card and let the
            # marker clear below; nothing else ever will.
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                cancelled = True
                log.info("teams.turn.cancelled", conversation_id=activity.conversation_id)
                raise
            log.warning("teams.turn.failed", exc_info=True)
            with contextlib.suppress(Exception):
                await lifecycle.close_with_text(FAILURE_MESSAGE)
            raise
        except Exception:
            # Anything past post_initial must not leave the progress message
            # spinning forever — collapse it to a failure notice, then let the
            # task-level logger record the crash.
            log.warning("teams.turn.failed", exc_info=True)
            with contextlib.suppress(Exception):
                await lifecycle.close_with_text(FAILURE_MESSAGE)
            raise
        finally:
            # Clear the marker only for a turn that reached its own end:
            # completion and ordinary failure already rendered a terminal
            # card, so nothing remains for the sweep. A task cancelled by
            # drain() — flag above, or a cancel() still pending delivery —
            # keeps its marker so the next boot's list_orphaned_turns can
            # find the row and edit the frozen card to interrupted.
            task = asyncio.current_task()
            if not cancelled and (task is None or task.cancelling() == 0):
                for marker_id in marker_mapping_ids:
                    with contextlib.suppress(SQLAlchemyError):
                        async with self.sessionmaker() as clear_session:
                            await clear_active_turn(clear_session, id=marker_id)
                            await clear_session.commit()

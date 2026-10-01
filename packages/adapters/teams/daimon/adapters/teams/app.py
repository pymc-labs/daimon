"""Teams turn orchestration: queueing, the turn body, commands and Cancel.

Mirrors `SlackApp`. One turn per conversation thread at a time; messages that
arrive meanwhile queue silently (Teams bots cannot react) and run after it as
one turn per author. A per-tenant cap sheds load, and no turn starts until the
boot sweep has retired the previous process's turns. A wake poller opens chats
with due handoffs and timers.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from xml.sax.saxutils import escape, quoteattr

import anthropic
import structlog
from daimon.adapters.teams.attachments import BotToken, prepare_attachments
from daimon.adapters.teams.boot_sweep import retire_orphaned_turns
from daimon.adapters.teams.card_actions import toast
from daimon.adapters.teams.commands import (
    CHANNEL_POINTER,
    CommandContext,
    CommandHandler,
    parse_command,
)
from daimon.adapters.teams.credential_requests import TeamsCredentialRequests
from daimon.adapters.teams.identity import (
    DENIED,
    Refusal,
    TeamsInbound,
    canonical_uuid,
    live_tenant_id,
    parse_inbound,
)
from daimon.adapters.teams.lifecycle import (
    SEND_TIMEOUT_S,
    TEAMS_SEND_ERRORS,
    TeamsSender,
    TeamsTurnLifecycle,
    TimedSender,
)
from daimon.adapters.teams.output_delivery import TeamsOutputDelivery
from daimon.adapters.teams.provisioning import provision_configured_tenant
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import route_to_setup
from daimon.adapters.teams.tool_confirmation import TeamsConfirmationCards
from daimon.core.continuity.continuation import check_wake_responder
from daimon.core.continuity.dispatch import dispatch_pending_continuations
from daimon.core.continuity.messages import (
    render_current_work_must_finish,
    render_preparation_failed,
    render_replacement_summary,
    render_responder_changed_without_handoff,
    render_unexpected_loss,
)
from daimon.core.continuity.wakes import WakeThread, run_wake_poller
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.stores.domain import Role, TaskContinuationRow
from daimon.core.stores.tenants import get_tenant, get_turn_cap
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    get_live_thread_session,
    mark_turn_active,
    update_watermark,
)
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    record_turn_card_message,
    retire_turn_card_intent,
)
from daimon.core.teams_threads import conversation_of
from daimon.core.turn import turn_deadline
from daimon.core.turn.admission import Admission, AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.errors import (
    AdmissionDenialReason,
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.gating import should_admit_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.outcomes import observe_turn, record_refusal
from daimon.core.turn.prepare import ContinuityOutcome, bind_session
from daimon.core.turn.protection import protection_state
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import ToolUseBlock
from daimon.core.turn_keys import list_mounted_key_names, render_keys_element
from daimon.core.turn_origin import (
    HandoffNotice,
    build_handoff_notice,
    render_turn_origin,
    turn_origin,
)
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivity,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

_SEEN_ACTIVITY_CAP = 10_000
_RECOVERY_RETRY_DELAY_S = 1.0
_RECOVERY_MAX_RETRY_DELAY_S = 30.0
_FAILED = "Sorry, something went wrong handling that. Please try again."
_SHED = "Too many chats are in flight right now. Try again in a moment."
_BALANCE_DEPLETED = (
    "This organisation's credit is depleted. An admin can top up with `billing` in a 1:1 "
    "chat with me."
)
_CAP_REACHED = "Monthly usage cap reached for this organisation. Ask an admin to adjust it."
_NOT_INVITED = (
    "You aren't on this organisation's list of people who can start a turn. An admin can add you."
)
_PINNED_ELSEWHERE = (
    "This agent only runs in the channels an operator pinned it to, so it can't answer here."
)
# Teams sets no channel budgets, so this is unreachable; the reply keeps the map total.
_CHANNEL_BUDGET = "This channel has used its spending budget. An admin can raise or clear it."
_RESOLVER_MISS = (
    "The configured agent or environment no longer exists. Pick another with `setup` in a "
    "1:1 chat with me, or ask an admin to restore it."
)
_CANCEL_NOT_AUTHOR = "Only the person who started this turn can cancel it."
_CANCEL_TURN_ENDED = "This turn has already finished — there is nothing left to cancel."
_CANCELLING = "Cancelling…"
# Everything a turn can raise that is not a bug in this adapter.
_TURN_ERRORS = (DaimonError, anthropic.APIError, SQLAlchemyError, *TEAMS_SEND_ERRORS)
_BIND_REFUSALS = (SessionPreparationFailed, SessionBusyError, SessionAgentMismatch)
_DENIALS: dict[AdmissionDenialReason, tuple[str, str | None]] = {
    "balance_depleted": ("turn.skipped.over_balance", _BALANCE_DEPLETED),
    "cap_exceeded": ("turn.skipped.over_cap", _CAP_REACHED),
    "invoker_not_allowed": ("turn.skipped.invoker_not_allowed", _NOT_INVITED),
    "agent_pinned_elsewhere": ("turn.skipped.agent_pinned_elsewhere", _PINNED_ELSEWHERE),
    "channel_budget_exceeded": ("turn.skipped.channel_budget_exceeded", _CHANNEL_BUDGET),
    # A protected channel hears nothing, a refusal included.
    "channel_protected": ("turn.skipped.channel_protected", None),
}

LifecycleFactory = Callable[[asyncio.Event, str | None], TeamsTurnLifecycle]
# Builds a continuation turn's handoff notice from what the bind carried across.
HandoffFactory = Callable[[ContinuityOutcome], HandoffNotice]


def _user_message(
    controls: str, inbound: TeamsInbound, *, is_admin: bool, keys: str, prefix: str
) -> str:
    """Host facts, then the person's escaped words, shaped like Slack's context XML."""
    context = f'<channel platform="teams" id={quoteattr(inbound.channel_id)}/>'
    query = (
        f"<user_query author_id={quoteattr(inbound.user_id)} "
        f'is_admin="{str(is_admin).lower()}">{escape(inbound.text)}</user_query>'
    )
    return "\n".join(
        [
            controls,
            "<context>",
            context,
            *([keys] if keys else []),
            "</context>",
            "",
            prefix + query,
        ]
    )


def _compose_queued(queued: list[TeamsInbound]) -> list[TeamsInbound]:
    """One turn per author, replying where that author's last message came from."""
    by_author: dict[str, list[TeamsInbound]] = {}
    for item in queued:
        by_author.setdefault(item.user_id, []).append(item)
    return [
        dataclasses.replace(
            items[-1],
            text="\n\n".join(item.text for item in items),
            files=tuple(file for item in items for file in item.files),
        )
        for items in by_author.values()
    ]


def _admission_refusal(
    err: MissingTurnConfigError | MAResolverMissError | AdmissionDenied, tenant_id: uuid.UUID
) -> str | None:
    """Log a refused admission; what to tell the person, if anything."""
    if isinstance(err, MissingTurnConfigError):
        log.info("teams.missing_config", missing=list(err.missing))
        return f"No {' or '.join(err.missing)} configured here. Ask the operator to set one."
    if isinstance(err, MAResolverMissError):
        log.warning("teams.resolver.miss", kind=err.kind, daimon_tag=err.daimon_tag)
        return _RESOLVER_MISS
    event, copy = _DENIALS[err.reason]
    log.info(event, tenant_id=str(tenant_id))
    return copy


class TeamsApp:
    """Handlers the SDK app routes to, plus the turn state they share."""

    def __init__(
        self,
        *,
        runtime: TeamsRuntime,
        sender: TeamsSender,
        commands: Mapping[str, CommandHandler],
        bot_token: BotToken,
    ) -> None:
        teams = runtime.settings.teams
        if teams is None:
            raise ValueError("TeamsApp requires Teams settings")
        self.runtime = runtime
        self._teams = teams
        # Known before any read, so `_may_post` can gate every post.
        self._tenant_id = derive_tenant_uuid(platform="teams", workspace_id=teams.tenant_id)
        self._sender = TimedSender(sender)
        self._commands = commands
        self._bot_token = bot_token
        self.outputs = TeamsOutputDelivery(runtime=runtime, sender=self._sender, spawn=self.spawn)
        self.credentials = TeamsCredentialRequests(
            runtime=runtime,
            sender=self._sender,
            spawn=self.spawn,
            dispatch=self.dispatch_after_input,
        )
        # Tool-write confirmation cards awaiting a click.
        self.confirmations = TeamsConfirmationCards(self._sender)
        self._processing: set[str] = set()
        self._pending: dict[str, list[TeamsInbound]] = {}
        self._inflight: dict[uuid.UUID, int] = {}
        # cancel_key (the card intent id) -> (cancel Event, author's Entra id).
        self._cancel_registry: dict[str, tuple[asyncio.Event, str]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        # Bot Framework retries a slow delivery with the same activity id.
        # Not durable: a retry landing after a restart runs a second turn.
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._recovery: asyncio.Task[None] | None = None
        self._wake_poller: asyncio.Task[None] | None = None
        # When each busy conversation last got a message: a newer one supersedes
        # queued continuation work (Teams cannot list a conversation's history).
        self._last_message_at: dict[str, datetime] = {}
        # Dispatches that found their chat busy, by thread key: the service URL to
        # use and whether the cap holds them back (only wakes; a saved input wins).
        self._deferred_dispatch: dict[str, tuple[uuid.UUID, str | None, bool]] = {}
        self.draining = False

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("teams.task_failed", task_name=task.get_name(), exc_info=task.exception())

    def start(self) -> asyncio.Task[None]:
        """Start the boot sweep (turns wait for it), tenant provisioning and the wake poller."""
        if self._recovery is None:
            self._recovery = asyncio.create_task(self._recover(), name="teams.boot-sweep")
            self.spawn(self._provision(), name="teams.provision")
            if self._teams.enabled:  # a disabled deployment runs no timers either
                poller = run_wake_poller(
                    self.runtime.sessionmaker,
                    platform="teams",
                    open_thread=self._open_wake_thread,
                    should_stop=lambda: self.draining,
                )
                self._wake_poller = asyncio.create_task(poller, name="teams.wake-poller")
        return self._recovery

    async def _open_wake_thread(self, wake: WakeThread) -> bool:
        """The wake poller's hook: run a chat's due wakes via `dispatch_after_input`.

        False (a missing or archived tenant) makes the poller push the rows back.
        A wake stores no service URL, so its sends use the SDK default.
        """
        if self.draining:
            return True
        async with self.runtime.sessionmaker() as session:
            tenant = await get_tenant(session, wake.tenant_id)
        # Another organisation's rows (a re-pointed or shared database) are not ours to run.
        if tenant is None or tenant.archived_at is not None or tenant.id != self._tenant_id:
            return False
        wake_dispatch = self.dispatch_after_input(wake.tenant_id, wake.thread_id, None, capped=True)
        self.spawn(wake_dispatch, name="teams.wake")
        return True

    async def _recover(self) -> None:
        delay = _RECOVERY_RETRY_DELAY_S
        while True:
            try:
                await retire_orphaned_turns(
                    anthropic=self.runtime.anthropic,
                    sessionmaker=self.runtime.sessionmaker,
                    sender=self._sender,
                    now=datetime.now(UTC),
                )
                return
            except Exception:
                # Admission waits on this sweep: retry rather than wedge turns.
                log.exception("teams.turn.orphan_recovery_failed", retry_delay_s=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECOVERY_MAX_RETRY_DELAY_S)

    async def _provision(self) -> None:
        settings = self.runtime.settings
        public_url = str(settings.mcp.public_url) if settings.mcp.public_url else None
        await provision_configured_tenant(
            anthropic=self.runtime.anthropic,
            sessionmaker=self.runtime.sessionmaker,
            defaults_root=settings.defaults_root,
            deployment_default=self.runtime.deployment_default,
            public_url=public_url,
            entra_tenant_id=self._teams.tenant_id,
            signup_credit=settings.billing.signup_credit,
            admin_user_ids=self._teams.admin_user_ids,
        )

    async def drain(self, timeout: float) -> None:
        """Stop admitting, give turns and the sweep `timeout`, then cancel the rest.

        A cancelled turn keeps its marker and card intent, so the next boot's
        sweep edits its card to interrupted.
        """
        self.draining = True
        if self._wake_poller is not None:
            # Dispatches it spawned drain with the turns; due rows wait for the next boot.
            self._wake_poller.cancel()
            await asyncio.gather(self._wake_poller, return_exceptions=True)
        tasks = set(self._tasks)
        if self._recovery is not None and not self._recovery.done():
            tasks.add(self._recovery)
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _role(self, inbound: TeamsInbound) -> Role:
        return Role.ADMIN if inbound.user_id in self._teams.admin_user_ids else Role.USER

    def _first_delivery(self, conversation_id: str, activity_id: str) -> bool:
        key = (conversation_id, activity_id)
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > _SEEN_ACTIVITY_CAP:
            self._seen.popitem(last=False)
        return True

    async def handle_message(self, ctx: ActivityContext[MessageActivity]) -> None:
        """SDK message handler: verify, then hand off. Returns before any turn work."""
        activity = ctx.activity
        if self.draining or not self._first_delivery(activity.conversation.id, activity.id):
            return
        parsed = parse_inbound(
            activity,
            configured_tenant=self._teams.tenant_id,
            service_url=ctx.conversation_ref.service_url,
        )
        if isinstance(parsed, Refusal):
            conversation_id = activity.conversation.id
            if parsed.text is not None and await self._may_post(
                conversation_id.split(";", 1)[0], conversation_id
            ):
                with contextlib.suppress(*TEAMS_SEND_ERRORS):
                    await asyncio.wait_for(ctx.reply(parsed.text), SEND_TIMEOUT_S)
            return
        self.spawn(self._handle(parsed), name="teams.turn")

    async def _say(self, inbound: TeamsInbound, text: str) -> None:
        await self._sender.send(
            inbound.conversation_id,
            MessageActivityInput(text=text),
            service_url=inbound.service_url,
        )

    async def _may_post(self, channel_id: str, thread_id: str) -> bool:
        """True only for an unprotected target; a protected or unreadable one gets nothing."""
        state = await protection_state(
            self.runtime.sessionmaker,
            tenant_id=self._tenant_id,
            channel_id=channel_id,
            thread_id=thread_id,
        )
        if not state.may_post:
            log.info(
                "turn.skipped.channel_protected",
                channel_id=channel_id,
                thread_id=thread_id,
                state=state.value,
            )
        return state.may_post

    async def _handle(self, inbound: TeamsInbound) -> None:
        """Error boundary around tenant lookup, routing, commands and the turn loop.

        May-post is decided first, so no post below (denial, pointer, shed, error) skips it.
        """
        if not await self._may_post(inbound.channel_id, inbound.thread_id):
            return
        try:
            await self._route(inbound)
        except _TURN_ERRORS as exc:
            log.error("teams.message.failed", conversation_id=inbound.conversation_id, exc_info=exc)
            capture_exception_with_scope(exc)
            with contextlib.suppress(*TEAMS_SEND_ERRORS):
                await self._say(inbound, _FAILED)

    async def _route(self, inbound: TeamsInbound) -> None:
        if self._recovery is not None:
            await asyncio.shield(self._recovery)
        tenant_id = await live_tenant_id(self.runtime.sessionmaker, inbound.entra_tenant_id)
        if tenant_id is None:
            await self._say(inbound, DENIED)
            return
        command = parse_command(inbound.text, self._commands)
        if command is not None:
            name, args = command
            if inbound.kind != "dm":
                await self._say(inbound, CHANNEL_POINTER.format(name=name))
                return
            inbound = await route_to_setup(self.runtime.sessionmaker, inbound, tenant_id)
            await self._commands[name](
                CommandContext(
                    inbound=inbound,
                    tenant_id=tenant_id,
                    args=args,
                    is_admin=self._role(inbound) is Role.ADMIN,
                    runtime=self.runtime,
                    send=functools.partial(
                        self._sender.send, inbound.conversation_id, service_url=inbound.service_url
                    ),
                )
            )
            return
        await self._orchestrate(inbound, tenant_id)

    async def _turn_cap(self, tenant_id: uuid.UUID) -> int:
        default = self._teams.max_concurrent_turns_per_tenant
        return await get_turn_cap(self.runtime.sessionmaker, tenant_id=tenant_id, default=default)

    async def _orchestrate(self, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        key = inbound.conversation_id
        # Read first: no await may split the queue check from the claim below.
        cap = await self._turn_cap(tenant_id)
        if key in self._processing:
            self._last_message_at[key] = datetime.now(UTC)
            self._pending.setdefault(key, []).append(inbound)
            return
        count = self._inflight.get(tenant_id, 0)
        if not should_admit_turn(current_in_flight=count, cap=cap):
            record_refusal(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="teams",
                channel_id=inbound.channel_id,
                thread_id=inbound.thread_id,
            )
            log.info(
                "turn.skipped.concurrency_shed", tenant_id=str(tenant_id), in_flight=count, cap=cap
            )
            await self._say(inbound, _SHED)
            return
        self._last_message_at[key] = datetime.now(UTC)
        async with self._holding(key, tenant_id):
            await self._run_turns(key, tenant_id, [inbound])

    @contextlib.asynccontextmanager
    async def _holding(self, key: str, tenant_id: uuid.UUID) -> AsyncIterator[None]:
        """Hold a chat and a tenant turn slot; on exit, answer what could not run."""
        self._inflight[tenant_id] = self._inflight.get(tenant_id, 0) + 1
        self._processing.add(key)
        try:
            yield
        finally:
            self._release(key)
            if (remaining := self._inflight.pop(tenant_id, 1) - 1) > 0:
                self._inflight[tenant_id] = remaining
            for item in self._pending.pop(key, []):
                with contextlib.suppress(*TEAMS_SEND_ERRORS):
                    await self._say(item, _FAILED)

    async def _run_turns(self, key: str, tenant_id: uuid.UUID, turns: list[TeamsInbound]) -> None:
        """Run `turns`, then what queued behind them, in order, until the queue is empty."""
        while turns:
            for queued in turns:
                # Routed now, not on arrival: the setup conversation may have ended since.
                turn = await route_to_setup(self.runtime.sessionmaker, queued, tenant_id)
                await self._run_turn_guarded(turn, tenant_id)
                await self._dispatch_continuations(turn.thread_id, tenant_id, turn.service_url)
            turns = _compose_queued(self._pending.pop(key, []))

    async def _run_turn_guarded(self, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        """Error boundary for failures before the status card exists."""
        try:
            await self._run_turn(inbound, tenant_id)
        except _TURN_ERRORS as exc:
            log.error("teams.turn.failed", conversation_id=inbound.conversation_id, exc_info=exc)
            capture_exception_with_scope(exc)
            with contextlib.suppress(*TEAMS_SEND_ERRORS):
                await self._say(inbound, _FAILED)

    async def _run_turn(
        self,
        inbound: TeamsInbound,
        tenant_id: uuid.UUID,
        *,
        handoff: HandoffFactory | None = None,
        reraise: bool = False,
        continuation: TaskContinuationRow | None = None,
    ) -> None:
        """The turn inside one outcome observation, so a failure before bind still records."""
        with observe_turn(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="teams",
            channel_id=inbound.channel_id,
            thread_id=inbound.thread_id,
            origin="chat" if continuation is None else "handoff",
        ):
            await self._run_turn_observed(
                inbound, tenant_id, handoff=handoff, reraise=reraise, continuation=continuation
            )

    async def _run_turn_observed(
        self,
        inbound: TeamsInbound,
        tenant_id: uuid.UUID,
        *,
        handoff: HandoffFactory | None,
        reraise: bool,
        continuation: TaskContinuationRow | None,
    ) -> None:
        """Turn body: admit → card → bind → marker → run → watermark. Mirrors Slack's.

        `reraise` (continuations) re-raises a refusal or failure after telling
        the person (nothing, in a protected channel), so the dispatcher can
        settle or re-queue the work.
        """
        deps = self.runtime.turn_deps
        try:
            admission = await admit(
                deps,
                tenant_id=tenant_id,
                platform="teams",
                external_user_id=inbound.user_id,
                channel_id=inbound.channel_id,
                thread_id=inbound.thread_id,
                role=self._role(inbound),
                now=datetime.now(UTC),
                is_dm=inbound.kind == "dm",
            )
        except (MissingTurnConfigError, MAResolverMissError, AdmissionDenied) as err:
            refusal = _admission_refusal(err, tenant_id)
            if refusal is not None and continuation is None:  # queued work settles silently
                await self._say(inbound, refusal)
            if reraise:
                raise
            return
        if continuation is not None:
            # A wake runs only as the agent it was queued for: refused before any card.
            check_wake_responder(
                reason=continuation.reason,
                target_ma_agent_id=continuation.target_ma_agent_id,
                target_name=continuation.target_name,
                admitted_ma_agent_id=admission.agent.id,
                admitted_name=admission.agent.name,
                asking_ma_agent_id=await self._asking_agent_id(continuation, tenant_id),
            )

        agent = admission.agent
        # Committed before the post so a lost response still leaves a record.
        async with self.runtime.sessionmaker.begin() as session:
            intent = await create_turn_card_intent(
                session,
                tenant_id=tenant_id,
                platform="teams",
                thread_id=inbound.conversation_id,
                turn_token=uuid.uuid4(),
                channel_id=inbound.conversation_id,
            )
        cancel_key = intent.id.hex
        # Every attempt's lifecycle; dead-session recovery adds one, the last is current.
        holder: list[TeamsTurnLifecycle] = []

        def new_lifecycle(cancel: asyncio.Event, adopt: str | None) -> TeamsTurnLifecycle:
            self._cancel_registry[cancel_key] = (cancel, inbound.user_id)
            attempt = TeamsTurnLifecycle(
                sender=self._sender,
                conversation_id=inbound.conversation_id,
                service_url=inbound.service_url,
                cancel_key=cancel_key,
                agent_name=agent.name,
                model_id=agent.model.id,
                adopt_message_id=adopt,
                sessionmaker=self.runtime.sessionmaker,
                tenant_id=tenant_id,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
            )
            holder.append(attempt)
            return attempt

        cancel = asyncio.Event()
        lifecycle = new_lifecycle(cancel, None)
        markers: set[uuid.UUID] = set()
        try:
            # Posted before bind: session creation can take minutes.
            await lifecycle.post_initial()
            try:
                # A gate: no untracked turn runs behind a visible card.
                async with self.runtime.sessionmaker.begin() as session:
                    recorded = await record_turn_card_message(
                        session, id=intent.id, message_id=lifecycle.message_id or ""
                    )
                if not recorded:
                    raise DaimonError("Teams card intent could not record its message id")
                await self._bind_and_run(
                    inbound,
                    tenant_id,
                    admission,
                    cancel,
                    holder,
                    markers,
                    new_lifecycle,
                    handoff=handoff,
                    reraise=reraise,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if reraise and isinstance(exc, _BIND_REFUSALS):
                    raise  # Already told the person; the dispatcher settles it.
                # The card exists: collapse it rather than post a second message.
                log.error(
                    "teams.turn.failed", conversation_id=inbound.conversation_id, exc_info=exc
                )
                capture_exception_with_scope(exc)
                await holder[-1].close_with_notice(_FAILED)
                if reraise:
                    raise
        finally:
            self._cancel_registry.pop(cancel_key, None)
            closed = any(each.card_closed for each in holder)
            task = asyncio.current_task()
            # A closed card shows its outcome; the sweep must never overwrite it.
            if closed or task is None or task.cancelling() == 0:
                await asyncio.shield(self._settle(intent.id, lifecycle.message_id, closed, markers))

    async def _refusal(
        self, error: SessionPreparationFailed | SessionBusyError | SessionAgentMismatch, name: str
    ) -> str:
        if isinstance(error, SessionPreparationFailed):
            return render_preparation_failed(name)
        if isinstance(error, SessionBusyError):
            return render_current_work_must_finish(name, handoff=True)
        owner = "the previous agent"
        with contextlib.suppress(anthropic.APIStatusError):
            owner = (await self.runtime.anthropic.beta.agents.retrieve(error.source_agent_id)).name
        return render_responder_changed_without_handoff(
            new_responder=name, owner=owner, channel="this chat"
        )

    async def _bind_and_run(
        self,
        inbound: TeamsInbound,
        tenant_id: uuid.UUID,
        admission: Admission,
        cancel: asyncio.Event,
        holder: list[TeamsTurnLifecycle],
        markers: set[uuid.UUID],
        new_lifecycle: LifecycleFactory,
        *,
        handoff: HandoffFactory | None,
        reraise: bool,
    ) -> None:
        deps = self.runtime.turn_deps
        lifecycle = holder[-1]
        deadline = turn_deadline(now=datetime.now(UTC))
        try:
            prepared = await bind_session(
                deps,
                admission,
                tenant_id=tenant_id,
                platform="teams",
                external_user_id=inbound.user_id,
                thread_id=inbound.thread_id,
                session_account_id=admission.account_id,
                reuse_existing=True,
                deadline=deadline,
            )
        except _BIND_REFUSALS as error:
            await lifecycle.close_with_notice(await self._refusal(error, admission.agent.name))
            if reraise:
                raise
            return

        summary: str | None = None
        if prepared.continuity.state == "replaced" and prepared.continuity.transfer_kind:
            summary = render_replacement_summary(prepared.continuity.transfer_kind, lost=[])
            lifecycle.answer_prefix = summary
        if prepared.mapping_id is not None and lifecycle.message_id is not None:
            async with self.runtime.sessionmaker.begin() as session:
                await mark_turn_active(
                    session,
                    id=prepared.mapping_id,
                    active_turn_message_id=lifecycle.message_id,
                    active_turn_channel_id=inbound.conversation_id,
                    now=datetime.now(UTC),
                )
            markers.add(prepared.mapping_id)

        def recovery_lifecycle(fresh_cancel: asyncio.Event) -> TurnLifecycle:
            # Keep rendering into the card the person is already watching.
            adopted = new_lifecycle(fresh_cancel, lifecycle.message_id)
            adopted.answer_prefix = lifecycle.answer_prefix
            return adopted

        attachments = await prepare_attachments(
            self.runtime.http_client,
            inbound.files,
            bot_token=self._bot_token,
            service_url=inbound.service_url,
        )
        if attachments.notice is not None:
            await self._say(inbound, attachments.notice)
        config = admission.config
        async with turn_origin(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            platform="teams",
            parent_channel_id=inbound.channel_id,
            thread_id=inbound.thread_id,
            responder_ma_agent_id=str(admission.agent.id),
            responder_name=config.agent_name or admission.agent.name,
            role=self._role(inbound),
            configuration_target_ma_agent_id=config.configuration_target_ma_agent_id,
            configuration_target_name=config.configuration_target_name,
            is_setup=config.thread_binding_kind == "setup",
        ) as origin:
            notice = handoff(prepared.continuity) if handoff is not None else None
            quiet = notice is not None or prepared.continuity.state == "continued"
            controls = render_turn_origin(
                origin,
                responder_handle=f"@{inbound.bot_name}" if inbound.bot_name else None,
                session_state=None if quiet else prepared.continuity.session_state(),
                handoff=notice,
            )
            message = _user_message(
                controls,
                inbound,
                is_admin=self._role(inbound) is Role.ADMIN,
                keys=render_keys_element(await self._key_names(tenant_id, inbound, admission)),
                prefix=attachments.prefix,
            )

            async def reseed() -> str:
                return message

            outcome = await run_prepared_turn(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform="teams",
                thread_id=inbound.thread_id,
                external_user_id=inbound.user_id,
                origin="handoff" if notice is not None else "chat",
                user_message=message,
                lifecycle=lifecycle,
                cancel=cancel,
                reseed_user_message=reseed,
                recovery_lifecycle=recovery_lifecycle,
                image_blocks=attachments.image_blocks or None,
                deadline=deadline,
                confirm_write=self.confirmations.hook(
                    conversation_id=inbound.conversation_id, service_url=inbound.service_url
                ),
            )
        if outcome.mapping_id is not None:
            markers.add(outcome.mapping_id)
        if any(isinstance(block, ToolUseBlock) for block in outcome.state.content):
            sweep = self.outputs.sweep(inbound, outcome.ma_session_id)
            self.spawn(sweep, name="teams.output-sweep")
        final = holder[-1]
        if outcome.continuity.state == "replaced_after_loss":
            kind: Literal["transcript", "history"] = (
                "transcript" if outcome.continuity.transfer_kind == "transcript" else "history"
            )
            loss = render_unexpected_loss(kind)
            if not await final.prepend_revealed_answer(loss):
                await self._say(inbound, loss)
        if summary is not None and not final.answer_prefix_applied:
            await self._say(inbound, summary)
        if outcome.mapping_id is not None and final.final_message_id is not None:
            async with self.runtime.sessionmaker.begin() as session:
                await update_watermark(
                    session, id=outcome.mapping_id, watermark_message_id=final.final_message_id
                )
        if prepared.continuity.pending:
            await self._say(
                inbound, render_current_work_must_finish(admission.agent.name, handoff=False)
            )

    async def _key_names(
        self, tenant_id: uuid.UUID, inbound: TeamsInbound, admission: Admission
    ) -> tuple[str, ...]:
        """This agent's stored key names while the mounted `.env` still matches them."""
        async with self.runtime.sessionmaker() as session:
            live = await get_live_thread_session(
                session,
                tenant_id=tenant_id,
                platform="teams",
                thread_id=inbound.thread_id,
                account_id=admission.account_id,
            )
            config = None if live is None else live.effective_config
            return await list_mounted_key_names(
                session,
                tenant_id=tenant_id,
                agent_id=derive_agent_uuid(
                    tenant_id=tenant_id, ma_agent_id=str(admission.agent.id)
                ),
                env_sha256=None if config is None else config.env_sha256,
            )

    def _release(self, key: str) -> None:
        """Free a conversation; re-run private-input dispatches that found it busy."""
        self._processing.discard(key)
        self._last_message_at.pop(key, None)
        for thread_id in [t for t in self._deferred_dispatch if conversation_of(t) == key]:
            tenant_id, service_url, capped = self._deferred_dispatch.pop(thread_id)
            if not self.draining:
                resume = self.dispatch_after_input(tenant_id, thread_id, service_url, capped=capped)
                self.spawn(resume, name="teams.resume")

    async def dispatch_after_input(
        self, tenant_id: uuid.UUID, thread_id: str, service_url: str | None, *, capped: bool = False
    ) -> None:
        """Run what a saved private input or a due wake queued here, from outside a turn.

        A busy conversation is left to its turn's tail dispatch, and re-run on
        release in case that tail already passed. Messages queued meanwhile follow.
        A `capped` dispatch (a wake) waits while the tenant is at its turn cap:
        its rows stay due, so the next poll retries them.
        """
        if self.draining:
            return
        if self._recovery is not None:
            await asyncio.shield(self._recovery)
        conversation_id = conversation_of(thread_id)
        # Only a wake is capped; read before the busy check, as `_orchestrate` does.
        cap = await self._turn_cap(tenant_id) if capped else 0
        if conversation_id in self._processing:
            # A wake's None must not drop a saved input's regional service URL, and
            # the cap never holds back a saved input's resume.
            _, previous_url, previous_capped = self._deferred_dispatch.get(
                thread_id, (tenant_id, None, True)
            )
            self._deferred_dispatch[thread_id] = (
                tenant_id,
                service_url or previous_url,
                capped and previous_capped,
            )
            return
        if capped and not should_admit_turn(
            current_in_flight=self._inflight.get(tenant_id, 0), cap=cap
        ):
            return
        async with self._holding(conversation_id, tenant_id):
            await self._dispatch_continuations(thread_id, tenant_id, service_url)
            queued = _compose_queued(self._pending.pop(conversation_id, []))
            await self._run_turns(conversation_id, tenant_id, queued)

    async def _dispatch_continuations(
        self, thread_id: str, tenant_id: uuid.UUID, service_url: str | None
    ) -> None:
        """Run what a handoff, saved input or wake queued in a thread. The caller holds its chat."""
        key = conversation_of(thread_id)

        async def notify(text: str) -> None:
            with contextlib.suppress(*TEAMS_SEND_ERRORS):
                await self._sender.send(
                    key, MessageActivityInput(text=text), service_url=service_url
                )

        async def latest_message_at(_row: TaskContinuationRow) -> datetime | None:
            return self._last_message_at.get(key)

        async def run(row: TaskContinuationRow, seed: str) -> None:
            await self._run_continuation(row, seed, tenant_id, service_url)

        try:
            await dispatch_pending_continuations(
                self.runtime.sessionmaker,
                self.runtime.anthropic,
                tenant_id=tenant_id,
                platform="teams",
                thread_id=thread_id,
                run_follow_up=run,
                post_notice=notify,
                latest_user_message_at=latest_message_at,
                # Any failure was already shown to the person; the row must still settle.
                dispatch_errors=(Exception,),
            )
        except _TURN_ERRORS as exc:
            log.error("teams.continuation.dispatch_failed", conversation_id=key, exc_info=exc)

    async def _asking_agent_id(self, row: TaskContinuationRow, tenant_id: uuid.UUID) -> str | None:
        """The agent of the requester's live session here, which a private input resumes."""
        if row.reason != "private_input_applied":
            return None
        async with self.runtime.sessionmaker() as session:
            live = await get_live_thread_session(
                session,
                tenant_id=tenant_id,
                platform="teams",
                thread_id=row.thread_id,
                account_id=row.requester_account_id,
            )
        return live.ma_agent_id if live is not None else None

    async def _run_continuation(
        self,
        row: TaskContinuationRow,
        seed: str,
        tenant_id: uuid.UUID,
        service_url: str | None,
    ) -> None:
        """The receiving agent's first turn, run as the requester on the ordinary path."""
        handoff: HandoffFactory | None = None
        if row.reason == "task_handoff":
            # Read before the bind, which supersedes the outgoing agent's session.
            async with self.runtime.sessionmaker() as session:
                live = await get_live_thread_session(
                    session,
                    tenant_id=tenant_id,
                    platform="teams",
                    thread_id=row.thread_id,
                    account_id=row.requester_account_id,
                )
            config = live.effective_config if live is not None else None
            from_name = config.agent_name if config is not None else None
            from_id = live.ma_agent_id if live is not None else None
            handoff = lambda continuity: build_handoff_notice(  # noqa: E731
                from_name=from_name,
                from_ma_agent_id=from_id,
                requested_by="the requester",
                requested_work=seed,
                transfer_kind=continuity.transfer_kind,
            )

        conversation = conversation_of(row.thread_id)
        inbound = TeamsInbound(
            kind="dm" if conversation == row.parent_channel_id else "channel",
            entra_tenant_id=self._teams.tenant_id,
            user_id=row.requester_external_user_id,
            conversation_id=conversation,
            setup_thread_id=row.thread_id if row.thread_id != conversation else None,
            channel_id=row.parent_channel_id,
            activity_id=str(row.id),
            text=seed,
            service_url=service_url,
        )
        await self._run_turn(inbound, tenant_id, handoff=handoff, reraise=True, continuation=row)

    async def _settle(
        self,
        intent_id: uuid.UUID,
        message_id: str | None,
        closed: bool,
        markers: set[uuid.UUID],
    ) -> None:
        """Retire the intent of a closed card and clear the turn markers.

        A card still showing a live turn keeps its intent so the next boot's
        sweep marks it interrupted. Store failures are logged, never raised:
        the next boot's sweep is the backstop.
        """
        try:
            async with self.runtime.sessionmaker.begin() as session:
                if closed:
                    await retire_turn_card_intent(
                        session, id=intent_id, expected_message_id=message_id
                    )
                for marker_id in markers:
                    await clear_active_turn(session, id=marker_id)
        except SQLAlchemyError:
            log.exception("teams.turn.settle_failed", intent_id=str(intent_id))

    async def handle_cancel(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        """Cancel button: only the turn's author may stop it."""
        action = ctx.activity.value.action
        entry = self._cancel_registry.get(str(action.data.get("turn") or ""))
        if entry is None:
            return toast(_CANCEL_TURN_ENDED)
        cancel, author = entry
        if canonical_uuid(ctx.activity.from_.aad_object_id) != author:
            return toast(_CANCEL_NOT_AUTHOR)
        cancel.set()
        return toast(_CANCELLING)

"""Teams turn orchestration: queueing, the turn body, commands and Cancel.

Mirrors `SlackApp`. One turn per conversation thread at a time; messages that
arrive meanwhile queue silently (Teams bots cannot react) and run after it as
one turn per author. A per-tenant cap sheds load, and no turn starts until the
boot sweep has retired the previous process's turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import uuid
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

import anthropic
import structlog
from daimon.adapters.teams.boot_sweep import retire_orphaned_turns
from daimon.adapters.teams.commands import (
    CHANNEL_POINTER,
    CommandContext,
    CommandHandler,
    parse_command,
)
from daimon.adapters.teams.identity import (
    DENIED,
    Refusal,
    TeamsInbound,
    canonical_uuid,
    parse_inbound,
    resolve_tenant,
)
from daimon.adapters.teams.lifecycle import (
    SEND_TIMEOUT_S,
    TEAMS_SEND_ERRORS,
    TeamsSender,
    TeamsTurnLifecycle,
    TimedSender,
)
from daimon.adapters.teams.provisioning import provision_configured_tenant
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.continuity.messages import (
    render_current_work_must_finish,
    render_preparation_failed,
    render_replacement_summary,
    render_responder_changed_without_handoff,
    render_unexpected_loss,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    mark_turn_active,
    update_watermark,
)
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    record_turn_card_message,
    retire_turn_card_intent,
)
from daimon.core.turn import turn_deadline
from daimon.core.turn.admission import Admission, AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.errors import (
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.gating import should_admit_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import ContinuityOutcome, bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn_origin import SessionState, render_turn_origin, turn_origin
from microsoft_teams.api import (
    AdaptiveCardActionMessageResponse,
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
_BALANCE_DEPLETED = "This deployment's credit is depleted. Ask the operator to top it up."
_CAP_REACHED = "Monthly usage cap reached. Ask the operator to adjust it."
_RESOLVER_MISS = (
    "The configured agent or environment no longer exists. Ask the operator to restore it."
)
_CANCEL_NOT_AUTHOR = "Only the person who started this turn can cancel it."
_CANCEL_TURN_ENDED = "This turn has already finished — there is nothing left to cancel."
_CANCELLING = "Cancelling…"
# Everything a turn can raise that is not a bug in this adapter.
_TURN_ERRORS = (DaimonError, anthropic.APIError, SQLAlchemyError, *TEAMS_SEND_ERRORS)
_DENIAL_EVENTS = {"balance_depleted": "turn.skipped.over_balance", "cap": "turn.skipped.over_cap"}

LifecycleFactory = Callable[[asyncio.Event, str | None], TeamsTurnLifecycle]


def session_state(continuity: ContinuityOutcome) -> SessionState:
    """The bind's continuity as turn-control facts. Duplicated from the Slack adapter."""
    lost: tuple[str, ...] = ()
    if continuity.transfer_kind == "transcript":
        lost = ("working files",)
    elif continuity.transfer_kind == "history":
        lost = ("working files", "earlier conversation")
    return SessionState(state=continuity.state, applied=tuple(continuity.applied), lost=lost)


def _compose_queued(items: list[TeamsInbound]) -> TeamsInbound:
    """One author's queued messages as one turn, replying where the last one came from."""
    return dataclasses.replace(items[-1], text="\n\n".join(item.text for item in items))


class TeamsApp:
    """Handlers the SDK app routes to, plus the turn state they share."""

    def __init__(
        self,
        *,
        runtime: TeamsRuntime,
        sender: TeamsSender,
        commands: Mapping[str, CommandHandler],
    ) -> None:
        teams = runtime.settings.teams
        if teams is None:
            raise ValueError("TeamsApp requires Teams settings")
        self.runtime = runtime
        self._teams = teams
        self._sender = TimedSender(sender)
        self._commands = commands
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
        self.draining = False

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def _spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("teams.task_failed", task_name=task.get_name(), exc_info=task.exception())

    def start(self) -> asyncio.Task[None]:
        """Start the boot sweep (turns wait for it) and tenant provisioning."""
        if self._recovery is None:
            self._recovery = asyncio.create_task(self._recover(), name="teams.boot-sweep")
            self._spawn(self._provision(), name="teams.provision")
        return self._recovery

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
        )

    async def drain(self, timeout: float) -> None:
        """Stop admitting, give turns and the sweep `timeout`, then cancel the rest.

        A cancelled turn keeps its marker and card intent, so the next boot's
        sweep edits its card to interrupted.
        """
        self.draining = True
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
            if parsed.text is not None:
                with contextlib.suppress(*TEAMS_SEND_ERRORS):
                    await asyncio.wait_for(ctx.reply(parsed.text), SEND_TIMEOUT_S)
            return
        self._spawn(self._handle(parsed), name="teams.turn")

    async def _say(self, inbound: TeamsInbound, text: str) -> None:
        await self._sender.send(
            inbound.conversation_id,
            MessageActivityInput(text=text),
            service_url=inbound.service_url,
        )

    async def _handle(self, inbound: TeamsInbound) -> None:
        if self._recovery is not None:
            await asyncio.shield(self._recovery)
        tenant_id = await resolve_tenant(self.runtime.sessionmaker, inbound)
        if tenant_id is None:
            await self._say(inbound, DENIED)
            return
        command = parse_command(inbound.text, self._commands)
        if command is not None:
            name, args = command
            if inbound.kind != "dm":
                await self._say(inbound, CHANNEL_POINTER.format(name=name))
                return
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

    async def _orchestrate(self, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        key = inbound.conversation_id
        if key in self._processing:
            self._pending.setdefault(key, []).append(inbound)
            return
        cap = self._teams.max_concurrent_turns_per_tenant
        count = self._inflight.get(tenant_id, 0)
        if not should_admit_turn(current_in_flight=count, cap=cap):
            log.info(
                "turn.skipped.concurrency_shed", tenant_id=str(tenant_id), in_flight=count, cap=cap
            )
            await self._say(inbound, _SHED)
            return
        self._inflight[tenant_id] = count + 1
        self._processing.add(key)
        try:
            await self._run_turn_guarded(inbound, tenant_id)
            while queued := self._pending.pop(key, []):
                by_author: dict[str, list[TeamsInbound]] = {}
                for item in queued:
                    by_author.setdefault(item.user_id, []).append(item)
                for items in by_author.values():
                    await self._run_turn_guarded(_compose_queued(items), tenant_id)
        finally:
            self._processing.discard(key)
            remaining = self._inflight.get(tenant_id, 1) - 1
            if remaining > 0:
                self._inflight[tenant_id] = remaining
            else:
                self._inflight.pop(tenant_id, None)
            for item in self._pending.pop(key, []):
                with contextlib.suppress(*TEAMS_SEND_ERRORS):
                    await self._say(item, _FAILED)

    async def _run_turn_guarded(self, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        """Error boundary for failures before the status card exists."""
        try:
            await self._run_turn(inbound, tenant_id)
        except _TURN_ERRORS as exc:
            log.error("teams.turn.failed", conversation_id=inbound.conversation_id, exc_info=exc)
            capture_exception_with_scope(exc)
            with contextlib.suppress(*TEAMS_SEND_ERRORS):
                await self._say(inbound, _FAILED)

    async def _run_turn(self, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        """Turn body: admit → card → bind → marker → run → watermark. Mirrors Slack's."""
        deps = self.runtime.turn_deps
        try:
            admission = await admit(
                deps,
                tenant_id=tenant_id,
                platform="teams",
                external_user_id=inbound.user_id,
                channel_id=inbound.channel_id,
                thread_id=inbound.conversation_id,
                role=self._role(inbound),
                now=datetime.now(UTC),
            )
        except MissingTurnConfigError as err:
            log.info("teams.missing_config", missing=list(err.missing))
            missing = " or ".join(err.missing)
            await self._say(inbound, f"No {missing} configured here. Ask the operator to set one.")
            return
        except MAResolverMissError as err:
            log.warning("teams.resolver.miss", kind=err.kind, daimon_tag=err.daimon_tag)
            await self._say(inbound, _RESOLVER_MISS)
            return
        except AdmissionDenied as err:
            log.info(_DENIAL_EVENTS[err.reason], tenant_id=str(tenant_id))
            await self._say(
                inbound, _BALANCE_DEPLETED if err.reason == "balance_depleted" else _CAP_REACHED
            )
            return

        agent = admission.agent
        # Committed before the post so a lost response still leaves a record.
        async with self.runtime.sessionmaker() as session:
            intent = await create_turn_card_intent(
                session,
                tenant_id=tenant_id,
                platform="teams",
                thread_id=inbound.conversation_id,
                turn_token=uuid.uuid4(),
                channel_id=inbound.conversation_id,
            )
            await session.commit()
        cancel_key = intent.id.hex

        def new_lifecycle(cancel: asyncio.Event, adopt: str | None) -> TeamsTurnLifecycle:
            self._cancel_registry[cancel_key] = (cancel, inbound.user_id)
            return TeamsTurnLifecycle(
                sender=self._sender,
                conversation_id=inbound.conversation_id,
                service_url=inbound.service_url,
                cancel_key=cancel_key,
                agent_name=agent.name,
                model_id=agent.model.id,
                adopt_message_id=adopt,
            )

        lifecycle = new_lifecycle(asyncio.Event(), None)
        cancel = self._cancel_registry[cancel_key][0]
        # Every attempt's lifecycle; dead-session recovery appends, the last is current.
        holder: list[TeamsTurnLifecycle] = [lifecycle]
        markers: set[uuid.UUID] = set()
        try:
            # Posted before bind: session creation can take minutes.
            await lifecycle.post_initial()
            try:
                # A gate: no untracked turn runs behind a visible card.
                async with self.runtime.sessionmaker() as session:
                    recorded = await record_turn_card_message(
                        session, id=intent.id, message_id=lifecycle.message_id or ""
                    )
                    await session.commit()
                if not recorded:
                    raise DaimonError("Teams card intent could not record its message id")
                await self._bind_and_run(
                    inbound, tenant_id, admission, cancel, holder, markers, new_lifecycle
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The card exists: collapse it rather than post a second message.
                log.error(
                    "teams.turn.failed", conversation_id=inbound.conversation_id, exc_info=exc
                )
                capture_exception_with_scope(exc)
                await holder[-1].close_with_notice(_FAILED)
        finally:
            self._cancel_registry.pop(cancel_key, None)
            closed = any(each.card_closed for each in holder)
            task = asyncio.current_task()
            # A closed card shows its outcome; the sweep must never overwrite it.
            if closed or task is None or task.cancelling() == 0:
                await asyncio.shield(self._settle(intent.id, lifecycle.message_id, closed, markers))

    async def _bind_and_run(
        self,
        inbound: TeamsInbound,
        tenant_id: uuid.UUID,
        admission: Admission,
        cancel: asyncio.Event,
        holder: list[TeamsTurnLifecycle],
        markers: set[uuid.UUID],
        new_lifecycle: LifecycleFactory,
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
                thread_id=inbound.conversation_id,
                session_account_id=admission.account_id,
                reuse_existing=True,
                deadline=deadline,
            )
        except SessionPreparationFailed:
            await lifecycle.close_with_notice(render_preparation_failed(admission.agent.name))
            return
        except SessionBusyError:
            text = render_current_work_must_finish(admission.agent.name, handoff=True)
            await lifecycle.close_with_notice(text)
            return
        except SessionAgentMismatch as error:
            owner = "the previous agent"
            with contextlib.suppress(anthropic.APIStatusError):
                owner = (
                    await self.runtime.anthropic.beta.agents.retrieve(error.source_agent_id)
                ).name
            await lifecycle.close_with_notice(
                render_responder_changed_without_handoff(
                    new_responder=admission.agent.name, owner=owner, channel="this chat"
                )
            )
            return

        summary: str | None = None
        if prepared.continuity.state == "replaced" and prepared.continuity.transfer_kind:
            summary = render_replacement_summary(prepared.continuity.transfer_kind, lost=[])
            lifecycle.answer_prefix = summary
        if prepared.mapping_id is not None and lifecycle.message_id is not None:
            async with self.runtime.sessionmaker() as session:
                await mark_turn_active(
                    session,
                    id=prepared.mapping_id,
                    active_turn_message_id=lifecycle.message_id,
                    active_turn_channel_id=inbound.conversation_id,
                    now=datetime.now(UTC),
                )
                await session.commit()
            markers.add(prepared.mapping_id)

        def recovery_lifecycle(fresh_cancel: asyncio.Event) -> TurnLifecycle:
            # Keep rendering into the card the person is already watching.
            adopted = new_lifecycle(fresh_cancel, lifecycle.message_id)
            adopted.answer_prefix = lifecycle.answer_prefix
            holder.append(adopted)
            return adopted

        config = admission.config
        async with turn_origin(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            platform="teams",
            parent_channel_id=inbound.channel_id,
            thread_id=inbound.conversation_id,
            responder_ma_agent_id=str(admission.agent.id),
            responder_name=config.agent_name or admission.agent.name,
            role=self._role(inbound),
            configuration_target_ma_agent_id=config.configuration_target_ma_agent_id,
            configuration_target_name=config.configuration_target_name,
            is_setup=config.thread_binding_kind == "setup",
        ) as origin:
            # The controls are server facts, rendered apart from the person's words.
            message = (
                render_turn_origin(
                    origin,
                    responder_handle=f"@{inbound.bot_name}" if inbound.bot_name else None,
                    session_state=session_state(prepared.continuity),
                )
                + "\n"
                + inbound.text
            )

            async def reseed() -> str:
                return message

            outcome = await run_prepared_turn(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform="teams",
                thread_id=inbound.conversation_id,
                external_user_id=inbound.user_id,
                user_message=message,
                lifecycle=lifecycle,
                cancel=cancel,
                reseed_user_message=reseed,
                recovery_lifecycle=recovery_lifecycle,
                deadline=deadline,
            )
        if outcome.mapping_id is not None:
            markers.add(outcome.mapping_id)
        final = holder[-1]
        if outcome.continuity.state == "replaced_after_loss":
            kind: Literal["transcript", "history"] = (
                "transcript" if outcome.continuity.transfer_kind == "transcript" else "history"
            )
            await self._say(inbound, render_unexpected_loss(kind))
        if summary is not None and not final.answer_prefix_applied:
            await self._say(inbound, summary)
        if outcome.mapping_id is not None and final.final_message_id is not None:
            async with self.runtime.sessionmaker() as session:
                await update_watermark(
                    session, id=outcome.mapping_id, watermark_message_id=final.final_message_id
                )
                await session.commit()
        if prepared.continuity.pending:
            await self._say(
                inbound, render_current_work_must_finish(admission.agent.name, handoff=False)
            )

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
            async with self.runtime.sessionmaker() as session:
                if closed:
                    await retire_turn_card_intent(
                        session, id=intent_id, expected_message_id=message_id
                    )
                for marker_id in markers:
                    await clear_active_turn(session, id=marker_id)
                await session.commit()
        except SQLAlchemyError:
            log.exception("teams.turn.settle_failed", intent_id=str(intent_id))

    async def handle_cancel(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        """Cancel button: only the turn's author may stop it."""
        action = ctx.activity.value.action
        entry = self._cancel_registry.get(str(action.data.get("turn") or ""))
        if entry is None:
            return AdaptiveCardActionMessageResponse(value=_CANCEL_TURN_ENDED)
        cancel, author = entry
        if canonical_uuid(ctx.activity.from_.aad_object_id) != author:
            return AdaptiveCardActionMessageResponse(value=_CANCEL_NOT_AUTHOR)
        cancel.set()
        return AdaptiveCardActionMessageResponse(value=_CANCELLING)

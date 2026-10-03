"""Teams turn orchestration: queueing, the turn body, commands and Cancel.

Mirrors `SlackApp`. One turn per conversation thread at a time; messages that
arrive meanwhile queue silently (Teams bots cannot react) and run after it as
one turn per author. A per-tenant cap sheds load, and no turn starts until the
boot sweep has retired the previous process's turns. A wake poller opens chats
with due handoffs and timers. Unmentioned thread replies go to organic thread
participation (`participation.py`), whose turns run here, silently on failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

import anthropic
import daimon.core.turn.bookkeeping as turn_bookkeeping
import structlog
from daimon.adapters.teams.attachments import BotToken, prepare_attachments
from daimon.adapters.teams.boot_sweep import retire_orphaned_turns
from daimon.adapters.teams.budget_notice import with_budget_notifier
from daimon.adapters.teams.card import enable_files_card
from daimon.adapters.teams.card_actions import stored_external, toast
from daimon.adapters.teams.channel_admin_groups import owned_team_ids
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.commands import (
    ANSWERED_IN_CHAT,
    CHANNEL_POINTER,
    NEW_IN_CHANNEL,
    CommandContext,
    CommandHandler,
    parse_command,
)
from daimon.adapters.teams.context import (
    NOTHING_ATTACHED,
    HistoryBlock,
    newest_message_id,
    render_user_message,
)
from daimon.adapters.teams.credential_requests import TeamsCredentialRequests
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.identity import (
    DENIED,
    Refusal,
    TeamsInbound,
    canonical_uuid,
    live_tenant_id,
    parse_inbound,
)
from daimon.adapters.teams.installations import TeamInstalls
from daimon.adapters.teams.lifecycle import (
    SEND_TIMEOUT_S,
    TEAMS_SEND_ERRORS,
    TeamsSender,
    TeamsTurnLifecycle,
    TimedSender,
)
from daimon.adapters.teams.output_delivery import TeamsOutputDelivery
from daimon.adapters.teams.participation import TeamsParticipation
from daimon.adapters.teams.provisioning import provision_configured_tenant
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import route_to_setup
from daimon.adapters.teams.site_grant import (
    STATE_TTL_S,
    authorize_url,
    redirect_uri,
    sign_state,
)
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.adapters.teams.tool_confirmation import TeamsConfirmationCards
from daimon.core.continuity.continuation import check_wake_responder, load_asking_agent_id
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
from daimon.core.participation_gates import ParticipationGates
from daimon.core.permissions import confidential_channel_of
from daimon.core.routine_delivery import RoutinePoster, run_delivery_poller
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import Role, TaskContinuationRow, TurnCardIntentRow
from daimon.core.stores.teams_installations import list_teams_installations
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
from daimon.core.thread_participation import ParticipationMode
from daimon.core.turn import turn_deadline
from daimon.core.turn.admission import (
    Admission,
    AdmissionDenied,
    ExternalFinding,
    MissingTurnConfigError,
    admit,
)
from daimon.core.turn.errors import (
    AdmissionDenialReason,
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.gating import should_admit_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.outcomes import observe_turn, record_refusal
from daimon.core.turn.prepare import ContinuityOutcome, bind_session
from daimon.core.turn.protection import protection_state
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import ToolUseBlock
from daimon.core.turn.thread_queue import (
    ThreadQueue,
    claim_dispatch,
    dispatch_and_drain,
    group_by_author,
    release_thread,
)
from daimon.core.turn_keys import list_mounted_key_names, render_keys_element
from daimon.core.turn_origin import (
    HandoffNotice,
    build_handoff_notice,
    render_turn_origin,
    turn_origin,
)
from microsoft_teams.api import (
    Account,
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
TEAMS_REFUSAL_NOUNS = RefusalNouns(
    scope="organisation", admin="an admin", billing="`billing` in a 1:1 chat with me"
)
# The log event each refusal is recorded under.
_DENIALS: dict[AdmissionDenialReason, str] = {
    "balance_depleted": "turn.skipped.over_balance",
    "cap_exceeded": "turn.skipped.over_cap",
    "invoker_not_allowed": "turn.skipped.invoker_not_allowed",
    "agent_pinned_elsewhere": "turn.skipped.agent_pinned_elsewhere",
    "channel_budget_exceeded": "turn.skipped.channel_budget_exceeded",
    "channel_protected": "turn.skipped.channel_protected",
    "channel_isolated": "turn.skipped.channel_isolated",
    "external_participant": "turn.skipped.external_participant",
}

_NO_CONTEXT = (
    "I couldn't read this thread, so there's nothing for me to go on. "
    "Write your question after the mention. If this keeps happening, ask a team owner "
    "to check the app's permission to read channel messages."
)


def _bare_mention(inbound: TeamsInbound) -> bool:
    """A channel mention with no words or files: it asks about the thread."""
    return inbound.kind == "channel" and not inbound.text.strip() and not inbound.files


LifecycleFactory = Callable[[asyncio.Event, str | None], TeamsTurnLifecycle]
# Builds a continuation turn's handoff notice from what the bind carried across.
HandoffFactory = Callable[[ContinuityOutcome], HandoffNotice]


def _compose_queued(queued: list[TeamsInbound]) -> list[TeamsInbound]:
    """One turn per author, replying where that author's last message came from."""
    return [
        dataclasses.replace(
            items[-1],
            text="\n\n".join(item.text for item in items),
            files=tuple(file for item in items for file in item.files),
            composed_ids=tuple(item.activity_id for item in items[:-1]),
        )
        for items in group_by_author(queued, lambda item: item.user_id)
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
    log.info(_DENIALS[err.reason], tenant_id=str(tenant_id))
    # A protected channel hears nothing, a refusal included.
    if err.reason == "channel_protected":
        return None
    return admission_refusal_text(err.reason, TEAMS_REFUSAL_NOUNS)


class TeamsApp:
    """Handlers the SDK app routes to, plus the turn state they share."""

    def __init__(
        self,
        *,
        runtime: TeamsRuntime,
        sender: TeamsSender,
        commands: Mapping[str, CommandHandler],
        bot_token: BotToken,
        reader: ThreadReader | None = None,
        channel_files: ChannelFiles | None = None,
        installs: TeamInstalls | None = None,
        direct: DirectChats | None = None,
        routine_poster: RoutinePoster | None = None,
    ) -> None:
        teams = runtime.settings.teams
        if teams is None:
            raise ValueError("TeamsApp requires Teams settings")
        self.runtime = runtime = with_budget_notifier(runtime, direct)
        self._teams = teams
        # Known before any read, so `_may_post` can gate every post.
        self._tenant_id = derive_tenant_uuid(platform="teams", workspace_id=teams.tenant_id)
        self._sender = TimedSender(sender)
        self._commands = commands
        self._bot_token = bot_token
        # Channel history and media through Graph; None replays nothing.
        self._reader = reader
        # Channel files through SharePoint; None leaves channels without files.
        self._channel_files = channel_files
        # Records each team for the MCP server's channel reads; None records nothing.
        self._installs = installs
        # Opens 1:1 chats for commands sent in a channel; None points the sender there.
        self._direct = direct
        # Posts routine results to their channels; None leaves them pending.
        self._routine_poster = routine_poster
        self.outputs = TeamsOutputDelivery(
            runtime=runtime, sender=self._sender, spawn=self.spawn, files=channel_files
        )
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
        # Group id -> when its enable-files sign-in was offered (monotonic).
        self._files_offered: dict[str, float] = {}
        self._recovery: asyncio.Task[None] | None = None
        self._wake_poller: asyncio.Task[None] | None = None
        self._delivery_poller: asyncio.Task[None] | None = None
        # When each busy conversation last got a message: a newer one supersedes
        # queued continuation work (Bot Framework cannot list a chat's history).
        self._last_message_at: dict[str, datetime] = {}
        # Dispatches that found their chat busy, by thread key: the service URL to
        # use and whether the cap holds them back (only wakes; a saved input wins).
        self._deferred_dispatch: dict[str, tuple[uuid.UUID, str | None, bool]] = {}
        # Built on first use: the classifier names the bot as the activity's recipient does.
        self._participation: TeamsParticipation | None = None
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
                if self._routine_poster is not None:
                    deliveries = run_delivery_poller(
                        self.runtime.sessionmaker,
                        platform="teams",
                        post=self._routine_poster,
                        should_stop=lambda: self.draining,
                    )
                    self._delivery_poller = asyncio.create_task(
                        deliveries, name="teams.routine-delivery"
                    )
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
                    find_card=self._find_card,
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
        if self._participation is not None:
            self._participation.cancel_all()
        for poller in (self._wake_poller, self._delivery_poller):
            if poller is not None:
                # Wake dispatches drain with the turns; due rows wait for the next boot, and
                # a delivery cut short settles as interrupted, never posted twice.
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
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
        # Someone from another organisation is never an admin, whatever ids are configured.
        if inbound.is_external:
            return Role.USER
        return Role.ADMIN if inbound.user_id in self._teams.admin_user_ids else Role.USER

    async def _classified(self, inbound: TeamsInbound) -> TeamsInbound:
        """`inbound` with what its member lists say about the sender (`externals`)."""
        externals = self.runtime.externals
        if externals is None:
            return inbound
        membership = await externals.classify(
            foreign_tenant=inbound.home_tenant_id if inbound.is_external_known else None,
            kind=inbound.kind,
            conversation_id=inbound.channel_id,
            user_id=inbound.user_id,
            team_id=inbound.team_id,
            team_group_id=inbound.team_group_id,
            channel_type=inbound.channel_type,
        )
        return dataclasses.replace(
            inbound,
            is_external=membership.is_external,
            is_external_known=membership.is_known,
            home_tenant_id=membership.home_tenant_id,
        )

    async def _stored_external(self, inbound: TeamsInbound, tenant_id: uuid.UUID) -> TeamsInbound:
        """`inbound` held as external when nothing placed them but their account says so."""
        if inbound.is_external or inbound.is_external_known:
            return inbound
        stored = await stored_external(self.runtime.sessionmaker, tenant_id, inbound.user_id)
        return dataclasses.replace(inbound, is_external=True) if stored else inbound

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
        if self._installs is not None:
            # Inline: a no-op once the team is recorded in this process.
            await self._installs.observe(activity)
        parsed = parse_inbound(
            activity,
            configured_tenant=self._teams.tenant_id,
            service_url=ctx.conversation_ref.service_url,
        )
        if isinstance(parsed, Refusal):
            # Type and reason only, never the text: these drops are otherwise invisible.
            conversation_type = activity.conversation.conversation_type
            if parsed.text is None:
                # Debug: with channel-message consent Teams delivers every channel post.
                log.debug(
                    "teams.message.ignored",
                    conversation_type=conversation_type,
                    reason="not_mentioned",
                )
                return
            conversation_id = activity.conversation.id
            if await self._may_post(conversation_id.split(";", 1)[0], conversation_id):
                try:
                    await asyncio.wait_for(ctx.reply(parsed.text), SEND_TIMEOUT_S)
                except TEAMS_SEND_ERRORS as exc:
                    log.warning(
                        "teams.refusal.send_failed",
                        conversation_type=conversation_type,
                        reason=type(exc).__name__,
                    )
            return
        if parsed.unprompted:
            if self.runtime.settings.thread_participation.mode is ParticipationMode.DISABLED:
                log.debug("teams.message.ignored", conversation_type="channel", reason="disabled")
                return
            self.spawn(self._observe(parsed), name="teams.participation")
            return
        self.spawn(self._handle(parsed), name="teams.turn")

    def start_wizard_turn(self, inbound: TeamsInbound) -> None:
        """Run a submitted form's turn on the ordinary path, as a message would."""
        self.spawn(self._handle(inbound), name="teams.wizard-turn")

    def _participation_for(self, inbound: TeamsInbound) -> TeamsParticipation:
        if self._participation is None:
            settings = self.runtime.settings
            gates = ParticipationGates(
                platform="teams",
                settings=settings.thread_participation,
                sessionmaker=self.runtime.sessionmaker,
                anthropic=self.runtime.anthropic,
                bot_display_name=inbound.bot_name or "daimon",
                billing_config=self.runtime.billing_config,
                markup=settings.billing.markup,
            )
            self._participation = TeamsParticipation(
                gates=gates,
                settings=settings.thread_participation,
                reader=self._reader,
                spawn=self.spawn,
                fire=self._participate,
                is_busy=lambda key: self.draining or key in self._processing,
            )
        return self._participation

    async def _observe(self, inbound: TeamsInbound) -> None:
        """An unmentioned thread reply: one cascade read, then maybe a batch."""

        async def admitted() -> TeamsInbound | None:
            # Only a followed thread pays these reads; a protected one is never judged.
            if not await self._may_post(inbound.channel_id, inbound.thread_id):
                return None
            live = await live_tenant_id(self.runtime.sessionmaker, inbound.entra_tenant_id)
            if live is None:
                return None
            placed = await self._stored_external(await self._classified(inbound), live)
            if placed.is_external and not await self._isolated(live, placed):
                return None  # admission would refuse them: judge nothing they wrote
            return placed

        await self._participation_for(inbound).observe(inbound, self._tenant_id, admitted=admitted)

    async def _isolated(self, tenant_id: uuid.UUID, inbound: TeamsInbound) -> bool:
        """Whether `inbound` lies in an isolated channel; an unreadable policy says no."""
        try:
            async with self.runtime.sessionmaker() as session:
                policy = await load_access_policy(session, tenant_id=tenant_id)
        except AccessPolicyUnreadable:
            return False
        return confidential_channel_of(policy, inbound.thread_id, inbound.channel_id) is not None

    async def _participate(self, trigger: TeamsInbound, tenant_id: uuid.UUID) -> None:
        """The classifier said reply: run one turn as the burst's author, silently shed.

        Admission, billing and the turn itself are a mention's; only every
        notice is withheld (`_say`, the unprompted lifecycle).
        """
        if self._recovery is not None:
            await asyncio.shield(self._recovery)
        key = trigger.conversation_id
        cap = await self._turn_cap(tenant_id)
        if self.draining or key in self._processing:
            return  # a mention landed while the classifier was deciding
        count = self._inflight.get(tenant_id, 0)
        if not should_admit_turn(current_in_flight=count, cap=cap):
            record_refusal(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="teams",
                channel_id=trigger.channel_id,
                thread_id=trigger.thread_id,
            )
            log.info(
                "turn.skipped.concurrency_shed", tenant_id=str(tenant_id), in_flight=count, cap=cap
            )
            return
        self._last_message_at[key] = datetime.now(UTC)
        async with self._holding(key, tenant_id):
            # Written on admission, not on answer: a turn that ends silent still spent.
            if self._participation is not None:
                await self._participation.record(trigger, tenant_id)
            await self._run_turns(key, tenant_id, [trigger])

    async def _say(self, inbound: TeamsInbound, text: str) -> None:
        if inbound.unprompted:
            # Nobody asked: a refusal or notice per quiet burst would spam the thread.
            log.info("teams.notice.withheld", reason="unprompted")
            return
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
        inbound = await self._classified(inbound)
        if inbound.is_external:
            # Type and tenant only: who sent it stays out of the log.
            log.info(
                "teams.message.external",
                channel_type=inbound.channel_type,
                home_tenant_id=inbound.home_tenant_id,
            )
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
        inbound = await self._stored_external(inbound, tenant_id)
        # Someone from another organisation gets no command: the agent hears their words,
        # and a command's 1:1 chat cannot be opened across organisations.
        command = None if inbound.is_external else parse_command(inbound.text, self._commands)
        if command is not None:
            name, args = command
            asked_in = None
            if inbound.kind != "dm" and name == "new":
                await self._say(inbound, NEW_IN_CHANNEL)
                return
            if inbound.kind != "dm":
                # Teams has no message only its sender sees: answer in their 1:1 chat.
                chat = await self._direct_chat(inbound)
                await self._say(
                    inbound, (ANSWERED_IN_CHAT if chat else CHANNEL_POINTER).format(name=name)
                )
                if chat is None:
                    return
                asked_in, inbound = inbound, chat
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
                    asked_in=asked_in,
                )
            )
            return
        await self._orchestrate(inbound, tenant_id)

    async def _direct_chat(self, inbound: TeamsInbound) -> TeamsInbound | None:
        """`inbound` moved to its sender's 1:1 chat; None when Teams will not open one."""
        if self._direct is None:
            return None
        try:
            member = await self._direct.member(inbound.channel_id, inbound.user_id)
            chat = await self._direct.open_chat(member) if member else None
        except TEAMS_SEND_ERRORS as exc:
            log.info("teams.command.no_direct_chat", reason=type(exc).__name__)
            return None
        if chat is None:
            return None
        return dataclasses.replace(
            inbound,
            kind="dm",
            conversation_id=chat,
            channel_id=chat,
            team_id=None,
            team_group_id=None,
            channel_name=None,
            channel_type=None,
            team_name=None,
        )

    async def _find_card(self, intent: TurnCardIntentRow) -> str | None:
        """A channel card whose post returned no id, found by its Cancel key."""
        if self._reader is None:
            return None
        async with self.runtime.sessionmaker() as session:
            teams = await list_teams_installations(session, tenant_id=intent.tenant_id)
        return await self._reader.find_card(
            intent.channel_id or intent.thread_id,
            intent.id.hex,
            group_ids=[team.group_id for team in teams],
        )

    async def _turn_cap(self, tenant_id: uuid.UUID) -> int:
        default = self._teams.max_concurrent_turns_per_tenant
        return await get_turn_cap(self.runtime.sessionmaker, tenant_id=tenant_id, default=default)

    async def _orchestrate(self, inbound: TeamsInbound, tenant_id: uuid.UUID) -> None:
        key = inbound.conversation_id
        # Read first: no await may split the queue check from the claim below.
        cap = await self._turn_cap(tenant_id)
        if key in self._processing:
            self._last_message_at[key] = datetime.now(UTC)
            self._thread_queue.enqueue(key, inbound)
            self._supersede_batch(inbound)
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
        self._supersede_batch(inbound)
        async with self._holding(key, tenant_id):
            await self._run_turns(key, tenant_id, [inbound])

    def _supersede_batch(self, inbound: TeamsInbound) -> None:
        """A mention's turn owns the thread now: drop its waiting batch, whose replies it replays.

        Not on arrival: a mention refused before here (protected, command, shed) drops nothing.
        """
        if inbound.kind == "channel" and self._participation is not None:
            self._participation.cancel(inbound.conversation_id)

    @contextlib.asynccontextmanager
    async def _holding(self, key: str, tenant_id: uuid.UUID) -> AsyncIterator[None]:
        """Hold a chat and a tenant turn slot; on exit, answer what could not run."""
        self._inflight[tenant_id] = self._inflight.get(tenant_id, 0) + 1
        self._thread_queue.claim(key)
        try:
            yield
        finally:
            self._release(key)
            if (remaining := self._inflight.pop(tenant_id, 1) - 1) > 0:
                self._inflight[tenant_id] = remaining
            for item in self._pending.pop(key, []):
                with contextlib.suppress(*TEAMS_SEND_ERRORS):
                    await self._say(item, _FAILED)

    @property
    def _thread_queue(self) -> ThreadQueue[str, TeamsInbound]:
        return ThreadQueue(self._processing, self._pending)

    async def _run_turns(self, key: str, tenant_id: uuid.UUID, turns: list[TeamsInbound]) -> None:
        async def run(queued: TeamsInbound) -> None:
            turn = await route_to_setup(self.runtime.sessionmaker, queued, tenant_id)
            await self._run_turn_guarded(turn, tenant_id)
            await self._dispatch_continuations(turn.thread_id, tenant_id, turn.service_url)

        await self._thread_queue.drain(key, initial=turns, compose=_compose_queued, run=run)

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
                platform_role_ids=sorted(
                    await owned_team_ids(self.runtime, tenant_id=tenant_id, user_id=inbound.user_id)
                )
                # An admin needs no grant; someone from another organisation gets none.
                if self._role(inbound) is not Role.ADMIN and not inbound.is_external
                else (),
                now=datetime.now(UTC),
                is_dm=inbound.kind == "dm",
                external=ExternalFinding(inbound.is_external, inbound.is_external_known),
            )
        except (MissingTurnConfigError, MAResolverMissError, AdmissionDenied) as err:
            refusal = _admission_refusal(err, tenant_id)
            if refusal is not None and continuation is None:  # queued work settles silently
                await self._say(inbound, refusal)
            if reraise:
                raise
            return
        if admission.is_external and not inbound.is_external:
            inbound = dataclasses.replace(inbound, is_external=True)
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

        # Committed before the post so a lost response still leaves a record. An
        # unprompted turn posts no card up front, so there is none to sweep.
        intent_id: uuid.UUID | None = None
        if not inbound.unprompted:
            async with self.runtime.sessionmaker.begin() as session:
                intent = await create_turn_card_intent(
                    session,
                    tenant_id=tenant_id,
                    platform="teams",
                    thread_id=inbound.conversation_id,
                    turn_token=uuid.uuid4(),
                    channel_id=inbound.conversation_id,
                )
            intent_id = intent.id
        cancel_key = (intent_id or uuid.uuid4()).hex
        # Every attempt's lifecycle; dead-session recovery adds one, the last is current.
        holder: list[TeamsTurnLifecycle] = []
        # A ping mentions the asker in a channel; the 1:1 chat notifies them anyway.
        requester = None
        if inbound.kind == "channel":
            requester = Account(id=inbound.user_id, name=inbound.user_name or "you")

        def new_lifecycle(cancel: asyncio.Event, adopt: str | None) -> TeamsTurnLifecycle:
            self._cancel_registry[cancel_key] = (cancel, inbound.user_id)
            attempt = TeamsTurnLifecycle(
                sender=self._sender,
                conversation_id=inbound.conversation_id,
                service_url=inbound.service_url,
                cancel_key=cancel_key,
                adopt_message_id=adopt,
                tenant_id=tenant_id,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                unprompted=inbound.unprompted,
                completion_ping=self.runtime.settings.completion_pings.get(tenant_id) is True,
                requester=requester,
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
                if intent_id is not None:
                    async with self.runtime.sessionmaker.begin() as session:
                        recorded = await record_turn_card_message(
                            session, id=intent_id, message_id=lifecycle.message_id or ""
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
                await asyncio.shield(self._settle(intent_id, lifecycle.message_id, closed, markers))

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
        bare = _bare_mention(inbound)
        if bare and self._reader is None:
            await lifecycle.close_with_notice(_NO_CONTEXT)
            return
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

        # The messages this turn answers and its status card are not history.
        skip = frozenset(filter(None, (*inbound.message_ids, lifecycle.message_id)))
        watermark = prepared.watermark if prepared.reused else None
        history = await self._history(inbound, watermark=watermark, skip_ids=skip)
        read = [history]
        # An unprompted turn without its thread has nothing to answer: it stays silent.
        if (bare or inbound.unprompted) and (history is None or history.unavailable):
            await lifecycle.close_with_notice(_NO_CONTEXT)
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

        reader = self._reader
        # A channel activity carries only the text: its media are on Graph's copy.
        media = await reader.read_media(inbound) if reader else None
        attachments = await prepare_attachments(
            self.runtime.http_client,
            inbound.files,
            bot_token=self._bot_token,
            service_url=inbound.service_url,
            channel=inbound.kind == "channel",
            channel_media=media,
            graph_token=reader.token if reader else None,
            history_images=history.image_urls if history else (),
        )
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
            is_external=admission.is_external,
        ) as origin:
            notice = handoff(prepared.continuity) if handoff is not None else None
            quiet = notice is not None or prepared.continuity.state == "continued"
            controls = render_turn_origin(
                origin,
                responder_handle=f"@{inbound.bot_name}" if inbound.bot_name else None,
                session_state=None if quiet else prepared.continuity.session_state(),
                handoff=notice,
            )
            render = functools.partial(
                render_user_message,
                controls,
                inbound,
                is_admin=self._role(inbound) is Role.ADMIN,
                keys=render_keys_element(await self._key_names(tenant_id, inbound, admission)),
                prefix=attachments.prefix,
                channel_files=await self._files_reachable(inbound),
            )
            message = render(history=history)

            async def reseed() -> str:
                # A recreated session has seen nothing: replay the whole thread.
                images = (history.attached if history else NOTHING_ATTACHED).images
                read.append(
                    await self._history(inbound, watermark=None, skip_ids=skip, images=images)
                )
                return render(history=read[-1])

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
            append = holder[-1].append_to_answer
            sweep = self.outputs.sweep(inbound, outcome.ma_session_id, append=append)
            self.spawn(sweep, name="teams.output-sweep")
        final = holder[-1]
        if outcome.continuity.state == "replaced_after_loss":
            kind: Literal["transcript", "history"] = (
                "transcript" if outcome.continuity.transfer_kind == "transcript" else "history"
            )
            # Above the answer or not at all: a message of its own lands below it.
            if not await final.prepend_revealed_answer(render_unexpected_loss(kind)):
                log.info("teams.turn.loss_notice_dropped")
        if summary is not None and not final.answer_prefix_applied:
            log.info("teams.turn.summary_dropped")
        if outcome.mapping_id is not None and final.final_message_id is not None:
            mark = final.final_message_id
            if inbound.kind == "channel":
                # The newest message read, not the answer: replies posted while the
                # turn ran are older than the answer, and the next delta needs them.
                newest = (h.newest_id for h in read if h is not None)
                mark = newest_message_id([*inbound.message_ids, *newest]) or mark
            async with self.runtime.sessionmaker.begin() as session:
                await update_watermark(session, id=outcome.mapping_id, watermark_message_id=mark)
        if prepared.continuity.pending:
            # The change was saved where it was made; the next turn applies it.
            log.info("teams.turn.change_pending", reasons=prepared.continuity.pending)
        if media is not None and any(f.refused for f in media.files):
            await self._offer_enable_files(inbound, media.group_id)

    async def _offer_enable_files(self, inbound: TeamsInbound, group_id: str | None) -> None:
        """An admin whose channel files were refused gets the sign-in that grants them.

        A card, not a status line: never unprompted, and again only once the last
        offer's sign-in has expired.
        """
        teams = self._teams
        now = time.monotonic()
        if (
            group_id is None
            or teams.public_url is None
            or inbound.unprompted
            or self._role(inbound) is not Role.ADMIN
            or now - self._files_offered.get(group_id, -STATE_TTL_S) < STATE_TTL_S
        ):
            return
        self._files_offered[group_id] = now
        state = sign_state(group_id, secret=teams.client_secret.get_secret_value(), now=time.time())
        url = authorize_url(
            tenant_id=teams.tenant_id,
            client_id=teams.client_id,
            redirect_uri=redirect_uri(teams.public_url),
            state=state,
        )
        try:
            await self._sender.send(
                inbound.conversation_id, enable_files_card(url), service_url=inbound.service_url
            )
        except TEAMS_SEND_ERRORS:
            self._files_offered.pop(group_id, None)
            log.warning("teams.enable_files.send_failed", exc_info=True)

    async def _files_reachable(self, inbound: TeamsInbound) -> bool | None:
        if inbound.kind != "channel":
            return None
        return self._channel_files is not None and await self._channel_files.is_available(inbound)

    async def _history(
        self,
        inbound: TeamsInbound,
        *,
        watermark: str | None,
        skip_ids: frozenset[str],
        images: Mapping[str, int] | None = None,
    ) -> HistoryBlock | None:
        if self._reader is None:
            return None
        return await self._reader.read(
            inbound, watermark=watermark, skip_ids=skip_ids, images=images
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
        def resume(thread_id: str, request: tuple[uuid.UUID, str | None, bool]) -> None:
            tenant_id, service_url, capped = request
            self.spawn(
                self.dispatch_after_input(tenant_id, thread_id, service_url, capped=capped),
                name="teams.resume",
            )

        release_thread(
            self._processing,
            key,
            self._deferred_dispatch,
            dispatch_keys=lambda: [t for t in self._deferred_dispatch if conversation_of(t) == key],
            draining=self.draining,
            resume=resume,
            on_release=lambda: self._last_message_at.pop(key, None),
        )

    async def dispatch_after_input(
        self, tenant_id: uuid.UUID, thread_id: str, service_url: str | None, *, capped: bool = False
    ) -> None:
        if self.draining:
            return
        if self._recovery is not None:
            await asyncio.shield(self._recovery)
        conversation_id = conversation_of(thread_id)
        cap = await self._turn_cap(tenant_id) if capped else 0

        def merge(
            previous: tuple[uuid.UUID, str | None, bool] | None,
            request: tuple[uuid.UUID, str | None, bool],
        ) -> tuple[uuid.UUID, str | None, bool]:
            _, previous_url, previous_capped = previous or (tenant_id, None, True)
            return tenant_id, request[1] or previous_url, request[2] and previous_capped

        if not claim_dispatch(
            self._processing,
            conversation_id,
            self._deferred_dispatch,
            thread_id,
            (tenant_id, service_url, capped),
            merge=merge,
            claim_slot=False,
            admit=lambda: (
                not capped
                or should_admit_turn(current_in_flight=self._inflight.get(tenant_id, 0), cap=cap)
            ),
        ):
            return
        async with self._holding(conversation_id, tenant_id):

            async def drain() -> None:
                queued = _compose_queued(self._pending.pop(conversation_id, []))
                await self._run_turns(conversation_id, tenant_id, queued)

            await dispatch_and_drain(
                lambda: self._dispatch_continuations(thread_id, tenant_id, service_url),
                drain,
            )

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
        """The agent that asked for a private input, while the requester's live session is
        still with it (`load_asking_agent_id`)."""
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
            return await load_asking_agent_id(
                session, row, live_ma_agent_id=live.ma_agent_id if live is not None else None
            )

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
        inbound = await self._stored_external(await self._classified(inbound), tenant_id)
        await self._run_turn(inbound, tenant_id, handoff=handoff, reraise=True, continuation=row)

    async def _settle(
        self,
        intent_id: uuid.UUID | None,
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
                if closed and intent_id is not None:
                    await retire_turn_card_intent(
                        session, id=intent_id, expected_message_id=message_id
                    )
                await turn_bookkeeping.clear_turn_markers(session, markers, clear=clear_active_turn)
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

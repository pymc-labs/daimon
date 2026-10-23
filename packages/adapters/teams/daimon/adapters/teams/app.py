"""Teams turn orchestration: queueing, the turn body, commands and Cancel.

Mirrors `SlackApp`. One turn per conversation thread at a time; messages that
arrive meanwhile queue, marked ⌛ (`reactions`), and run after it as one turn
per author. A per-tenant cap sheds load, and no turn starts until the
boot sweep has retired the previous process's turns. A wake poller opens chats
with due handoffs and timers. Unmentioned thread replies go to organic thread
participation (`participation.py`), whose turns run here, silently on failure.
In private and shared channels, where Teams delivers only @mentions, the
other messages arrive from Graph (`channel_subscriptions.py`) and are routed
the same way (`handle_graph_message`). After a boot, the messages posted while
the adapter was restarting are read back from Graph the same way
(`restart_catch_up.py`). Every message claims one admission row
(`admissions.py`), whichever source brings it first.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple

import anthropic
import daimon.core.turn.bookkeeping as turn_bookkeeping
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.teams.activity_feed import ActivityFeed
from daimon.adapters.teams.admissions import (
    HOLDING,
    NOT_HELD,
    RECLAIM_INTERVAL_S,
    TeamsAdmissions,
    too_old_to_replay,
)
from daimon.adapters.teams.attachments import BotToken, prepare_attachments
from daimon.adapters.teams.boot_sweep import retire_orphaned_turns
from daimon.adapters.teams.budget_notice import with_budget_notifier
from daimon.adapters.teams.card import CANCELLED_NOTICE
from daimon.adapters.teams.card_actions import replace_card, toast
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.channel_subscriptions import ChannelSubscriptions, SeenChannel
from daimon.adapters.teams.commands import CommandHandler
from daimon.adapters.teams.context import (
    NOTHING_ATTACHED,
    HistoryBlock,
    newest_message_id,
    render_user_message,
)
from daimon.adapters.teams.credential_requests import TeamsCredentialRequests
from daimon.adapters.teams.deployment_admins import (
    admin_team_owners,
    boot_owner_ids,
    deployment_role,
    inbound_is_admin,
    inbound_role,
    with_admin_team_if_asked,
)
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.identity import (
    DENIED,
    GraphSource,
    Refusal,
    TeamsInbound,
    canonical_uuid,
    kind_of,
    live_tenant_id,
    parse_graph_message,
    parse_inbound,
    strip_typed_name,
)
from daimon.adapters.teams.installations import TeamInstalls
from daimon.adapters.teams.lifecycle import (
    SEND_TIMEOUT_S,
    TEAMS_SEND_ERRORS,
    TeamsSender,
    TeamsTurnLifecycle,
    TimedSender,
    retry_hint,
    send_error_details,
)
from daimon.adapters.teams.names import remember_inbound
from daimon.adapters.teams.output_delivery import TeamsOutputDelivery
from daimon.adapters.teams.participation import TeamsParticipation
from daimon.adapters.teams.private_replies import SENDER_ONLY, PrivateReplies, reply_privately
from daimon.adapters.teams.provider_actions import TeamsProviderActions
from daimon.adapters.teams.provisioning import provision_configured_tenant
from daimon.adapters.teams.reactions import React, TeamsReactions
from daimon.adapters.teams.restart_catch_up import RestartCatchUp
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import route_to_setup
from daimon.adapters.teams.thread_handoff import hand_over_button
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.adapters.teams.tool_confirmation import TeamsConfirmationCards
from daimon.adapters.teams.turn_hooks import turn_hooks
from daimon.core.agent_identity import identity_enabled_for, is_builtin_agent
from daimon.core.anthropic_guard import guard_selfhosted_channel
from daimon.core.authz import Action, authorize, build_subject, build_turn_place
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
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.observability import capture_exception_with_scope
from daimon.core.participation_gates import ParticipationGates
from daimon.core.routine_delivery import RoutinePoster, run_delivery_poller
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import (
    Role,
    TaskContinuationRow,
    TeamsChannelSubscriptionRow,
    TurnCardIntentRow,
)
from daimon.core.stores.teams_installations import list_teams_installations
from daimon.core.stores.tenants import get_plain_text_mentions, get_tenant, get_turn_cap
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
from daimon.core.teams_graph import GraphMessage
from daimon.core.teams_threads import conversation_of
from daimon.core.thread_participation import ParticipationMode
from daimon.core.turn import turn_deadline
from daimon.core.turn.admission import (
    Admission,
    AdmissionDenied,
    MissingTurnConfigError,
    admit,
)
from daimon.core.turn.errors import (
    AdmissionDenialReason,
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
    TurnNotStarted,
)
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.outcomes import observe_turn, record_refusal
from daimon.core.turn.prepare import ContinuityOutcome, bind_session
from daimon.core.turn.protection import ProtectionState, protection_state
from daimon.core.turn.run import RunOutcome, run_prepared_turn
from daimon.core.turn.slots import (
    QUEUE_TIMED_OUT_TEXT,
    holding,
    release_turn_slot,
    wait_for_slot,
)
from daimon.core.turn.state import ToolUseBlock
from daimon.core.turn.thread_queue import (
    ThreadQueue,
    claim_dispatch,
    dispatch_and_drain,
    group_by_author,
    release_thread,
)
from daimon.core.turn_hold import RequesterOrg
from daimon.core.turn_keys import list_mounted_key_names, render_keys_element
from daimon.core.turn_origin import (
    HandoffNotice,
    build_handoff_notice,
    render_turn_origin,
    turn_origin,
)
from daimon.core.turn_queue import TurnQueue, TurnTicket
from microsoft_teams.api import (
    Account,
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivity,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import ExecuteAction
from mux.errors import ScopeViolation
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

_SEEN_ACTIVITY_CAP = 10_000
_RECOVERY_RETRY_DELAY_S = 1.0
# A turn's output sweep polls the listing for up to 14 s, then uploads: 15 s cut a
# sweep that began 14 s before a deploy's SIGTERM. Within the documented 90 s grace.
DRAIN_GRACE_S = 60.0
# The controls card waits this long at most for the output sweep it goes below.
CONTROLS_WAIT_S = 30.0
_RECOVERY_MAX_RETRY_DELAY_S = 30.0
_FAILED = "Sorry, something went wrong handling that. Please try again."
_SHED = "Too many chats at once right now. Try again in a moment."
_CANCEL_NOT_AUTHOR = "Only the person who asked, or a Daimon admin, can stop this reply."
_CANCEL_ROLE_UNKNOWN = "I couldn't check your role just now. Try again in a moment."
_CANCEL_TURN_ENDED = "This reply has already finished — there is nothing left to cancel."


class _CancelEntry(NamedTuple):
    """What a Stop click needs of a running turn."""

    cancel: asyncio.Event
    author: str  # The asker's Entra id.
    lifecycle: TeamsTurnLifecycle  # The current attempt, which shows the stopping card.


# Everything a turn can raise that is not a bug in this adapter.
_TURN_ERRORS = (DaimonError, anthropic.APIError, SQLAlchemyError, *TEAMS_SEND_ERRORS)
_DB_ERRORS = (SQLAlchemyError, OSError)
_BIND_REFUSALS = (SessionPreparationFailed, SessionBusyError, SessionAgentMismatch)
TEAMS_REFUSAL_NOUNS = RefusalNouns.for_platform("teams")
# A sender from another organisation has no 1:1 chat with Daimon to send `setup` or
# `billing` in, so their refusals name who to ask instead.
TEAMS_FOREIGN_REFUSAL_NOUNS = RefusalNouns.for_platform("teams", foreign=True)


def _not_set_up(nouns: RefusalNouns) -> str:
    """ "Daimon can't answer here": the same words as Discord and Slack, with Teams'
    `setup` command and where to send it."""
    return (
        "Daimon isn't set up in this channel yet.\n\n"
        f"Ask {nouns.who} to {nouns.verb} {nouns.setup}."
    )


def _resolver_miss(nouns: RefusalNouns) -> str:
    return f"This channel's setup is out of date.\n\nAsk {nouns.who} to check {nouns.setup}."


_NOT_SET_UP = _not_set_up(TEAMS_REFUSAL_NOUNS)
_RESOLVER_MISS = _resolver_miss(TEAMS_REFUSAL_NOUNS)
# The log event each refusal is recorded under.
_DENIALS: dict[AdmissionDenialReason, str] = {
    "balance_depleted": "turn.skipped.over_balance",
    "cap_exceeded": "turn.skipped.over_cap",
    "invoker_not_allowed": "turn.skipped.invoker_not_allowed",
    "runs_elsewhere": "turn.skipped.runs_elsewhere",
    "channel_budget_exceeded": "turn.skipped.channel_budget_exceeded",
    "writers_none": "turn.skipped.writers_none",
    "own_agents_only": "turn.skipped.own_agents_only",
    "client_requester": "turn.skipped.client_requester",
    "backend_unsupported": "turn.skipped.backend_unsupported",
    "provider_hold_cap_reached": "turn.skipped.provider_hold_cap_reached",
    "provider_setup_pending": "turn.skipped.provider_setup_pending",
}


def _requester_org(inbound: TeamsInbound) -> tuple[RequesterOrg, str | None]:
    """Which organisation admission is told the sender belongs to.

    Teams keeps its existing behaviour for now: every sender it admits is
    passed as a member, so no Teams turn is held. Fail-closed detection of a
    Teams sender's organisation is separate work. `is_foreign` still reaches
    the turn origin for the wording and the 1:1-only tools that read it.
    """
    del inbound
    return RequesterOrg.HOST, None


_NO_CONTEXT = (
    "I couldn't read this conversation, so there's nothing for me to go on. "
    "Write your question after the mention. If this keeps happening, ask a team owner "
    "to check the app's permission to read channel messages."
)


def _bare_mention(inbound: TeamsInbound) -> bool:
    """A channel or group chat mention with no words or files: it asks about the thread."""
    return inbound.kind != "dm" and not inbound.text.strip() and not inbound.files


LifecycleFactory = Callable[[asyncio.Event, str | None], TeamsTurnLifecycle]
# Builds a continuation turn's handoff notice from what the bind carried across.
HandoffFactory = Callable[[ContinuityOutcome], HandoffNotice]


def _as_mention(inbound: TeamsInbound) -> TeamsInbound:
    """A typed "@<bot name>" read as a mention: the tenant's switch is on."""
    text = strip_typed_name(inbound.text, inbound.bot_name)
    return dataclasses.replace(inbound, text=text, by_name=False, unprompted=False)


def _by_author(queued: list[TeamsInbound]) -> list[list[TeamsInbound]]:
    return group_by_author(queued, lambda item: item.user_id)


def _compose(items: list[TeamsInbound]) -> TeamsInbound:
    """One author's messages as one turn, replying where the last came from.

    Its id is the latest message the person sent rather than a form submit,
    whose id names no message, so 👀 and ✅ land there; a form only when all are.
    """
    sent = [item for item in items if not item.from_form]
    named = sent[-1] if sent else items[-1]
    return dataclasses.replace(
        items[-1],
        activity_id=named.activity_id,
        from_form=not sent,
        text="\n\n".join(item.text for item in items),
        files=tuple(file for item in items for file in item.files),
        composed_ids=tuple(item.activity_id for item in items if item is not named),
    )


def _admission_refusal(
    err: MissingTurnConfigError | MAResolverMissError | AdmissionDenied,
    tenant_id: uuid.UUID,
    *,
    in_dm: bool = False,
    foreign: bool = False,
) -> str | None:
    """Log a refused admission; what to tell the person, if anything."""
    nouns = TEAMS_FOREIGN_REFUSAL_NOUNS if foreign else TEAMS_REFUSAL_NOUNS
    if isinstance(err, MissingTurnConfigError):
        log.info("teams.missing_config", missing=list(err.missing))
        return _not_set_up(nouns)
    if isinstance(err, MAResolverMissError):
        log.warning("teams.resolver.miss", kind=err.kind, daimon_tag=err.daimon_tag)
        return _resolver_miss(nouns)
    log.info(_DENIALS[err.reason], tenant_id=str(tenant_id))
    # A protected channel hears nothing, a refusal included.
    if err.reason == "writers_none":
        return None
    return admission_refusal_text(
        err.reason, nouns, in_dm=in_dm, cause=err.cause, provider=err.provider
    )


async def _controls_after(sweep: asyncio.Task[None], lifecycle: TeamsTurnLifecycle) -> None:
    """The turn's controls below whatever its output sweep posts, or after CONTROLS_WAIT_S."""
    try:
        with contextlib.suppress(Exception):  # The sweep logs its own failure.
            await asyncio.wait_for(asyncio.shield(sweep), CONTROLS_WAIT_S)
    finally:  # A drain's cancel too: the answer landed, so do they.
        await lifecycle.release_controls()


def _agent_name_prefix(
    *, enabled: bool, name: str, metadata: Mapping[str, str] | None, default_name: str | None
) -> str | None:
    if not enabled or is_builtin_agent(
        name=name, metadata=metadata, default_agent_name=default_name
    ):
        return None
    return name


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
        ask_human: bool = False,
        subscriptions: ChannelSubscriptions | None = None,
        catch_up: RestartCatchUp | None = None,
        react: React | None = None,
        feed: ActivityFeed | None = None,
    ) -> None:
        teams = runtime.settings.teams
        if teams is None:
            raise ValueError("TeamsApp requires Teams settings")
        self.runtime = runtime = with_budget_notifier(runtime, direct)
        self._teams = teams
        # Known before any read, so `_may_post` can gate every post.
        self._tenant_id = derive_tenant_uuid(platform="teams", workspace_id=teams.tenant_id)
        # With Graph subscriptions, a channel send refused as not in the roster drops the
        # channel: a lost removal event would otherwise keep its subscription alive.
        self._sender = TimedSender(subscriptions.watch(sender) if subscriptions else sender)
        self._bot_token = bot_token
        # Channel history and media through Graph; None replays nothing.
        self._reader = reader
        # Channel files through SharePoint; None leaves channels without files.
        self._channel_files = channel_files
        # Records each team for the MCP server's channel reads; None records nothing.
        self._installs = installs
        # Commands and sender-only notices; `direct` opens a channel command's 1:1 chat.
        self._private = PrivateReplies(
            runtime=self.runtime,
            sender=self._sender,
            commands=commands,
            direct=direct,
            is_admin=self._is_admin,
        )
        # Posts routine results to their channels; None leaves them pending.
        self._routine_poster = routine_poster
        # Support is on: answers carry an Ask a human button.
        self._ask_human = ask_human
        # Graph subscriptions to private and shared channels; None leaves them mention-only.
        self._subscriptions = subscriptions
        # Reads back what a restart's gap lost; None leaves it lost.
        self._catch_up = catch_up
        self._renewals: asyncio.Task[None] | None = None
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
        self.confirmations = TeamsConfirmationCards(self._sender, feed=feed)
        self.provider_actions = TeamsProviderActions(self._sender, feed=feed)
        self._processing: set[str] = set()
        self._pending: dict[str, list[TeamsInbound]] = {}
        # Controls held for a turn's output sweep, by conversation: the next turn
        # there posts them before its own card.
        self._held_controls: dict[str, TeamsTurnLifecycle] = {}
        # Per-tenant turn slots, with the queue a turn waits in at the cap.
        self.turn_queue = TurnQueue.from_settings(runtime.settings.turn_queue, platform="teams")
        # cancel_key (the card intent id) -> the turn's cancel Event, author and current attempt.
        self._cancel_registry: dict[str, _CancelEntry] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        # Not `spawn`: a reaction never holds up a drain.
        self._reactions = TeamsReactions(react)
        # Bot Framework retries a slow delivery with the same activity id.
        # Not durable: a retry landing after a restart runs a second turn.
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._recovery: asyncio.Task[None] | None = None
        # When the HTTP service began accepting activities: what a restart's catch-up stops at.
        self._serving_from: datetime | None = None
        self._serving = asyncio.Event()
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
        # Exactly-once admission across deliveries and restarts (`admissions`).
        self._admissions = TeamsAdmissions(runtime.sessionmaker, self._tenant_id, runtime.owner_key)
        self._reclaimer: asyncio.Task[None] | None = None
        # A replayed message's (conversation, activity) -> set once it holds its
        # place in the thread queue, so the chat's next replay may follow.
        self._replay_queued: dict[tuple[str, str], asyncio.Event] = {}
        # Messages whose fence hit a database error: the reclaim loop runs them again.
        self._unfenced: list[TeamsInbound] = []

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
                self._reclaimer = asyncio.create_task(
                    self._reclaim_loop(), name="teams.admission-reclaim"
                )
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
                if self._subscriptions is not None:
                    self._renewals = asyncio.create_task(
                        self._subscriptions.run(), name="teams.channel-subscriptions"
                    )
        return self._recovery

    def begin_serving(self) -> None:
        """The service is about to accept Bot Framework activities: what it posted before
        is Graph's to read back (`RestartCatchUp`), what comes after is its own."""
        if self._serving_from is None:
            self._serving_from = datetime.now(UTC)
            self._serving.set()

    async def serving_from(self) -> datetime:
        """When `begin_serving` was called, waiting for it."""
        await self._serving.wait()
        assert self._serving_from is not None
        return self._serving_from

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
                break
            except Exception:
                # Admission waits on this sweep: retry rather than wedge turns.
                log.exception("teams.turn.orphan_recovery_failed", retry_delay_s=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECOVERY_MAX_RETRY_DELAY_S)
        if self._teams.enabled:
            await self._reclaim()
            if self._catch_up is not None:
                # After the reclaim, so a row a predecessor held is replayed, not claimed anew.
                self.spawn(self._catch_up.run(), name="teams.restart-catch-up")

    async def _reclaim(self) -> None:
        """Replay what an exited process admitted but never handled, oldest first per chat.

        At boot it runs inside the sweep, which turns wait for; a chat's replays
        keep their order, but a message sent since may still run before them.
        A failed pass leaves the rows for the next one.
        """
        try:
            reclaimed = await self._admissions.reclaim(now=datetime.now(UTC))
        except _DB_ERRORS as exc:
            log.error("teams.admission.reclaim_failed", exc_info=exc)
            return
        if not reclaimed.replay and not reclaimed.stale:
            return
        log.info(
            "teams.admission.reclaimed",
            replayed=len(reclaimed.replay),
            stale=len(reclaimed.stale),
        )
        if reclaimed.stale:
            self.spawn(self._tell_stale(reclaimed.stale), name="teams.admission-stale")
        for inbound in reclaimed.replay:
            # The pre-filter learns it too, so a redelivery here is dropped cheaply.
            root = inbound.conversation_id.removesuffix(f";messageid={inbound.activity_id}")
            for conversation in {inbound.conversation_id, root}:
                self._first_delivery(conversation, inbound.activity_id)
        self._replay(reclaimed.replay)

    def _replay(self, items: list[TeamsInbound]) -> None:
        chats: dict[str, list[TeamsInbound]] = {}
        for inbound in items:
            chats.setdefault(inbound.conversation_id, []).append(inbound)
        for queue in chats.values():
            self.spawn(self._replay_chat(queue), name="teams.replay")

    async def _tell_stale(self, stale: list[TeamsInbound]) -> None:
        """Ask a too-old message's chat to resend it; a typed name only while it still asks.

        Only where Daimon may post: a protected or unreadable policy gets nothing.
        """
        if any(i.by_name for i in stale) and await self._plain_text_mentions_on():
            stale = [_as_mention(i) if i.by_name else i for i in stale]
        may_post: dict[str, bool] = {}
        for i in stale:
            if not i.by_name and i.conversation_id not in may_post:
                may_post[i.conversation_id] = await self._may_post(i.channel_id, i.thread_id)
        await self._tell([i for i in stale if may_post.get(i.conversation_id)], NOT_HELD)

    async def _reclaim_loop(self) -> None:
        """Catch rows of a predecessor still draining at boot, once it exits; retry
        messages whose fence failed; prune."""
        while not self.draining:
            await asyncio.sleep(RECLAIM_INTERVAL_S)
            await self._reclaim()
            await self._retry_unfenced()
            try:
                await self._admissions.prune(now=datetime.now(UTC))
            except _DB_ERRORS as exc:
                log.warning("teams.admission.prune_failed", error_type=type(exc).__name__)

    async def _retry_unfenced(self) -> None:
        """Run again what a failed fence or an unread policy left unhandled; its intake
        passes the fence first. One too old to replay is closed and told as a stale one.

        A message the fence did hold (its turn started) stays done, never rerun.
        """
        unfenced, self._unfenced = self._unfenced, []
        if not unfenced:
            return
        try:
            pending = await self._admissions.unhandled(unfenced)
        except _DB_ERRORS as exc:
            log.error("teams.admission.retry_failed", exc_info=exc)
            self._unfenced[:0] = unfenced
            return
        if dropped := len(unfenced) - len(pending):
            # Handled after all (an unacknowledged commit), rowless or another owner's.
            log.warning("teams.admission.retry_dropped", count=dropped)
        now = datetime.now(UTC)
        if stale := [i for i in pending if too_old_to_replay(i, now)]:
            self.spawn(self._close_stale(stale), name="teams.admission-stale")
        self._replay([i for i in pending if not too_old_to_replay(i, now)])

    async def _close_stale(self, stale: list[TeamsInbound]) -> None:
        """Fence each chat's too-old retries (a fence is one chat's), then tell them."""
        chats: dict[str, list[TeamsInbound]] = {}
        for inbound in stale:
            chats.setdefault(inbound.conversation_id, []).append(inbound)
        closed = [i for chat in chats.values() for i in await self._finish(chat)]
        self._reactions.clear_held(closed)
        await self._tell_stale(closed)

    async def _replay_chat(self, queue: list[TeamsInbound]) -> None:
        """Feed one chat's replays through the intake in order, each queued behind
        the last before the next starts."""
        for inbound in queue:
            # A predecessor that queued it may have left ⌛ on it; if it queues here
            # again, its new ⌛ follows this removal.
            self._reactions.clear_held([inbound])
            key = (inbound.conversation_id, inbound.activity_id)
            queued = self._replay_queued[key] = asyncio.Event()
            intake = self.spawn(self._intake(inbound), name="teams.turn")
            waiter = asyncio.ensure_future(queued.wait())
            try:
                await asyncio.wait({intake, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                self._replay_queued.pop(key, None)

    async def _hold(self, items: list[TeamsInbound]) -> None:
        """Leave never-started messages for the next boot; tell each chat once.

        They are already stored (`_admit`); the notice is recorded before it is
        sent, so a second drain stays silent. An unrecorded notice is not sent.
        """
        chats: dict[str, list[TeamsInbound]] = {}
        for item in items:
            if not item.unprompted:
                chats.setdefault(item.conversation_id, []).append(item)
        for chat in chats.values():
            try:
                owed = await self._admissions.mark_notice(chat)
            except _DB_ERRORS as exc:
                log.error("teams.admission.notice_unrecorded", exc_info=exc)
                continue
            if owed:
                await self._tell(chat[-1:], HOLDING)

    async def _tell(self, items: list[TeamsInbound], notice: str) -> None:
        """One `notice` per chat, best effort: a failed send is not retried."""
        told: set[str] = set()
        for item in items:
            if item.unprompted or item.conversation_id in told:
                continue
            told.add(item.conversation_id)
            with contextlib.suppress(*TEAMS_SEND_ERRORS):
                await self._say(item, notice)

    async def _hold_arrival(self, activity: MessageActivity, service_url: str | None) -> None:
        """A message landing while the process drains: admit and hold it if it would
        start a turn."""
        parsed = parse_inbound(
            activity, configured_tenant=self._teams.tenant_id, service_url=service_url
        )
        if not isinstance(parsed, Refusal):
            await self._hold_inbound(parsed)

    async def _hold_inbound(self, parsed: TeamsInbound) -> None:
        """Admit and hold a message parsed while draining, if it would start a turn."""
        if parsed.by_name:
            if not await self._plain_text_mentions_on():
                return
            parsed = _as_mention(parsed)
        # Unaddressed chatter is not held: the participation it could feed is cancelled.
        if parsed.unprompted:
            return
        state = await self._post_state(parsed.channel_id, parsed.thread_id)
        if state is ProtectionState.PROTECTED:
            return
        try:
            claimed = await self._admissions.claim(parsed, now=datetime.now(UTC))
        except _DB_ERRORS as exc:
            # Unrecorded, a redelivery could be answered too: stay silent.
            log.error("teams.admission.claim_failed", exc_info=exc)
            return
        if claimed and state.may_post:
            # An unread policy is still kept: its replay reads it again before any post.
            await self._hold([parsed])

    async def _admit(self, parsed: TeamsInbound) -> None:
        """Claim the message's admission, then run the intake; a duplicate stops here.

        Claimed first, so a drain during a slow boot sweep leaves it for the next boot.
        """
        if not await self._claim(parsed):
            return
        if self._recovery is not None:
            await asyncio.shield(self._recovery)  # a predecessor's replays go first
        await self._intake(parsed)

    async def _claim(self, parsed: TeamsInbound) -> bool:
        """Admit the message; False for a duplicate, or when the claim could not be recorded."""
        try:
            claimed = await self._admissions.claim(parsed, now=datetime.now(UTC))
        except _DB_ERRORS as exc:
            # Unrecorded, a redelivery could be answered too: stay silent.
            log.error("teams.admission.claim_failed", exc_info=exc)
            return False
        if not claimed:
            log.info("teams.message.duplicate", conversation_type=parsed.kind)
        return claimed

    async def _intake(self, parsed: TeamsInbound) -> None:
        """Route an admitted message as a fresh one: a replay takes this path too."""
        if parsed.by_name:
            await self._route_by_name(parsed)
        elif parsed.unprompted:
            await self._observe(parsed)
        else:
            await self._handle(parsed)

    async def _finish(self, items: list[TeamsInbound]) -> list[TeamsInbound]:
        """The admission fence before a turn or an answer: the items this process may act on.

        On a database error none: they stay unhandled and silent, kept for the reclaim
        loop to run again.
        """
        try:
            return await self._admissions.finish(items)
        except _DB_ERRORS as exc:
            log.error("teams.admission.finish_failed", exc_info=exc)
            self._unfenced.extend(items)
            return []

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
            admin_ids=await boot_owner_ids(self.runtime, self._teams),
        )

    async def drain(self, timeout: float) -> None:
        """Stop admitting, give turns, output sweeps and the boot sweep `timeout`, then
        cancel the rest.

        Work spawned meanwhile, such as the output sweep of a turn that ends
        during the drain, gets the same deadline. A cancelled turn keeps its
        marker and card intent, so the next boot's sweep edits its card to
        interrupted; a cancelled output sweep leaves its files listed for the
        session's next sweep.
        """
        self.draining = True
        if self._participation is not None:
            self._participation.cancel_all()
        pollers = (self._wake_poller, self._delivery_poller, self._reclaimer, self._renewals)
        for poller in pollers:
            if poller is not None:
                # Wake dispatches drain with the turns; due rows wait for the next boot, and
                # a delivery cut short settles as interrupted, never posted twice.
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            tasks = set(self._tasks)
            if self._recovery is not None and not self._recovery.done():
                tasks.add(self._recovery)
            if not tasks:
                break
            if (left := deadline - loop.time()) > 0:
                await asyncio.wait(tasks, timeout=left)
                continue
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            break
        # A held message keeps its ⌛ until the next boot replays it (`_replay_chat`).
        await self._reactions.close()
        self.outputs.close()

    async def _is_admin(self, inbound: TeamsInbound) -> bool:
        return await inbound_is_admin(self.runtime, self._teams, inbound)

    async def _role(self, inbound: TeamsInbound) -> Role | None:  # None: owners unknown
        return await inbound_role(self.runtime, self._teams, inbound)

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
        if not self._first_delivery(activity.conversation.id, activity.id):
            return
        if self.draining:
            await self._hold_arrival(activity, ctx.conversation_ref.service_url)
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
            await self._refuse(
                parsed,
                activity.conversation.id,
                activity.id,
                activity.conversation.conversation_type,
                reply_privately(ctx),
            )
            return
        if parsed.kind == "channel" and parsed.team_id and self._subscriptions is not None:
            self._subscriptions.notice(
                SeenChannel(
                    team_id=parsed.team_id,
                    team_group_id=parsed.team_group_id,
                    channel_id=parsed.channel_id,
                    channel_type=parsed.channel_type,
                    channel_name=parsed.channel_name,
                    team_name=parsed.team_name,
                    bot_name=parsed.bot_name,
                    service_url=parsed.service_url,
                )
            )
        self._dispatch_inbound(parsed)

    async def handle_graph_message(
        self,
        message: GraphMessage,
        channel: TeamsChannelSubscriptionRow,
        *,
        mentions: bool = False,
    ) -> None:
        """A message Graph reported from a subscribed channel, admitted as a Bot Framework one.

        Its admission row is keyed like the activity's (`admission_channel`), so a
        message seen both ways runs once, across restarts too. The parse drops
        @mentions first, as Bot Framework delivers those, unless `mentions`: a
        boot's catch-up makes up for a restart's gap, when it could not. While
        draining it is held as an activity would be: Graph never resends a
        notice answered 202, and the caller moves its watermark past it, so the
        next boot's reclaim runs it.
        """
        source = GraphSource.of_subscription(channel)
        parsed = self._parse_graph(message, source, mentions=mentions)
        if self.draining:
            if not isinstance(parsed, Refusal):
                await self._hold_inbound(parsed)
        elif not isinstance(parsed, Refusal):
            self._dispatch_inbound(parsed)
        else:
            await self._refuse_graph(parsed, message, source)

    async def catch_up_graph_messages(
        self, source: GraphSource, messages: list[GraphMessage]
    ) -> None:
        """Admit and run what a restart's gap lost, read back from one conversation.

        `messages` are oldest first and include @mentions, which Bot Framework did
        not deliver. Each claims the admission row an activity would: one that
        Bot Framework or an earlier boot already admitted is skipped. The rest
        run in order, as a predecessor's reclaimed ones do. Nothing is read once
        draining; the next boot reads again.
        """
        fresh: list[TeamsInbound] = []
        try:
            for message in messages:
                if self.draining:
                    break
                try:
                    parsed = self._parse_graph(message, source, mentions=True)
                    if isinstance(parsed, Refusal):
                        await self._refuse_graph(parsed, message, source)
                    elif not self._ignored_unprompted(parsed) and await self._claim(parsed):
                        fresh.append(parsed)
                except Exception:
                    # One message must not strand the rows already claimed for replay.
                    log.exception("teams.restart_catch_up.message_failed")
        finally:
            self._replay(fresh)

    def _parse_graph(
        self, message: GraphMessage, source: GraphSource, *, mentions: bool
    ) -> TeamsInbound | Refusal:
        return parse_graph_message(
            message,
            source,
            configured_tenant=self._teams.tenant_id,
            bot_app_id=self._teams.client_id,
            mentions=mentions,
        )

    async def _refuse_graph(
        self, parsed: Refusal, message: GraphMessage, source: GraphSource
    ) -> None:
        """Answer a refused Graph message in its thread, if it is owed an answer."""
        if parsed.text is None:
            return
        conversation = source.channel_id
        if source.kind == "channel":
            conversation += f";messageid={message.reply_to_id or message.id}"

        async def reply(text: str) -> object:
            return await self._sender.send(
                conversation, MessageActivityInput(text=text), service_url=source.service_url
            )

        await self._refuse(parsed, conversation, message.id, source.kind, reply)

    async def _refuse(
        self,
        refusal: Refusal,
        conversation_id: str,
        message_id: str,
        conversation_type: str | None,
        reply: Callable[[str], Awaitable[object]],
    ) -> None:
        """Answer a refused message where it was posted, once, or drop it silently."""
        # Type and reason only, never the text: these drops are otherwise invisible.
        if refusal.text is None:
            # Debug: with channel-message consent Teams delivers every channel post.
            log.debug(
                "teams.message.ignored",
                conversation_type=conversation_type,
                reason="not_mentioned",
            )
            return
        if refusal.by_name and not await self._plain_text_mentions_on():
            return  # a typed name with the switch off asked nothing
        if not await self._may_post(conversation_id.split(";", 1)[0], conversation_id):
            return
        try:
            fresh = await self._admissions.claim_receipt(
                conversation_id, message_id, now=datetime.now(UTC)
            )
        except _DB_ERRORS as exc:
            # Unrecorded, another process could send it too: stay silent.
            log.warning("teams.admission.claim_failed", error_type=type(exc).__name__)
            return
        if fresh:
            try:
                await asyncio.wait_for(reply(refusal.text), SEND_TIMEOUT_S)
            except TEAMS_SEND_ERRORS as exc:
                log.warning(
                    "teams.refusal.send_failed",
                    conversation_type=conversation_type,
                    reason=type(exc).__name__,
                )

    def _dispatch_inbound(self, parsed: TeamsInbound) -> None:
        """Admit a verified message for the mention, typed-name or participation path."""
        if not self._ignored_unprompted(parsed):
            self.spawn(self._admit(parsed), name="teams.turn")

    def _ignored_unprompted(self, parsed: TeamsInbound) -> bool:
        """An unaddressed thread reply with participation off asks nothing: drop it."""
        participation = self.runtime.settings.thread_participation.mode
        unwanted = (
            parsed.unprompted and not parsed.by_name and participation is ParticipationMode.DISABLED
        )
        if unwanted:
            log.debug("teams.message.ignored", conversation_type="channel", reason="disabled")
        return unwanted

    async def _plain_text_mentions_on(self) -> bool:
        """Read the tenant's switch for a message that already names the bot as text."""
        try:
            return await get_plain_text_mentions(
                self.runtime.sessionmaker, tenant_id=self._tenant_id
            )
        except SQLAlchemyError as exc:
            # A failed read leaves the message unaddressed, as with the switch off.
            log.warning("plain_text_mention.lookup_failed", error_type=type(exc).__name__)
            return False

    async def _route_by_name(self, inbound: TeamsInbound) -> None:
        """A typed "@<bot name>": a mention with the tenant's switch on, else as before."""
        if await self._plain_text_mentions_on():
            log.info("plain_text_mention.accepted", conversation_type="channel")
            await self._handle(_as_mention(inbound))
            return
        # Off: a root post stays unanswered and a thread reply is screened as before.
        if not await self._finish([inbound]):
            return
        if (
            inbound.unprompted
            and self.runtime.settings.thread_participation.mode is not ParticipationMode.DISABLED
        ):
            await self._observe(dataclasses.replace(inbound, by_name=False))

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
            return inbound

        await self._participation_for(inbound).observe(inbound, self._tenant_id, admitted=admitted)

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
        # Nobody asked for this turn, so nobody waits on it: a free slot or
        # a silent refusal, never the queue.
        ticket = self.turn_queue.try_claim(tenant_id, cap=cap)
        if ticket is None:
            record_refusal(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="teams",
                channel_id=trigger.channel_id,
                thread_id=trigger.thread_id,
            )
            log.info(
                "turn.skipped.concurrency_shed",
                tenant_id=str(tenant_id),
                in_flight=self.turn_queue.in_flight(tenant_id),
                cap=cap,
                reason="unprompted_at_cap",
            )
            return
        self._last_message_at[key] = datetime.now(UTC)
        async with self._holding(key, ticket):
            # Written on admission, not on answer: a turn that ends silent still spent.
            if self._participation is not None:
                await self._participation.record(trigger, tenant_id)
            await self._run_turns(key, tenant_id, [trigger])

    async def _say(self, inbound: TeamsInbound, text: str, *, sender_only: bool = False) -> None:
        """Post `text` in reply; `sender_only`, or to a targeted message, for its sender alone."""
        if inbound.unprompted:
            # Nobody asked: a refusal or notice per quiet burst would spam the thread.
            log.info("teams.notice.withheld", reason="unprompted")
            return
        if sender_only or inbound.is_targeted:
            await self._private.tell_sender(inbound, text)
            return
        await self._sender.send(
            inbound.conversation_id,
            MessageActivityInput(text=text),
            service_url=inbound.service_url,
        )

    async def _may_post(self, channel_id: str, thread_id: str) -> bool:
        """True only for an unprotected target; a protected or unreadable one gets nothing."""
        return (await self._post_state(channel_id, thread_id)).may_post

    async def _post_state(self, channel_id: str, thread_id: str) -> ProtectionState:
        state = await protection_state(
            self.runtime.sessionmaker,
            tenant_id=self._tenant_id,
            channel_id=channel_id,
            thread_id=thread_id,
        )
        if not state.may_post:
            log.info(
                "turn.skipped.writers_none",
                channel_id=channel_id,
                thread_id=thread_id,
                state=state.value,
            )
        return state

    async def _handle(self, inbound: TeamsInbound) -> None:
        """Error boundary around tenant lookup, routing, commands and the turn loop.

        May-post is decided first, so no post below (denial, pointer, shed, error) skips it.
        """
        state = await self._post_state(inbound.channel_id, inbound.thread_id)
        if state is ProtectionState.UNKNOWN:
            self._unfenced.append(inbound)  # the policy went unread: retried, not dropped
            return
        if not state.may_post:
            await self._finish([inbound])  # nothing to answer, so nothing to replay
            return
        # For the billing card's names; in the background, never failing the turn.
        remember_inbound(self.runtime.sessionmaker, self._tenant_id, inbound)
        try:
            await self._route(inbound)
        except _TURN_ERRORS as exc:
            status, error_code = send_error_details(exc)
            log.error(
                "teams.message.failed",
                conversation_id=inbound.conversation_id,
                status=status,
                error_code=error_code,
                exc_info=exc,
            )
            capture_exception_with_scope(exc)
            if await self._finish([inbound]):  # unfinished, it replays: say nothing
                command = self._private.command(inbound) is not None  # its sender's alone
                with contextlib.suppress(*TEAMS_SEND_ERRORS):
                    await self._say(inbound, _FAILED, sender_only=command)

    async def _route(self, inbound: TeamsInbound) -> None:
        if self._recovery is not None:
            await asyncio.shield(self._recovery)
        tenant_id = await live_tenant_id(self.runtime.sessionmaker, inbound.entra_tenant_id)
        if tenant_id is None:
            if await self._finish([inbound]):
                await self._say(inbound, DENIED, sender_only=True)
            return
        command = self._private.command(inbound)
        if command is None and not inbound.is_targeted:
            await self._orchestrate(inbound, tenant_id)
        elif await self._finish([inbound]):
            await self._private.run(inbound, tenant_id, command)

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
        if replayed := self._replay_queued.get((key, inbound.activity_id)):
            replayed.set()  # wakes only at the next await, after the claim or enqueue
        if key in self._processing:
            self._last_message_at[key] = datetime.now(UTC)
            self._thread_queue.enqueue(key, inbound)
            allowed = functools.partial(self._may_start_turn, inbound, tenant_id)
            self._reactions.mark_queued(inbound, allowed=allowed)
            self._supersede_batch(inbound)
            return
        # Over the cap the turn queues: it posts the ordinary card and waits
        # after it (`wait_for_slot` in _run_turn). Only a full queue refuses.
        ticket = self.turn_queue.admit(tenant_id, cap=cap, conversation_id=key)
        if ticket is None:
            record_refusal(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="teams",
                channel_id=inbound.channel_id,
                thread_id=inbound.thread_id,
            )
            log.info(
                "turn.skipped.concurrency_shed",
                tenant_id=str(tenant_id),
                in_flight=self.turn_queue.in_flight(tenant_id),
                cap=cap,
                reason="queue_full",
            )
            if await self._finish([inbound]):
                await self._say(inbound, _SHED)
            return
        self._last_message_at[key] = datetime.now(UTC)
        self._supersede_batch(inbound)
        async with self._holding(key, ticket):
            await self._run_turns(key, tenant_id, [inbound])

    async def _may_start_turn(self, inbound: TeamsInbound, tenant_id: uuid.UUID) -> bool:
        """Whether admission's people-and-place gates let `inbound` start a turn there.

        ⌛ says an answer is coming, so a protected place or someone off the
        invoker allowlist gets none; nor does anyone while the policy cannot be read.
        """
        try:
            async with self.runtime.sessionmaker() as session:
                policy = await load_access_policy(session, tenant_id=tenant_id)
        except (AccessPolicyUnreadable, *_DB_ERRORS):
            return False
        subject = build_subject(
            is_admin=await self._is_admin(inbound), platform_user_id=inbound.user_id
        )
        place = build_turn_place(channel_id=inbound.channel_id, thread_id=inbound.thread_id)
        return bool(authorize(policy, subject=subject, action=Action.START_TURN, place=place))

    def _supersede_batch(self, inbound: TeamsInbound) -> None:
        """A mention's turn owns the thread now: drop its waiting batch, whose replies it replays.

        Not on arrival: a mention refused before here (protected, command, shed) drops nothing.
        """
        if inbound.kind == "channel" and self._participation is not None:
            self._participation.cancel(inbound.conversation_id)

    @contextlib.asynccontextmanager
    async def _holding(self, key: str, ticket: TurnTicket) -> AsyncIterator[None]:
        """Hold a chat and a turn slot (or a place in the queue); on exit, answer
        what could not run."""
        self._thread_queue.claim(key)
        try:
            with holding(ticket):
                yield
        finally:
            self._release(key)
            unstarted = self._pending.pop(key, [])
            self._reactions.clear_queued(unstarted)
            if self.draining:
                if unstarted:
                    await self._hold(unstarted)
            elif unstarted:
                for item in await self._finish(unstarted):
                    with contextlib.suppress(*TEAMS_SEND_ERRORS):
                        await self._say(item, _FAILED)

    @property
    def _thread_queue(self) -> ThreadQueue[str, TeamsInbound]:
        return ThreadQueue(self._processing, self._pending)

    async def _run_turns(self, key: str, tenant_id: uuid.UUID, queued: list[TeamsInbound]) -> None:
        """One turn per author, composed of the messages its fence still held."""

        async def run(batch: list[TeamsInbound]) -> None:
            self._reactions.clear_queued(batch)
            if self.draining:
                # A turn started now would be cut off: `_holding` holds these instead.
                self._pending.setdefault(key, []).extend(batch)
                return
            kept = await self._finish(batch)  # one another process has drops alone
            if not kept:
                return
            turn = await route_to_setup(self.runtime.sessionmaker, _compose(kept), tenant_id)
            await self._run_turn_guarded(turn, tenant_id)
            await self._dispatch_continuations(turn.thread_id, tenant_id, turn.service_url)

        def compose(queued: list[TeamsInbound]) -> list[list[TeamsInbound]]:
            if self.draining and queued:
                self._pending.setdefault(key, []).extend(queued)
                return []
            return _by_author(queued)

        await self._thread_queue.drain(key, initial=_by_author(queued), compose=compose, run=run)

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
        """The turn inside one outcome observation, so a failure before bind still records.

        One slot per turn: when it ends, the next turn in the chat (a drained
        follow-up or a continuation) re-enters admission (`wait_for_slot`).
        """
        if (held := self._held_controls.pop(inbound.conversation_id, None)) is not None:
            await held.release_controls()  # The last turn's, its sweep still going.
        try:
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
        finally:
            release_turn_slot()

    async def _run_turn_observed(
        self,
        inbound: TeamsInbound,
        tenant_id: uuid.UUID,
        *,
        handoff: HandoffFactory | None,
        reraise: bool,
        continuation: TaskContinuationRow | None,
    ) -> None:
        async with guard_selfhosted_channel(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="teams",
            channel_id=inbound.channel_id,
            thread_id=inbound.thread_id if inbound.kind != "dm" else None,
            caller_id=inbound.user_id,
            force=False,
        ):
            return await self._run_turn_observed_guarded(
                inbound, tenant_id, handoff=handoff, reraise=reraise, continuation=continuation
            )

    async def _run_turn_observed_guarded(
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
        deps, role = self.runtime.turn_deps, await self._role(inbound)
        org, home_id = _requester_org(inbound)
        try:
            admission = await admit(
                deps,
                tenant_id=tenant_id,
                platform="teams",
                external_user_id=inbound.user_id,
                channel_id=inbound.channel_id,
                thread_id=inbound.thread_id,
                role=role,
                now=datetime.now(UTC),
                is_dm=inbound.kind == "dm",
                requester_org=org,
                requester_home_id=home_id,
            )
        except (MissingTurnConfigError, MAResolverMissError, AdmissionDenied) as err:
            refusal = _admission_refusal(
                err, tenant_id, in_dm=inbound.kind == "dm", foreign=inbound.is_foreign
            )
            if refusal is not None and continuation is None:  # queued work settles silently
                private = isinstance(err, AdmissionDenied) and err.reason in SENDER_ONLY
                refusal = await with_admin_team_if_asked(self.runtime, refusal)
                await self._say(inbound, refusal, sender_only=private)
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
        # A ping mentions the asker in a channel or group chat; a 1:1 chat notifies anyway.
        requester = None
        if inbound.kind != "dm":
            requester = Account(id=inbound.user_id, name=inbound.user_name or "you")

        def new_lifecycle(cancel: asyncio.Event, adopt: str | None) -> TeamsTurnLifecycle:
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
                agent_name_prefix=_agent_name_prefix(
                    enabled=identity_enabled_for(
                        self.runtime.settings, "teams", inbound.entra_tenant_id
                    ),
                    name=admission.agent.name,
                    metadata=admission.agent.metadata,
                    default_name=self.runtime.deployment_default.agent_name,
                ),
                ask_human=self._ask_human,
                direct_chat=inbound.kind == "dm",
                foreign=inbound.is_foreign,
                agent_name=admission.agent.name,
                model_id=admission.agent.model.id,
                markup=self.runtime.turn_deps.markup,
                sessionmaker=self.runtime.sessionmaker,
                budget_channel_id=admission.budget_channel_id,
                acknowledge=None if continuation else self._reactions.acknowledger(inbound),
                hold_controls=True,
            )
            holder.append(attempt)
            self._cancel_registry[cancel_key] = _CancelEntry(cancel, inbound.user_id, attempt)
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
                # Over the cap the turn waits here, behind its ordinary card:
                # the queue is never shown.
                slot = await wait_for_slot(
                    cancel,
                    sessionmaker=self.runtime.sessionmaker,
                    tenant_id=tenant_id,
                    default_cap=self._teams.max_concurrent_turns_per_tenant,
                )
                if slot != "started":
                    notice = {
                        "cancelled": CANCELLED_NOTICE,
                        "timed_out": retry_hint(
                            QUEUE_TIMED_OUT_TEXT, direct_chat=inbound.kind == "dm"
                        ),
                        "balance_depleted": admission_refusal_text(
                            "balance_depleted",
                            TEAMS_FOREIGN_REFUSAL_NOUNS
                            if inbound.is_foreign
                            else TEAMS_REFUSAL_NOUNS,
                        ),
                        "queue_full": _SHED,
                    }[slot]
                    await lifecycle.close_with_notice(
                        await with_admin_team_if_asked(self.runtime, notice)
                    )
                    if reraise:
                        # A continuation that never ran must not settle as
                        # delivered: tell its dispatcher.
                        if slot == "balance_depleted":
                            raise AdmissionDenied(reason="balance_depleted")
                        raise TurnNotStarted(slot)
                    return
                await self._bind_and_run(
                    inbound,
                    tenant_id,
                    admission,
                    cancel,
                    holder,
                    markers,
                    new_lifecycle,
                    role=role or Role.USER,  # admission's: one read per turn
                    handoff=handoff,
                    reraise=reraise,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if reraise and isinstance(exc, (*_BIND_REFUSALS, TurnNotStarted, AdmissionDenied)):
                    raise  # Already told the person; the dispatcher settles it.
                # The card exists: collapse it rather than post a second message.
                log.error(
                    "teams.turn.failed", conversation_id=inbound.conversation_id, exc_info=exc
                )
                capture_exception_with_scope(exc)
                await holder[-1].close_with_notice(_FAILED)
                await holder[-1].release_controls()  # An answer that landed keeps its own.
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
        self,
        error: SessionPreparationFailed | SessionBusyError | SessionAgentMismatch,
        agent: BetaManagedAgentsAgent,
        *,
        direct_chat: bool,
        tenant_id: uuid.UUID,
        selfhosted: bool = False,
    ) -> tuple[str, tuple[ExecuteAction, ...]]:
        """The notice for a refused bind, and the Hand over button when the agent changed."""
        name = agent.name
        if isinstance(error, SessionPreparationFailed):
            return render_preparation_failed(name), ()
        if isinstance(error, SessionBusyError):
            return render_current_work_must_finish(name, handoff=True), ()
        owner = "the previous agent"
        if not selfhosted:
            with contextlib.suppress(anthropic.APIStatusError, ScopeViolation):
                owner = (
                    await retrieve_agent(
                        self.runtime.anthropic,
                        error.source_agent_id,
                        scope=resource_scope(tenant_id=str(tenant_id)),
                    )
                ).name
        text = render_responder_changed_without_handoff(
            new_responder=name,
            owner=owner,
            channel="this chat",
            offer_button=not selfhosted,
            new_thread_hint=not direct_chat,
        )
        if selfhosted:
            return text, ()
        return text, (hand_over_button(agent_id=agent.id, agent_name=name),)

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
        role: Role,
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
            text, actions = await self._refusal(
                error,
                admission.agent,
                direct_chat=inbound.kind == "dm",
                tenant_id=tenant_id,
                selfhosted=(
                    admission.backend_revision is not None
                    and admission.backend_revision.backend == "selfhosted"
                ),
            )
            await lifecycle.close_with_notice(text, actions=actions)
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
        if prepared.continuity.announces_replacement() and prepared.continuity.transfer_kind:
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
            previous = holder[-1]
            adopted = new_lifecycle(fresh_cancel, lifecycle.message_id)
            adopted.answer_prefix = lifecycle.answer_prefix
            adopted.inherit_stop(previous)
            return adopted

        reader = self._reader
        # A channel activity carries only the text: its media are on Graph's copy.
        media = await reader.read_media(inbound) if reader else None
        attachments = await prepare_attachments(
            self.runtime.http_client,
            inbound.files,
            bot_token=self._bot_token,
            service_url=inbound.service_url,
            channel=inbound.kind != "dm",
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
            role=role,
            configuration_target_ma_agent_id=config.configuration_target_ma_agent_id,
            configuration_target_name=config.configuration_target_name,
            is_setup=config.thread_binding_kind == "setup",
            sender_foreign=inbound.is_foreign,
            shared_channel=admission.shared_channel,
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
                is_admin=role is Role.ADMIN,
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
                **turn_hooks(
                    self.confirmations,
                    self.provider_actions,
                    runtime=self.runtime,
                    inbound=inbound,
                    prepared=prepared,
                    admission=admission,
                    tenant_id=tenant_id,
                ),
            )
        if outcome.mapping_id is not None:
            markers.add(outcome.mapping_id)
        final = holder[-1]
        await self._finish_outputs(
            inbound,
            outcome,
            final,
            selfhosted=(
                admission.backend_revision is not None
                and admission.backend_revision.backend == "selfhosted"
            ),
        )
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
            if inbound.kind != "dm":
                # The newest message read, not the answer: replies posted while the
                # turn ran are older than the answer, and the next delta needs them.
                newest = (h.newest_id for h in read if h is not None)
                mark = newest_message_id([*inbound.message_ids, *newest]) or mark
            async with self.runtime.sessionmaker.begin() as session:
                await update_watermark(session, id=outcome.mapping_id, watermark_message_id=mark)
        if prepared.continuity.pending:
            # The change was saved where it was made; the next turn applies it.
            log.info("teams.turn.change_pending", reasons=prepared.continuity.pending)

    async def _finish_outputs(
        self,
        inbound: TeamsInbound,
        outcome: RunOutcome,
        final: TeamsTurnLifecycle,
        *,
        selfhosted: bool,
    ) -> None:
        """Release controls immediately when no Anthropic file sweep applies."""
        if not selfhosted and any(
            isinstance(block, ToolUseBlock) for block in outcome.state.content
        ):
            sweep = self.outputs.sweep(
                inbound,
                outcome.ma_session_id,
                append=final.append_to_answer,
                quiet_s=final.outputs_quiet_s(),
            )
            swept = self.spawn(sweep, name="teams.output-sweep")
            self._held_controls[inbound.conversation_id] = final
            controls = self.spawn(_controls_after(swept, final), name="teams.turn-controls")
            controls.add_done_callback(
                functools.partial(self._forget_controls, inbound.conversation_id, final)
            )
        else:
            await final.release_controls()

    def _forget_controls(
        self, conversation_id: str, lifecycle: TeamsTurnLifecycle, _: asyncio.Task[None]
    ) -> None:
        if self._held_controls.get(conversation_id) is lifecycle:
            del self._held_controls[conversation_id]

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

        ticket: TurnTicket | None = None

        def admit_slot() -> bool:
            # A capped wake takes a free slot or waits for the next release;
            # an uncapped one always runs but still counts.
            nonlocal ticket
            ticket = (
                self.turn_queue.try_claim(tenant_id, cap=cap)
                if capped
                else self.turn_queue.claim(tenant_id)
            )
            return ticket is not None

        if (
            not claim_dispatch(
                self._processing,
                conversation_id,
                self._deferred_dispatch,
                thread_id,
                (tenant_id, service_url, capped),
                merge=merge,
                claim_slot=False,
                admit=admit_slot,
            )
            or ticket is None
        ):
            return
        async with self._holding(conversation_id, ticket):

            async def drain() -> None:
                if self.draining:
                    return  # `_holding` holds what is queued
                queued = self._pending.pop(conversation_id, [])
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
            kind=kind_of(conversation, row.parent_channel_id),
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
        """Stop button: the turn's author or a current deployment admin may stop it.

        The answer is the stopping card.
        """
        action = ctx.activity.value.action
        turn = str(action.data.get("turn") or "")
        entry = self._cancel_registry.get(turn)
        if entry is None:
            return toast(_CANCEL_TURN_ENDED)
        clicker = canonical_uuid(ctx.activity.from_.aad_object_id)
        if clicker != entry.author:
            if clicker is None:
                return toast(_CANCEL_NOT_AUTHOR)
            # Live: an owner of the admin team now; None when the owners can't be read.
            role = await deployment_role(
                self._teams, admin_team_owners(self.runtime), user_id=clicker
            )
            if role is None:
                return toast(_CANCEL_ROLE_UNKNOWN)
            if role is not Role.ADMIN:
                return toast(_CANCEL_NOT_AUTHOR)
        stopping = entry.lifecycle.request_stop()
        if stopping is None:  # The closing notice is already on its way.
            return toast(_CANCEL_TURN_ENDED)
        log.info(
            "turn.stop_pressed",
            platform="teams",
            turn=turn,
            requester_id=entry.author,
            pressed_by=clicker,
            by_admin=clicker != entry.author,
        )
        entry.cancel.set()
        # The clicker sees the card stopping at once; the card in the chat is
        # only ever edited by the turn's own lifecycle.
        return replace_card(stopping)

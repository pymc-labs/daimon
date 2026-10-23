"""Discord adapter implementation of the TurnLifecycle protocol.

Created fresh per turn. Accumulates embed state via SSE events,
debounces Discord API calls, and performs a clean replace on terminal success.

Design decisions:
- send/edit callables injected at construction (no discord.py imports required
  for testing)
- 10s debounce between intermediate embed edits
- First SSE event causes immediate embed post
- Terminal success replaces embed with plain text
- Terminal failure shows red error embed that stays visible
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import structlog
from anthropic.types import RawMessageStreamEvent
from daimon.adapters.discord.embed import (
    EmbedData,
    EmbedEvent,
    TurnPhase,
    embed_fields,
    format_termination_notice,
    to_embed_data,
    update,
    update_activity,
)
from daimon.adapters.discord.errors import bound_request_id
from daimon.adapters.discord.output_delivery import AnswerMessage
from daimon.adapters.discord.progress_receipt import InitialProgressReceipt
from daimon.adapters.discord.split import split_for_discord_safe
from daimon.adapters.discord.tables import render_discord_tables
from daimon.adapters.discord.thread_gone import ThreadStopped
from daimon.core.agent_post_identity import fallback_name_prefix
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.channel_budget import balance_footer
from daimon.core.native_footer_cost import native_footer_text
from daimon.core.ops_alerts import alert_ops
from daimon.core.pricing import MODEL_PRICING, cost_of_totals, format_cost
from daimon.core.tenant_balance import debit_amount
from daimon.core.turn.card_state import CardState as EmbedState
from daimon.core.turn.degraded import render_degraded_notice
from daimon.core.turn.lifecycle import Acknowledgment, InterruptSource, ReconnectReason
from daimon.core.turn.notices import RefusalNouns, render_termination_notice
from daimon.core.turn.provider_events import ProviderDraft
from daimon.core.turn.state import (
    TextBlock,
    ToolUseBlock,
    TurnState,
    extract_sealed_responses,
    final_answer_texts,
    join_messages,
)
from daimon.core.turn.status_log import status_log_enabled
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.core.usage_totals import ProviderUsageTotals
from mux.contracts.events import Event
from mux.profiles import emits_legacy_events_for
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCard

log = structlog.get_logger()

# Terminal writes and their repairs may outlive the turn that started them.
_RETAINED_CARD_TASKS: set[asyncio.Task[object]] = set()


async def drain_retained_card_tasks(timeout_s: float) -> int:
    """Wait for retained Discord card writes without cancelling them."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while pending := {task for task in _RETAINED_CARD_TASKS if not task.done()}:
        _, pending = await asyncio.wait(
            pending, timeout=max(0.0, deadline - asyncio.get_running_loop().time())
        )
        if pending:
            return len(pending)
    return 0


def _retain_card_task(task: asyncio.Task[object]) -> None:
    _RETAINED_CARD_TASKS.add(task)
    task.add_done_callback(_RETAINED_CARD_TASKS.discard)


SendFn = Callable[..., Awaitable[discord.Message]]
EditFn = Callable[..., Awaitable[discord.Message | None]]
DeleteFn = Callable[[discord.Message], Awaitable[None]]

_DEBOUNCE_S = 10.0
_PROGRESS_SETTLE_S = 5.0
_TERMINAL_EDIT_S = 10.0
_REPAIR_EDIT_S = 120.0

# A text block sealed by a later tool use posts permanently once it reaches
# this size; shorter sealed blocks are pre-tool narration and stay in the
# ephemeral draft on the status card. Calibrated on real sessions: the
# largest narration block was 429 chars, the smallest swallowed answer 542.
_SEALED_RESPONSE_MIN_CHARS = 500
# Reserve one of the five answer messages for the terminal answer/card.
_MAX_ANSWER_MESSAGES = 5
_OUTPUT_COLLAPSED_NOTICE = "Additional output was combined into the attached answer.txt."
_TOOL_MARKUP_NOTICE = "The model returned tool-call markup without an answer."
_TOOL_CALL_MARKUP = re.compile(
    r"<(?P<tag>invoke_[\w:.-]+|tool_call|tool_use|function_call)\b[^>]*>"
    r".*?(?:</(?P=tag)\s*>|</?$)",
    re.DOTALL,
)


def _pure_tool_markup(text: str) -> bool:
    """Recognise whole pseudo calls, including a truncated final closing tag.

    Match only known call-shaped tags; HTML, code examples with surrounding
    prose and ordinary angle brackets are still valid answer text.
    """
    text = text.strip()
    position = 0
    while position < len(text):
        match = _TOOL_CALL_MARKUP.match(text, position)
        if match is None:
            return False
        position = match.end()
        while position < len(text) and text[position].isspace():
            position += 1
    return bool(text)


class _CardWriteSequencer:
    """Serialize progress and track terminal races across a recovery handover."""

    def __init__(self, owner: DiscordTurnLifecycle) -> None:
        self.owner = owner
        self.epoch = 0
        self.card_ids: set[int] = set()
        self.locks: dict[int, asyncio.Lock] = {}
        self.inflight: dict[int, set[object]] = {}
        self.terminal: dict[int, dict[str, Any]] = {}
        self.terminal_version: dict[int, int] = {}
        self.terminal_tokens: dict[object, tuple[int, int, int]] = {}
        self.terminal_ready: set[int] = set()
        self.dirty: set[int] = set()
        self.repair_task: asyncio.Task[None] | None = None
        self.repair_id: int | None = None
        self.terminal_tasks: set[asyncio.Task[bool]] = set()

    @staticmethod
    def key(message: discord.Message) -> int:
        # Tests and injected transports may use an opaque message reference.
        return getattr(message, "id", id(message))

    def handover(self, owner: DiscordTurnLifecycle) -> None:
        self.epoch += 1
        self.owner = owner
        # The successor may show progress while recovering from the old error.
        self.terminal.clear()
        self.terminal_version.clear()
        self.terminal_ready.clear()
        self.dirty.clear()

    def track(self, message: discord.Message) -> None:
        self.card_ids.add(self.key(message))

    def lock_for(self, message: discord.Message) -> asyncio.Lock:
        return self.locks.setdefault(self.key(message), asyncio.Lock())

    def begin(
        self, message: discord.Message, *, epoch: int, terminal: bool, kwargs: dict[str, Any]
    ) -> object | None:
        message_id = self.key(message)
        if epoch != self.epoch:
            return None
        if terminal:
            render = dict(kwargs)
            render.pop("_allow_replacement", None)
            if "embed" in render:
                embed = render.pop("embed")
                render["embeds"] = [embed] if embed is not None else []
            self.terminal[message_id] = render
            self.terminal_version[message_id] = self.terminal_version.get(message_id, 0) + 1
            self.terminal_ready.discard(message_id)
            token = object()
            self.terminal_tokens[token] = (message_id, epoch, self.terminal_version[message_id])
            return token
        if message_id in self.terminal:
            return None
        token = object()
        self.inflight.setdefault(message_id, set()).add(token)
        return token

    def complete(
        self, message: discord.Message, token: object, *, terminal: bool, applied: bool
    ) -> None:
        message_id = self.key(message)
        if terminal:
            _, epoch, version = self.terminal_tokens.pop(token)
            if applied and epoch == self.epoch and version == self.terminal_version.get(message_id):
                self.terminal_ready.add(message_id)
            elif applied and message_id in self.terminal:
                # A previous terminal request can also finish after a successor
                # or a newer answer has issued its own render.
                self.dirty.add(message_id)
        else:
            pending = self.inflight.get(message_id)
            if pending is not None:
                pending.discard(token)
                if not pending:
                    del self.inflight[message_id]
            if applied and (
                message_id in self.terminal_ready
                or any(target == message_id for target, _, _ in self.terminal_tokens.values())
            ):
                self.dirty.add(message_id)
        self.queue_repair()

    def owns_terminal_replacement(self, token: object, message: discord.Message) -> bool:
        issued = self.terminal_tokens.get(token)
        message_id = self.key(message)
        return (
            issued is not None
            and issued == (message_id, self.epoch, self.terminal_version.get(message_id))
            and self.owner._message_ref is message  # pyright: ignore[reportPrivateUsage]
        )

    def queue_repair(self) -> None:
        owner = self.owner
        message = owner._card_message_ref  # pyright: ignore[reportPrivateUsage]
        if message is None or self.inflight or self.repair_task is not None:
            return
        message_id = self.key(message)
        if message_id not in self.dirty or message_id not in self.terminal_ready:
            return
        self.dirty.discard(message_id)
        log.info(
            "turn.card_repair_scheduled",
            turn_id=str(owner._turn_id),  # pyright: ignore[reportPrivateUsage]
            message_id=str(message_id),
            epoch=self.epoch,
        )
        self.repair_id = message_id
        self.repair_task = asyncio.create_task(
            self._repair(
                message,
                dict(self.terminal[message_id]),
                self.epoch,
                self.terminal_version[message_id],
            ),
            name="discord.terminal_reassert",
        )
        _retain_card_task(self.repair_task)
        owner._terminal_reassert_task = self.repair_task  # pyright: ignore[reportPrivateUsage]

    async def _repair(
        self, message: discord.Message, kwargs: dict[str, Any], epoch: int, version: int
    ) -> None:
        try:
            if epoch == self.epoch and version == self.terminal_version.get(self.key(message)):
                edit = self.owner._urgent_edit or self.owner._edit  # pyright: ignore[reportPrivateUsage]
                await edit(message, _allow_replacement=False, **kwargs)
        except Exception:
            log.warning("turn.terminal_card_reassert_failed", exc_info=True)
        finally:
            self.repair_task = None
            self.repair_id = None
            current = self.owner._card_message_ref  # pyright: ignore[reportPrivateUsage]
            message_id = self.key(message)
            if (
                current is not None
                and self.key(current) == message_id
                and (epoch != self.epoch or version != self.terminal_version.get(message_id))
            ):
                self.dirty.add(message_id)
            self.queue_repair()


def _map_sse_event(event: RawMessageStreamEvent) -> EmbedEvent | None:
    """Map a Managed Agents session SSE event to an EmbedEvent, or None if irrelevant.

    Only the draft rides SSE: tool calls and the Thinking/Working phase are
    read from the turn state on each render, which sees every tool kind.
    """
    if getattr(event, "type", "") != "agent.message":
        return None
    parts: list[object] = getattr(event, "content", [])
    # Full text — the embed state machine clips it for the draft.
    text = "".join(getattr(p, "text", "") for p in parts).strip()
    return EmbedEvent(kind="message", label=text)


def _has_visible_output(state: TurnState) -> bool:
    """Whether the turn has produced anything a reader would see: text or a tool call."""
    return any(
        isinstance(block, ToolUseBlock) or bool(block.text.strip()) for block in state.content
    )


def build_discord_embed(data: EmbedData) -> discord.Embed:
    """Convert EmbedData to a discord.Embed.

    Empty title/description (the collapsed terminal state) are passed as None so
    Discord renders just the colored bar + footer — no blank title/body element.
    """
    embed = discord.Embed(
        title=data.title or None, description=data.description or None, color=data.color
    )
    if data.footer is not None:
        embed.set_footer(text=data.footer)
    if data.notice is not None:
        embed.add_field(name="Notice", value=data.notice[:1024], inline=False)
    for name, value in embed_fields(data):
        embed.add_field(name=name, value=value, inline=False)
    return embed


def _split_with_name_prefix(text: str, agent_name: str) -> list[str]:
    """Keep the fallback sender label attached to the first answer chunk."""
    prefix = fallback_name_prefix(agent_name, "")
    chunks = split_for_discord_safe(text, limit=1900 - len(prefix))
    chunks[0] = prefix + chunks[0]
    return chunks


def _without_adjacent_repeats(messages: list[str], previous: str | None) -> list[str]:
    """``messages`` with each one dropped that repeats the one before it (or ``previous``)."""
    kept: list[str] = []
    for message in messages:
        text = message.strip()
        if not text or text != previous:
            kept.append(message)
        previous = text
    return kept


class DiscordTurnLifecycle:
    """TurnLifecycle implementation for Discord. Created fresh per turn.

    Receives SSE events, accumulates embed state, debounces Discord API calls.
    send and edit callables are injected so the class is testable without
    a real discord.py connection.

    The embed flush (``_maybe_flush``) rides ``on_render``, not
    ``on_sse_event`` -- the pump awaits ``on_sse_event`` inline, so a slow or
    rate-limited Discord edit there would stall the whole consume loop
    (deadening Cancel and blurring the read-timeout clock). Moving it to
    ``on_render`` costs at most one render tick (~2s) of first-update
    latency, which the existing 10s debounce already dominates; accepted
    as-is with no tuning. Terminal flushes (``_flush_terminal``) bypass both
    paths and are unaffected.
    """

    def __init__(
        self,
        *,
        send: SendFn,
        edit: EditFn,
        urgent_edit: EditFn | None = None,
        agent_name: str,
        fallback_active: Callable[[], bool] | None = None,
        model_id: str,
        turn_id: uuid.UUID | None = None,
        markup: Decimal = Decimal(1),
        cancel_view: discord.ui.View | None = None,
        requester_id: int | None = None,
        requester_is_admin: bool = False,
        trigger_message: discord.Message | None = None,
        in_dm: bool | None = None,
        notify_on_completion: bool = False,
        acknowledgment_managed: bool = False,
        render_tables: bool = False,
        clock: Callable[[], float] = time.monotonic,
        adopt_message_ref: discord.Message | None = None,
        adopt_pending_progress: DiscordTurnLifecycle | None = None,
        delete: DeleteFn | None = None,
        unprompted: bool = False,
        on_first_post: Callable[[discord.Message], Awaitable[None]] | None = None,
        on_replacement: Callable[[discord.Message], Awaitable[None]] | None = None,
        request_id: Callable[[], str] = bound_request_id,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        tenant_id: uuid.UUID | None = None,
        budget_channel_id: str | None = None,
        on_initial_progress: Callable[[InitialProgressReceipt], None] | None = None,
        alert_webhook_url: SecretStr | None = None,
        waiting_for_tenant_slot: Callable[[], bool] | None = None,
        thread_stopped: Callable[[], bool] | None = None,
        fleet_card: DiscordFleetCard | None = None,
    ) -> None:
        # Once the turn's thread is gone or closed, nothing more is written:
        # no progress edit, no terminal card, no replacement for a missing card.
        self._thread_stopped = thread_stopped or (lambda: False)
        self._fleet_card = fleet_card
        self._fleet_notice: str | None = None
        self._requester_id = requester_id
        # Only an admin sees the request ref on a stop or failure notice.
        self._requester_is_admin = requester_is_admin
        self._turn_id = turn_id
        self._trigger_message = trigger_message
        if in_dm is None:
            in_dm = isinstance(getattr(trigger_message, "channel", None), discord.DMChannel)
        self._in_dm = in_dm
        self._notify_on_completion = notify_on_completion
        self._acknowledgment_managed = acknowledgment_managed
        self._render_tables = render_tables
        self._send = send
        self._request_id = request_id
        self._sessionmaker = sessionmaker
        self._tenant_id = tenant_id
        self._budget_channel_id = budget_channel_id
        self._on_initial_progress = on_initial_progress
        self._alert_webhook_url = alert_webhook_url
        self._waiting_for_tenant_slot = waiting_for_tenant_slot
        self._queue_card_shown = False
        self._edit = edit
        self._urgent_edit = urgent_edit
        self._delete = delete
        # Nobody asked for an unprompted turn, so it stays invisible until it
        # has something to show: no up-front thinking embed, no "Turn
        # cancelled." left behind, and every message it does post suppresses
        # the push notification.
        self._unprompted = unprompted
        self._agent_name = agent_name
        self._fallback_active = fallback_active
        self._name_prefix_sent = False
        self._model_id = model_id
        self._provider_draft: ProviderDraft | None = None
        self._markup = markup
        self._clock = clock
        self._state = EmbedState(
            phase=TurnPhase.THINKING,
            agent_name=agent_name,
            platform="discord",
            started_at=self._clock(),
        )
        # Seeded only by dead-session recovery, which hands over the message the
        # failed attempt already posted. Without this the recovery lifecycle
        # sends a SECOND message and the first attempt's ❌ embed stays in the
        # thread forever — the user sees a scary upstream error immediately
        # followed by a working answer, and cannot tell that the error was
        # retracted. Adopting the ref means the recovered turn edits that embed
        # into the real answer, so a recovered turn looks like a normal one.
        self._message_ref: discord.Message | None = adopt_message_ref
        self._card_message_ref: discord.Message | None = adopt_message_ref
        self._initial_card_message_ref: discord.Message | None = (
            adopt_pending_progress._initial_card_message_ref
            if adopt_pending_progress is not None
            else adopt_message_ref
        )
        self._terminal_embed: discord.Embed | None = None
        # The answer message that carries the summary, once there is one. The
        # vote emoji and the turn's files go on it too.
        self._summary_ref: discord.Message | None = None
        self._delivered_answer_chunk_ids: list[str] = []
        # A snowflake just past the turn's end: posts before it belong to the turn.
        self._ended_before: int | None = None
        # The newest Discord-side moment this turn's own sends and edits carried.
        self._discord_mark: int | None = None
        self._card_discard_failed = False
        self._last_flush: float = 0.0
        self._terminal: bool = False
        self._progress_edits: set[asyncio.Task[None]] = set()
        self._progress_settled = asyncio.Event()
        self._progress_settled.set()
        self._replacement_lock = asyncio.Lock()
        self._card_writes = _CardWriteSequencer(self)
        if adopt_pending_progress is not None:
            # Shielded edits may outlive the failed turn. Share their settle
            # state and route their completion to this lifecycle's final card.
            self._progress_edits = adopt_pending_progress._progress_edits
            self._progress_settled = adopt_pending_progress._progress_settled
            self._replacement_lock = adopt_pending_progress._replacement_lock
            self._card_writes = adopt_pending_progress._card_writes
            self._card_writes.handover(self)
            old_repair = adopt_pending_progress._terminal_reassert_task
            if old_repair is not None and not old_repair.done():
                self._progress_edits.add(old_repair)
                self._progress_settled.clear()
                old_repair.add_done_callback(self._handover_write_done)
        self._card_epoch = self._card_writes.epoch
        if adopt_message_ref is not None:
            self._card_writes.track(adopt_message_ref)
        if adopt_pending_progress is not None:
            log.info(
                "turn.card_recovery_handover",
                turn_id=str(self._turn_id),
                message_id=str(self._card_writes.key(adopt_message_ref))
                if adopt_message_ref is not None
                else None,
                epoch=self._card_epoch,
            )
        self._terminal_card_embeds: list[discord.Embed] | None = None
        self._terminal_reassert_task: asyncio.Task[None] | None = None
        # A terminal render reached Discord (or there was by design nothing to
        # show), so the card is no longer a pending card with a Stop button.
        self._terminal_shown: bool = False
        self._cancel_view = cancel_view
        self._on_first_post = on_first_post
        self._on_replacement = on_replacement
        self._first_post_attempted: bool = False
        self._persisted_sealed_indices: set[int] = set()
        self._sealed_lock = asyncio.Lock()
        self._sealed_message_count = 0
        self._last_sealed_text: str | None = None
        self._deferred_sealed_text: list[str] = []
        self._output_limit_logged = False
        self._was_answered: bool = False
        # A continuity notice that belongs ABOVE the answer it explains. The
        # answer is an in-place edit of the embed posted at mention time, so a
        # notice sent as its own message always lands below it. Set before the
        # answer is revealed (a planned replacement, known at bind time);
        # `prepend_revealed_answer` covers the fact learned only after the turn ran.
        self.answer_prefix: str | None = None
        self.answer_prefix_applied: bool = False
        # The first chunk actually rendered into the message, kept so a late
        # notice can be edited in above it exactly once.
        self._revealed_first_chunk: str | None = None

    async def on_acknowledgment(self, phase: Acknowledgment) -> None:
        if not self._notify_on_completion or self._trigger_message is None or self._unprompted:
            return
        if phase == "done" and not self._was_answered:
            return
        if phase == "accepted" and self._acknowledgment_managed:
            return
        await self._trigger_message.add_reaction("👀" if phase == "accepted" else "✅")
        if (
            phase == "done"
            and not self._acknowledgment_managed
            and self._trigger_message.guild is not None
        ):
            me = self._trigger_message.guild.me
            await self._trigger_message.remove_reaction("👀", me)

    @property
    def message_ref(self) -> discord.Message | None:
        """The message this lifecycle is rendering into, if it has posted one.

        Public so dead-session recovery can hand it to the replacement
        lifecycle via ``adopt_message_ref``; nothing else should need it.
        """
        return self._message_ref

    def release_message_ref(self) -> discord.Message | None:
        """Give recovery the card; its constructor must adopt pending progress."""
        self.on_render_stopped()
        self._card_message_ref = None
        return self._message_ref

    async def post_initial(self) -> None:
        """Post the initial thinking embed immediately, before the turn starts.

        Called before session setup so the user gets instant feedback --
        MA sessions.create can hold its response for minutes while it
        provisions the session. Runs before the turn starts (and therefore
        before any render tick exists), so this is the one place that
        deliberately flushes directly instead of waiting on `on_render`.

        An unprompted turn posts nothing here: the render path posts the
        embed once the turn has content or tool activity.
        """
        if self._unprompted:
            return
        await self._maybe_flush(initial=True)

    async def on_slot_started(self) -> None:
        """Clear opted-in queue wording on the existing card after the real wait."""
        if not self._queue_card_shown or self._terminal or self._message_ref is None:
            return
        self._waiting_for_tenant_slot = None
        now = self._clock()
        try:
            await self._edit_message(
                self._message_ref, embeds=self._build_embeds(now), view=self._cancel_view
            )
        except Exception:
            # Cosmetic wording must not prevent an admitted turn from running.
            # The ordinary render/terminal path will try this same card again.
            log.info("turn.queue_card_clear_failed", exc_info=True)
            return
        self._last_flush = now
        self._queue_card_shown = False

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        # Cheap local tap per the hardened TurnLifecycle contract:
        # the pump awaits this hook inline, so it stays a local reducer
        # call only. The embed flush (chat-API I/O) rides the render tick
        # instead, where a slow or rate-limited edit only delays the
        # render task, never the consume loop.
        if self._provider_draft is not None:
            return
        embed_event = _map_sse_event(event)
        if embed_event is None:
            return
        self._state = update(self._state, embed_event)

    async def on_provider_event(self, event: Event) -> None:
        """Cheap local tap; only on_render may send the provider's draft."""
        if emits_legacy_events_for(event.native.provider) or self._terminal:
            return
        if self._provider_draft is None:
            self._provider_draft = ProviderDraft()
        text = self._provider_draft.text(event)
        if text is not None:
            self._state = (
                update(
                    self._state,
                    EmbedEvent(
                        kind="message" if self._provider_draft.takes_new_item() else "draft",
                        label=text,
                    ),
                )
                if text
                else dataclasses.replace(self._state, text_preview=None)
            )

    async def _send_message(self, **kwargs: Any) -> discord.Message:  # noqa: ANN401
        """Send through the injected callable, silencing unprompted turns.

        ``kwargs`` are forwarded verbatim to discord.py's overloaded ``send()``.

        `silent=True` is Discord's suppress-notification flag: a reply nobody
        asked for shows up in the thread without pinging anyone.
        """
        if self._unprompted:
            kwargs["silent"] = True
        if self._thread_stopped():
            raise ThreadStopped("thread is gone or closed")
        sent = await self._send(**kwargs)
        self._note_discord_time(getattr(sent, "id", None))
        return sent

    async def edit_card(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Update the turn's card, recovering a deleted card before delivery.

        Also used by admission and session-setup notices before the driver starts.
        """
        if self._thread_stopped():
            return
        await self._edit_message(self._message_ref, **kwargs)

    async def end_card(self, text: str) -> None:
        """Finish a card from the adapter's outer exception/ceiling boundary."""
        try:
            await self._settle_progress()
            self._terminal = True
            if self._thread_stopped():
                return
            await self._edit_message(
                self._message_ref,
                content=text,
                embed=None,
                view=None,
                _allow_replacement=False,
                recover_missing=False,
                missing_is_error=True,
            )
            self._terminal_shown = True
        finally:
            self._mark_ended()
            self._card_writes.queue_repair()

    async def _edit_message(
        self,
        message: discord.Message | None,
        *,
        progress: bool = False,
        terminal_override: bool = False,
        recover_missing: bool = True,
        missing_is_error: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> bool:
        assert message is not None
        original_message = message
        card_write = self._card_writes.key(message) in self._card_writes.card_ids
        terminal = terminal_override or (self._terminal and not progress)
        token: object | None = None
        applied = False
        write_id = str(uuid.uuid4()) if card_write else None
        fields = {
            "turn_id": str(self._turn_id),
            "message_id": str(self._card_writes.key(original_message)),
            "write_id": write_id,
            "kind": "terminal" if terminal else "progress",
            "epoch": self._card_epoch,
        }
        if card_write:
            token = self._card_writes.begin(
                message, epoch=self._card_epoch, terminal=terminal, kwargs=kwargs
            )
            if token is None:
                log.info("turn.card_write_dropped", reason="sealed_or_stale", **fields)
                return False
            log.info("turn.card_write_issued", **fields)
        try:
            if card_write and not terminal:
                async with self._card_writes.lock_for(message):
                    if self._card_epoch != self._card_writes.epoch:
                        log.info("turn.card_write_dropped", reason="handover", **fields)
                        return False
                    if progress and (
                        self._card_writes.key(message) in self._card_writes.terminal
                        or self._message_ref is not message
                    ):
                        log.info("turn.card_write_dropped", reason="overtaken", **fields)
                        return False
                    log.info("turn.card_write_dispatched", **fields)
                    result = await self._perform_edit_message(
                        message,
                        progress=progress,
                        recover_missing=recover_missing,
                        missing_is_error=missing_is_error,
                        **kwargs,
                    )
            else:
                if terminal and not terminal_override and self._message_ref is not None:
                    message = self._message_ref
                if card_write:
                    log.info("turn.card_write_dispatched", **fields)
                if terminal:

                    async def finish_terminal(terminal_token: object | None = token) -> bool:
                        succeeded = False
                        try:
                            succeeded = await self._perform_edit_message(
                                message,
                                progress=progress,
                                recover_missing=recover_missing,
                                missing_is_error=missing_is_error,
                                urgent=True,
                                terminal_token=terminal_token,
                                **kwargs,
                            )
                            if card_write:
                                log.info(
                                    "turn.card_write_completed"
                                    if succeeded
                                    else "turn.card_write_dropped",
                                    **fields,
                                )
                                if succeeded:
                                    log.info("turn.card_terminal_applied", **fields)
                            return succeeded
                        except Exception as err:
                            if card_write:
                                log.info(
                                    "turn.card_write_dropped", reason=type(err).__name__, **fields
                                )
                            raise
                        finally:
                            if terminal_token is not None:
                                self._card_writes.complete(
                                    original_message,
                                    terminal_token,
                                    terminal=True,
                                    applied=succeeded,
                                )

                    task = asyncio.create_task(finish_terminal(), name="discord.terminal_edit")
                    _retain_card_task(task)
                    self._card_writes.terminal_tasks.add(task)
                    task.add_done_callback(self._card_writes.terminal_tasks.discard)
                    task.add_done_callback(
                        lambda done_task: (
                            done_task.exception() if not done_task.cancelled() else None
                        )
                    )
                    # The task owns the token and records the real result even after
                    # this caller's deadline or cancellation.
                    token = None
                    done, _ = await asyncio.wait({task}, timeout=_TERMINAL_EDIT_S)
                    if not done:
                        raise TimeoutError("terminal card edit is still running")
                    return task.result()
                result = await self._perform_edit_message(
                    message,
                    progress=progress,
                    recover_missing=recover_missing,
                    missing_is_error=missing_is_error,
                    **kwargs,
                )
            if card_write:
                log.info(
                    "turn.card_write_completed" if result else "turn.card_write_dropped",
                    resolved_message_id=str(self._card_writes.key(self._message_ref))
                    if self._message_ref is not None
                    else None,
                    **fields,
                )
                if terminal and result:
                    log.info("turn.card_terminal_applied", **fields)
            applied = result
            return result
        except Exception as err:
            if card_write:
                log.info("turn.card_write_dropped", reason=type(err).__name__, **fields)
            raise
        finally:
            if token is not None:
                self._card_writes.complete(
                    original_message, token, terminal=terminal, applied=applied
                )

    async def _perform_edit_message(
        self,
        message: discord.Message,
        *,
        progress: bool,
        recover_missing: bool,
        missing_is_error: bool,
        urgent: bool = False,
        terminal_token: object | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> bool:
        if self._thread_stopped():
            log.info("turn.card_write_dropped", reason="thread_stopped", turn_id=str(self._turn_id))
            return False
        may_outlive_terminal = progress or not self._terminal

        def stale_replacement() -> bool:
            if terminal_token is not None:
                return not self._card_writes.owns_terminal_replacement(terminal_token, message)
            return may_outlive_terminal and (self._terminal or self._message_ref is not message)

        if self._terminal and not progress and message is self._card_message_ref:
            if "embeds" in kwargs:
                self._terminal_card_embeds = kwargs["embeds"]
            elif "embed" in kwargs:
                self._terminal_card_embeds = [kwargs["embed"]] if kwargs["embed"] else []
        try:
            replacement = await ((self._urgent_edit or self._edit) if urgent else self._edit)(
                message, **kwargs
            )
        except discord.HTTPException as err:
            if err.code != 10008:
                raise
            if not recover_missing or (progress and self._terminal):
                if missing_is_error:
                    raise
                return False
            delivered = self._revealed_first_chunk is not None or self._summary_ref is not None
            log.info("turn.message_missing", message_id=str(message.id), delivered=delivered)
            if delivered or self._thread_stopped():
                return False  # a stale edit must not turn a delivered answer into an error
            send_kwargs = dict(kwargs)
            attachments = send_kwargs.pop("attachments", None)
            if attachments:
                send_kwargs["files"] = [a for a in attachments if isinstance(a, discord.File)]
            for key in ("embed", "view"):
                if send_kwargs.get(key) is None:
                    send_kwargs.pop(key, None)
            if self._terminal_embed is not None and not {"embed", "embeds"} & kwargs.keys():
                send_kwargs["embeds"] = [self._terminal_embed]

            async def send_replacement() -> bool:
                # Another missing-card edit may already have replaced this ref.
                if stale_replacement():
                    return False
                replacement = await self._send_message(**send_kwargs)
                if stale_replacement() and await self._reconcile_late_replacement(
                    replacement, message
                ):
                    return False
                self._message_ref = replacement
                if message is self._card_message_ref:
                    self._card_message_ref = replacement
                    self._card_writes.track(replacement)
                if self._on_replacement is not None:
                    await self._on_replacement(replacement)
                return True

            if may_outlive_terminal:
                async with self._replacement_lock:
                    return await send_replacement()
            return await send_replacement()
        edited_at = getattr(replacement, "edited_at", None)
        if isinstance(edited_at, datetime):
            self._note_discord_time(discord.utils.time_snowflake(edited_at, high=True))
        if isinstance(replacement, discord.Message) and replacement.id != message.id:
            if stale_replacement() and await self._reconcile_late_replacement(replacement, message):
                return False
            self._message_ref = replacement
            if message is self._card_message_ref:
                self._card_message_ref = replacement
                self._card_writes.track(replacement)
            if self._on_replacement is not None:
                await self._on_replacement(replacement)
        return True

    async def _reconcile_late_replacement(
        self, replacement: discord.Message, previous: discord.Message
    ) -> bool:
        """Retire a progress card created after terminal delivery or handover.

        A replacement send can outlive the progress edit that requested it. It
        must not take ownership back from a terminal render or its successor.
        """
        owner = self._card_writes.owner
        if owner is self and not self._terminal and self._message_ref is previous:
            return False
        if owner._delete is not None:
            try:
                await owner._delete(replacement)
                return True
            except discord.NotFound as err:
                if err.code == 10008:
                    return True
                log.warning("turn.stale_replacement_delete_failed", exc_info=True)
            except Exception:
                log.warning("turn.stale_replacement_delete_failed", exc_info=True)
        # The adapter may lack delete permission. At least remove the pending
        # control; a terminal render is preferable when one is available.
        try:
            self._card_writes.track(replacement)
            await owner._edit_message(
                replacement,
                terminal_override=True,
                recover_missing=False,
                _allow_replacement=False,
                embeds=owner._terminal_card_embeds or [],
                view=None,
            )
        except Exception:
            log.warning("turn.stale_replacement_reconcile_failed", exc_info=True)
        return True

    def _build_embeds(self, now: float) -> list[discord.Embed]:
        """Render the one status embed: headline, the last few draft lines, plan and tool lines."""
        embed = build_discord_embed(to_embed_data(self._state, now=now))
        if self._waiting_for_tenant_slot is not None and self._waiting_for_tenant_slot():
            embed.title = "Queued — waiting for a free slot…"
            self._queue_card_shown = True
        return [embed]

    async def _maybe_flush(self, *, initial: bool = False) -> None:
        """Post or edit the embeds, subject to debounce. No-op after terminal."""
        if self._terminal or self._thread_stopped():
            return
        now = self._clock()
        if self._message_ref is None:
            # First post — immediate, no debounce
            self._first_post_attempted = True
            embeds = self._build_embeds(now)
            rendered_json = self._initial_render_json(embeds) if initial else None
            message = await self._send_message(embeds=embeds, view=self._cancel_view)
            self._message_ref = message
            self._card_message_ref = message
            self._card_writes.track(message)
            self._initial_card_message_ref = message
            self._last_flush = now
            if rendered_json is not None:
                self._observe_initial_progress(message, rendered_json, operation="send")
            if self._on_first_post is not None:
                # Persist the returned message ID before the turn proceeds. If
                # this fails, retain the prepared intent and stop the caller.
                await self._on_first_post(message)
                self._on_first_post = None
        elif self._on_first_post is not None:
            # A retry after database failure can confirm the same post without
            # issuing another Discord send or starting MA work.
            await self._on_first_post(self._message_ref)
            self._on_first_post = None
        elif now - self._last_flush >= _DEBOUNCE_S:
            embeds = self._build_embeds(now)
            rendered_json = self._initial_render_json(embeds) if initial else None
            # Keep every on-wire edit alive when the render loop is cancelled.
            # Its completion reconciles the card if terminal delivery overtook it.
            task = asyncio.create_task(
                self._edit_progress(self._message_ref, embeds, rendered_json=rendered_json),
                name="discord.progress_edit",
            )
            self._progress_edits.add(task)
            self._progress_settled.clear()
            task.add_done_callback(self._progress_edit_done)
            await asyncio.shield(task)
            self._last_flush = now
        # else: within debounce window — skip

    def on_render_stopped(self) -> None:
        self._terminal = True

    @staticmethod
    def _progress_edit_done(task: asyncio.Task[None]) -> None:
        # A shielded edit may outlive its cancelled waiter. Retrieve failures
        # there too; an active waiter still receives the exception for retry.
        if not task.cancelled():
            task.exception()

    def _handover_write_done(self, task: asyncio.Task[None]) -> None:
        self._progress_edits.discard(task)
        if not self._progress_edits:
            self._progress_settled.set()
        self._card_writes.queue_repair()

    async def _edit_progress(
        self,
        message: discord.Message,
        embeds: list[discord.Embed],
        *,
        rendered_json: str | None = None,
    ) -> None:
        try:
            if not self._terminal and not self._thread_stopped():
                landed = await self._edit_message(
                    message, progress=True, embeds=embeds, view=self._cancel_view
                )
                if landed and rendered_json is not None:
                    assert self._message_ref is not None
                    self._observe_initial_progress(
                        self._message_ref, rendered_json, operation="edit"
                    )
        except Exception:
            log.warning("turn.progress_edit_failed", exc_info=True)
            raise
        finally:
            task = asyncio.current_task()
            assert task is not None
            self._progress_edits.discard(task)
            last_edit = not self._progress_edits
            if last_edit:
                self._progress_settled.set()
            self._card_writes.queue_repair()
            repair = self._card_writes.repair_task
            if last_edit and repair is not None:
                await asyncio.wait({repair}, timeout=_REPAIR_EDIT_S)

    async def _settle_progress(self) -> None:
        self.on_render_stopped()
        if self._progress_edits:
            try:
                async with asyncio.timeout(_PROGRESS_SETTLE_S):
                    await self._progress_settled.wait()
            except TimeoutError:
                pass

    def _initial_render_json(self, embeds: list[discord.Embed]) -> str | None:
        if self._on_initial_progress is None:
            return None
        # Freeze before awaiting send: an early Cancel click may mutate the view.
        return json.dumps(
            {
                "embeds": [embed.to_dict() for embed in embeds],
                "components": self._cancel_view.to_components() if self._cancel_view else [],
            }
        )

    def _observe_initial_progress(
        self,
        message: discord.Message,
        rendered_json: str,
        *,
        operation: Literal["send", "edit"],
    ) -> None:
        if self._on_initial_progress is None:
            return
        self._on_initial_progress(
            InitialProgressReceipt(
                operation=operation,
                destination_id=str(message.channel.id),
                message_id=str(message.id),
                rendered_json=rendered_json,
                observed_s=self._clock(),
            )
        )

    def _apply_usage(self, state: TurnState) -> None:
        """Price the turn's accumulated token totals onto the embed state before a
        terminal flush.

        Price the short and long prompt request totals at their respective rates.
        An unpriced model omits the cost from the footer.
        """
        t = state.usage_totals
        if self._provider_draft is not None or isinstance(t, ProviderUsageTotals):
            # Only an opted-in, authorized host attempt supplies a complete
            # native cost. Compatibility counters never become an MA meter.
            self._state = dataclasses.replace(self._state, cost_str=native_footer_text(t))
            return
        cost = cost_of_totals(
            state.usage_totals,
            state.long_prompt_usage_totals,
            MODEL_PRICING.get(self._model_id),
        )
        if cost is not None:
            # What the tenant is debited, markup included, so `used` matches `left`.
            cost = float(debit_amount(cost, markup=self._markup))
        self._state = dataclasses.replace(self._state, cost_str=format_cost(cost))

    async def _flush_terminal(self, *, summary_on_card: bool = True) -> None:
        """Unconditionally flush terminal state as a single collapsed embed,
        bypassing debounce.

        ``summary_on_card=False`` is for an answer posted as a fresh message:
        that message carries the summary, so the card shows "Done." without it.
        The card is deleted only after the answer is up, best effort, so a card
        with the summary would show it twice until then, or for good if the delete fails.
        """
        if self._sessionmaker is not None and self._tenant_id is not None:
            try:
                async with self._sessionmaker() as session:
                    footer = await balance_footer(
                        session,
                        tenant_id=self._tenant_id,
                        platform="discord",
                        budget_channel_id=self._budget_channel_id,
                        now=datetime.now(UTC),
                    )
                if footer is not None:
                    self._state = dataclasses.replace(self._state, balance_str=footer)
            except Exception:
                log.warning("turn.balance_footer_failed", exc_info=True)
        self._terminal = True
        now = self._clock()
        data = to_embed_data(self._state, now=now)
        self._terminal_embed = build_discord_embed(data)
        embed = (
            self._terminal_embed
            if summary_on_card
            else build_discord_embed(dataclasses.replace(data, footer=None, description="Done."))
        )
        if self._message_ref is None:
            self._message_ref = await self._send_message(embeds=[embed], view=None)
            self._card_message_ref = self._message_ref
            self._card_writes.track(self._message_ref)
            self._initial_card_message_ref = self._message_ref
        else:
            await self._edit_message(self._message_ref, embeds=[embed], view=None)
        self._terminal_shown = True

    async def _persist_sealed_responses(self, state: TurnState) -> None:
        """Post sealed answers (text blocks a later tool call made immutable)
        as permanent messages, once each. Without this, an answer composed
        before a trailing tool call (e.g. a memory-repo write) is discarded by
        the final-response extraction and only the post-tool recap survives."""
        # Fleet turns disclose one collected, reviewed terminal summary.
        # Intermediate sealed text is not that summary, even on failure.
        if self._fleet_card is not None and not await self._fleet_card.allow_sealed_responses():
            return
        async with self._sealed_lock:
            for index, text in extract_sealed_responses(
                state.content, min_chars=_SEALED_RESPONSE_MIN_CHARS
            ):
                if index in self._persisted_sealed_indices:
                    continue
                self._persisted_sealed_indices.add(index)
                previous = self._last_sealed_text
                self._last_sealed_text = text.strip()
                if _pure_tool_markup(text) or text.strip() == previous:
                    log.warning("turn.sealed_response_suppressed", block_index=index)
                    continue
                use_name_prefix = (
                    self._fallback_active is not None
                    and self._fallback_active()
                    and not self._name_prefix_sent
                )
                chunks = (
                    _split_with_name_prefix(text, self._agent_name)
                    if use_name_prefix
                    else split_for_discord_safe(text)
                )
                # Defer a whole block rather than split it across the live
                # thread and attachment. Count actual Discord messages.
                if (
                    self._deferred_sealed_text
                    or self._sealed_message_count + len(chunks) >= _MAX_ANSWER_MESSAGES
                ):
                    self._deferred_sealed_text.append(text)
                    self._log_output_limit()
                    continue
                if use_name_prefix:
                    self._name_prefix_sent = True
                for chunk in chunks:
                    self._sealed_message_count += 1
                    await self._send_message(
                        content=chunk, allowed_mentions=discord.AllowedMentions.none()
                    )
                log.info("turn.sealed_response_posted", block_index=index, chars=len(text))

    def _log_output_limit(self) -> None:
        if not self._output_limit_logged:
            self._output_limit_logged = True
            log.warning(
                "turn.answer_message_limit",
                turn_id=str(self._turn_id),
                limit=_MAX_ANSWER_MESSAGES,
            )

    @staticmethod
    def _overflow_file(text: str) -> discord.File:
        return discord.File(io.BytesIO(text.encode("utf-8")), filename="answer.txt")

    def _final_response(self, state: TurnState) -> str:
        messages = final_answer_texts(state.content)
        if status_log_enabled():
            # Compare like with like: the final messages joined, against the
            # last sealed run, which was joined the same way.
            text = join_messages(_without_adjacent_repeats(messages, None))
            if text.strip() == self._last_sealed_text:
                text = ""
        else:
            text = join_messages(_without_adjacent_repeats(messages, self._last_sealed_text))
        if _pure_tool_markup(text):
            log.warning("turn.final_tool_markup_suppressed", turn_id=str(self._turn_id))
            text = "" if self._unprompted else _TOOL_MARKUP_NOTICE
        return text

    def _note_discord_time(self, snowflake: object) -> None:
        if isinstance(snowflake, int) and (
            self._discord_mark is None or snowflake > self._discord_mark
        ):
            self._discord_mark = snowflake

    def _skip_terminal(self) -> None:
        """End the card without a write: the thread it sits in is gone or closed."""
        self._terminal = True
        log.info("turn.terminal_skipped", reason="thread_stopped", turn_id=str(self._turn_id))

    def _mark_ended(self) -> None:
        """Close the turn's window on Discord's clock; the host's only if Discord gave none.

        The host clock is the fallback for a terminal path that neither sends
        nor edits (an unprompted turn whose card is deleted).
        """
        if self._discord_mark is not None:
            self._ended_before = self._discord_mark + 1
        else:
            self._ended_before = discord.utils.time_snowflake(datetime.now(UTC), high=True)

    async def on_terminal_success(self, state: TurnState) -> None:
        self._discord_mark = None  # only the terminal sends and edits close the window
        try:
            await self._settle_progress()
            if self._thread_stopped():
                self._skip_terminal()
                return
            if self._fleet_card is not None:
                from daimon.adapters.discord.posted_controls.fleet import fleet_result_seen

                allowed, notice = await self._fleet_card.summary_gate(seen=fleet_result_seen(state))
                if not allowed:
                    if notice is None:
                        self._terminal = True
                        return
                    # Never deliver the root's unreviewed final or sealed text.
                    self._state = update_activity(self._state, state)
                    state = dataclasses.replace(
                        state, content=[TextBlock(kind="text", text=notice)]
                    )
                else:
                    # A partial result names its missing workers above the answer.
                    self._fleet_notice = notice
            await self._deliver_success(state)
        finally:
            self._mark_ended()
            self._card_writes.queue_repair()

    async def _deliver_success(self, state: TurnState) -> None:
        self._state = update_activity(self._state, state)
        await self._persist_sealed_responses(state)
        response_text = self._final_response(state)
        if self._deferred_sealed_text:
            response_text = "\n\n".join([*self._deferred_sealed_text, response_text])
        if self._fleet_notice:
            response_text = "\n\n".join(filter(None, [self._fleet_notice, response_text]))
        if self._unprompted and not response_text:
            # No final answer on a turn nobody asked for: leave the thread as
            # it was. A tool trail with nothing to say is noise here, not a
            # "done" state worth keeping (unlike a mention, where the caller
            # watched the tools run); sealed text already posted stays.
            self._terminal = True
            self._terminal_shown = True
            await self._discard_embed()
            log.info("turn.terminal_success", has_text=False, unprompted=True)
            return
        cancelled = state.termination == TerminationReason.INTERRUPTED
        notify = (
            self._notify_on_completion
            and self._requester_id is not None
            and not self._unprompted
            and not cancelled
        )
        self._apply_usage(state)
        self._state = update(self._state, EmbedEvent(kind="done", label=""))
        # A pinged answer is a fresh message with the summary; the card goes.
        await self._flush_terminal(summary_on_card=not (notify and response_text))

        if cancelled:
            if not response_text:
                await self._edit_message(
                    self._message_ref,
                    content="Stopped.\nSend a message to start again.",
                    embed=None,
                    view=None,
                )
                log.info("turn.terminal_success", has_text=False, cancelled=True)
                return
            response_text = f"{response_text}\n\nStopped.\nSend a message to start again."
        if not response_text:
            # If tools ran but no final text, leave done embed visible.
            # If content is entirely empty, show "Turn cancelled."
            has_tool_activity = any(isinstance(block, ToolUseBlock) for block in state.content)
            if has_tool_activity:
                self._was_answered = True
                done_data = dataclasses.replace(
                    to_embed_data(self._state, now=self._clock()), description="Done."
                )
                await self._edit_message(
                    self._message_ref, embed=build_discord_embed(done_data), view=None
                )
                # The "Done." card carries the summary, so the turn's files go on it.
                self._summary_ref = self._message_ref
                # A tool-only turn has no reply to hang the notice under,
                # so a dropped server is named on its own line.
                tool_only_notice = render_degraded_notice(state.mcp_failures)
                if tool_only_notice is not None:
                    await self._send_message(
                        content=tool_only_notice, allowed_mentions=discord.AllowedMentions.none()
                    )
                log.info("turn.terminal_success", has_text=False, tool_only=True)
                return
            await self._edit_message(
                self._message_ref,
                content="Stopped.\nSend a message to start again.",
                embed=None,
                view=None,
            )
            log.info("turn.terminal_success", has_text=False)
            return

        self._was_answered = not cancelled
        if self.answer_prefix is not None:
            response_text = f"{self.answer_prefix}\n\n{response_text}"
            self.answer_prefix_applied = True
        # A server MA dropped this turn is named under the reply, so a
        # degraded answer never reads as a complete one.
        degraded_notice = render_degraded_notice(state.mcp_failures)
        if degraded_notice is not None:
            response_text = f"{response_text}\n\n{degraded_notice}"
        use_name_prefix = (
            self._fallback_active is not None
            and self._fallback_active()
            and not self._name_prefix_sent
        )
        if use_name_prefix:
            self._name_prefix_sent = True
        mentions = discord.AllowedMentions.none()
        if notify:
            assert self._requester_id is not None
            mentions = discord.AllowedMentions(
                users=[discord.Object(id=self._requester_id)],
                roles=False,
                everyone=False,
                replied_user=False,
            )
            response_text = f"<@{self._requester_id}>\n{response_text}"
        collapsed_notice = "\n\n".join(
            part
            for part in (
                _OUTPUT_COLLAPSED_NOTICE,
                "Stopped.\nSend a message to start again." if cancelled else None,
                degraded_notice,
                self.answer_prefix,
            )
            if part
        )
        # Notices can include operator-provided text or many dropped servers.
        # Their complete text is in the file; keep the visible notice bounded.
        if len(collapsed_notice) > 1800:
            collapsed_notice = collapsed_notice[:1750] + "…\nFull details are in answer.txt."
        if notify:
            collapsed_notice = f"<@{self._requester_id}>\n{collapsed_notice}"
        if use_name_prefix:
            collapsed_notice = fallback_name_prefix(self._agent_name, collapsed_notice)
        overflow_files: list[discord.File] = []
        remaining = _MAX_ANSWER_MESSAGES - self._sealed_message_count
        preview_chunks = (
            _split_with_name_prefix(response_text, self._agent_name)
            if use_name_prefix
            else split_for_discord_safe(response_text)
        )
        if self._deferred_sealed_text or len(preview_chunks) > remaining:
            self._log_output_limit()
            overflow_files = [self._overflow_file(response_text)]
            response_text = collapsed_notice
            use_name_prefix = False
        original_response_text = response_text
        table_files: list[discord.File] = []
        if not overflow_files:
            response_text, table_files = await render_discord_tables(
                response_text, enabled=self._render_tables
            )
        chunks = (
            _split_with_name_prefix(response_text, self._agent_name)
            if use_name_prefix
            else split_for_discord_safe(response_text)
        )

        # Table links can expand a short source beyond the remaining budget.
        if len(chunks) > remaining:
            self._log_output_limit()
            for file in table_files:
                file.close()
            table_files = []
            overflow_files = [self._overflow_file(original_response_text)]
            chunks = [collapsed_notice]

        summary = self._terminal_embed
        # A pinged answer's card shows no summary until the answer carries it.
        summary_posted = not notify

        async def deliver_first(content: str, files: list[discord.File]) -> None:
            nonlocal summary_posted
            if notify:
                self._message_ref = await self._send_message(
                    content=content,
                    allowed_mentions=mentions,
                    **({"files": files} if files else {}),
                    **({"embeds": [summary]} if summary and len(chunks) == 1 else {}),
                )
                summary_posted = len(chunks) == 1
                if hasattr(self._message_ref, "id"):
                    self._delivered_answer_chunk_ids.append(str(self._message_ref.id))
            else:
                file_kwargs: dict[str, Any] = {"attachments": files} if files else {}
                await self._edit_message(
                    self._message_ref,
                    content=content,
                    view=None,
                    allowed_mentions=mentions,
                    **file_kwargs,
                    # A long answer ends on its last chunk, so the summary moves there.
                    embeds=[summary] if summary and len(chunks) == 1 else [],
                )

        self._delivered_answer_chunk_ids.clear()
        try:
            try:
                await deliver_first(chunks[0], [*overflow_files, *table_files])
            except discord.HTTPException as exc:
                if not table_files:
                    raise
                log.warning("turn.table_delivery_failed", error_type=type(exc).__name__)
                chunks = (
                    _split_with_name_prefix(original_response_text, self._agent_name)
                    if use_name_prefix
                    else split_for_discord_safe(original_response_text)
                )
                await deliver_first(chunks[0], overflow_files)
            self._revealed_first_chunk = chunks[0]
            self._summary_ref = self._message_ref
            # Overflow: subsequent chunks posted as new messages, the summary under the last.
            for i, chunk in enumerate(chunks[1:], start=2):
                last = summary is not None and i == len(chunks)
                sent = await self._send_message(
                    content=chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                    **({"embeds": [summary]} if last else {}),
                )
                # Discord sends return messages with IDs. Lightweight lifecycle
                # recorders may return opaque refs when they do not test history.
                if hasattr(sent, "id"):
                    self._delivered_answer_chunk_ids.append(str(sent.id))
                if last:
                    self._summary_ref = sent
                    summary_posted = True
        except Exception:
            if not summary_posted:
                await self._restore_card_summary()
            raise
        if notify:
            # The ping posted the answer below the card, so the card goes.
            await self._delete_card()

        log.info("turn.terminal_success")

    async def _restore_card_summary(self) -> None:
        """Put the summary back on the card when the pinged answer could not carry it.

        The card stays: the answer failed before its summary went up, so the
        card is the one place left to show the turn's time and cost. Best
        effort; the delivery error is what the caller reports.
        """
        card = self._card_message_ref
        if card is None or self._terminal_embed is None or self._thread_stopped():
            return
        try:
            restored = await self._edit_message(
                card,
                terminal_override=True,
                recover_missing=False,
                embeds=[self._terminal_embed],
                view=None,
            )
        except Exception:
            log.warning("turn.card_summary_restore_failed", exc_info=True)
            return
        if restored:
            self._summary_ref = self._card_message_ref

    @property
    def feedback_message_id(self) -> str | None:
        """Where the vote emoji go: the message carrying the summary line."""
        if self._summary_ref is not None:
            return str(self._summary_ref.id)
        return self.final_message_id

    @property
    def answer_message(self) -> AnswerMessage | None:
        """Where the output sweep attaches the turn's files: the message with the summary.

        None when the turn left no answer to hang files on (stopped, failed),
        so the sweep posts them on their own.
        """
        if self._summary_ref is None:
            return None
        return AnswerMessage(message_id=self._summary_ref.id, edit=self._edit)

    @property
    def turn_window(self) -> tuple[int, int] | None:
        """Message ids bounding the turn's own posts: after its card, before its end."""
        card = self._initial_card_message_ref
        if card is None or self._ended_before is None:
            return None
        return card.id, self._ended_before

    async def prepend_revealed_answer(self, notice: str) -> bool:
        """Edit `notice` in above an answer already on screen; False if it cannot go there.

        For a fact the turn only produces on its way out (an unexpected
        workspace loss, discovered by the driver's mid-call recovery): by the
        time the caller knows it, the answer has already replaced the embed.
        Sending the notice afterwards puts it below the answer it explains, so
        instead the message is edited once with the notice on top.

        Returns False -- caller sends it as an ordinary message instead -- when
        there is no revealed answer to sit above, or when the notice would push
        the first chunk past Discord's message limit (re-splitting would strand
        the overflow messages already posted).
        """
        if (
            self._revealed_first_chunk is None
            or self._message_ref is None
            or self._thread_stopped()
        ):
            return False
        first_chunk = self._revealed_first_chunk
        prefix = fallback_name_prefix(self._agent_name, "")
        if (
            self._fallback_active is not None
            and self._fallback_active()
            and first_chunk.startswith(prefix)
        ):
            updated = f"{prefix}{notice}\n\n{first_chunk[len(prefix) :]}"
        else:
            updated = f"{notice}\n\n{first_chunk}"
        if len(split_for_discord_safe(updated)) > 1:
            return False
        await self._edit_message(
            self._message_ref,
            content=updated,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._revealed_first_chunk = updated
        self.answer_prefix_applied = True
        return True

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        self._discord_mark = None  # only the terminal sends and edits close the window
        try:
            await self._settle_progress()
            if self._thread_stopped():
                self._skip_terminal()
                return
            await self._deliver_failure(state, err)
            if self._fleet_card is not None:
                # The error card is up; the fleet card must not stay "running".
                try:
                    await self._fleet_card.end_after_error()
                except Exception:
                    log.warning("turn.fleet_card_error_end_failed", exc_info=True)
        finally:
            self._mark_ended()
            self._card_writes.queue_repair()

    async def _deliver_failure(self, state: TurnState, err: Exception) -> None:
        await self._persist_sealed_responses(state)
        self._state = update_activity(self._state, state)
        if (limit := spend_limit_error(err)) is not None:
            log.error(
                "anthropic.spend_limit_reached",
                tenant_id=str(self._tenant_id) if self._tenant_id is not None else None,
                limit=limit,
            )
            alert_ops(
                self._alert_webhook_url,
                key=f"spend_limit:{limit}",
                message=f"Anthropic spend limit reached: {limit} (tenant {self._tenant_id})",
            )
        if self._unprompted and self._message_ref is None and not self._deferred_sealed_text:
            # Same rule as an empty answer: an unprompted turn that never
            # spoke does not announce its own failure into the thread.
            self._terminal = True
            self._terminal_shown = True
            log.warning("turn.terminal_failure", error=str(err), unprompted=True)
            return
        self._apply_usage(state)
        label = "Something went wrong."
        body = "Send your message again." if self._in_dm else "@mention Daimon to try again."
        reason = request_id = None
        # The notice is words on top of the red card, never a reason not to
        # draw it: if building it fails, the card keeps the plain fallback copy.
        try:
            reason = state.termination or termination_reason(err)
            request_id = self._request_id()
            nouns = RefusalNouns.for_platform("discord", place="dm" if self._in_dm else "channel")
            notice = render_termination_notice(
                reason,
                state=state,
                request_id=request_id if self._requester_is_admin else None,
                error=err,
                in_dm=self._in_dm,
                setup_command=nouns.setup,
                billing_command=nouns.billing,
                agent_name=self._agent_name,
            )
            if notice is not None:
                label, body = notice.headline, format_termination_notice(notice)
        except Exception:
            log.warning("turn.terminal_notice_failed", exc_info=True)
        self._state = update(self._state, EmbedEvent(kind="error", label=label))
        self._state = dataclasses.replace(self._state, notice=body, notice_title=label)
        await self._flush_terminal()
        if self._deferred_sealed_text:
            await self._edit_message(
                self._message_ref,
                content=_OUTPUT_COLLAPSED_NOTICE,
                attachments=[self._overflow_file("\n\n".join(self._deferred_sealed_text))],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        log.warning(
            "turn.terminal_failure",
            error=str(err),
            reason=str(reason) if reason is not None else None,
            request_id=request_id,
        )

    async def on_render(self, state: TurnState) -> None:
        # Sole delivery path. Sealed answers first, then the embed
        # flush -- matches on_terminal_success's existing order, so a sealed
        # answer never trails behind an embed that already moved past it.
        if self._terminal or self._thread_stopped():
            return
        if self._fleet_card is not None and await self._fleet_card.refresh():
            return
        self._state = update_activity(self._state, state)
        await self._persist_sealed_responses(state)
        if self._unprompted and self._message_ref is None and not _has_visible_output(state):
            return  # nothing to show yet, and nobody asked: stay invisible
        await self._maybe_flush()

    async def _discard_embed(self) -> None:
        """Delete the embed this turn posted, if any, and forget it. Best effort."""
        if self._message_ref is None or self._delete is None or self._thread_stopped():
            return
        try:
            await self._delete(self._message_ref)
        except discord.NotFound as err:
            # 10008 means the message is gone. 10015 means the webhook is gone,
            # while its message and pending button may still be visible.
            if err.code != 10008:
                self._card_discard_failed = True
                log.info("turn.embed_discard_failed", exc_info=True)
        except (discord.HTTPException, discord.ClientException):
            # Keep the durable intent so a later recovery pass can resolve a
            # card whose pending button may still be visible.
            self._card_discard_failed = True
            log.info("turn.embed_discard_failed", exc_info=True)
        self._message_ref = None

    async def _delete_card(self) -> None:
        """Delete the finished card once the answer is posted below it. Best effort."""
        card = self._card_message_ref
        if (
            card is None
            or self._delete is None
            or card is self._message_ref
            or self._thread_stopped()
        ):
            return
        try:
            await self._delete(card)
        except Exception:  # the answer is already posted; a stale card must not fail the turn
            log.info("turn.card_delete_failed", exc_info=True)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        pass

    async def on_rate_limited(self, until: datetime | None) -> None:
        pass

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        pass

    @property
    def card_message_id(self) -> str | None:
        """Original status card ID, used to retire its durable intent."""
        card = self._initial_card_message_ref
        return str(card.id) if card is not None else None

    @property
    def card_discard_failed(self) -> bool:
        """Whether deleting an unprompted turn's pending card failed."""
        return self._card_discard_failed

    @property
    def final_message_id(self) -> str | None:
        """Discord message ID used for active-turn and recovery bookkeeping."""
        if self._message_ref is None:
            return None
        return str(self._message_ref.id)

    @property
    def watermark_message_id(self) -> str | None:
        """Consumed-input boundary: the terminal card, before any overflow sends."""
        return self.card_message_id or self.final_message_id

    @property
    def delivered_answer_chunk_ids(self) -> tuple[str, ...]:
        """Overflow messages delivered after the card, excluded from the next delta."""
        return tuple(self._delivered_answer_chunk_ids)

    @property
    def ended(self) -> bool:
        """Whether a terminal render (answer, stop or error card) reached Discord.

        False while the card is still the pending card with its Stop button,
        including when the terminal edit itself failed.
        """
        return self._terminal_shown

    @property
    def first_post_attempted(self) -> bool:
        """Whether the lifecycle has invoked Discord for its first card post."""
        return self._first_post_attempted

    @property
    def was_answered(self) -> bool:
        """Whether the turn actually produced an answer.

        False until a terminal success produced either a text answer or
        visible tool activity. A cancelled turn and a failed turn both leave
        this False. Callers need this to tell "the turn ended without error"
        apart from "the turn actually answered" -- the two are NOT the same,
        because a cancellation reaches this class through the success hook.
        """
        return self._was_answered


class _RecoveryReplacement(Exception):
    """A transport returned another card despite replacement being disabled."""


async def edit_recovered_card(
    message: discord.Message,
    *,
    turn_id: uuid.UUID,
    edit: EditFn,
    **kwargs: Any,  # noqa: ANN401
) -> bool:
    """Apply a restart recovery edit through the terminal card write sequencer.

    The old process has exited, so there is no live lifecycle to adopt. A new
    lifecycle owns this existing card and never posts a replacement if it was
    deleted while recovery was fetching it.
    """

    async def no_replacement(**_kwargs: Any) -> discord.Message:  # noqa: ANN401
        raise RuntimeError("restart recovery must not post a replacement card")

    async def guarded_edit(
        card: discord.Message,
        **edit_kwargs: Any,  # noqa: ANN401
    ) -> discord.Message | None:
        edited = await edit(card, **edit_kwargs)
        if isinstance(edited, discord.Message) and edited.id != card.id:
            raise _RecoveryReplacement()
        return edited

    lifecycle = DiscordTurnLifecycle(
        send=no_replacement,
        edit=guarded_edit,
        urgent_edit=guarded_edit,
        agent_name="Daimon",
        model_id="recovery",
        turn_id=turn_id,
        adopt_message_ref=message,
    )
    lifecycle._terminal = True  # pyright: ignore[reportPrivateUsage]
    try:
        return await lifecycle._edit_message(  # pyright: ignore[reportPrivateUsage]
            message, terminal_override=True, recover_missing=False, missing_is_error=True, **kwargs
        )
    except _RecoveryReplacement:
        return False

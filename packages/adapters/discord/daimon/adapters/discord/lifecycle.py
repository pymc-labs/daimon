"""Discord adapter implementation of the TurnLifecycle protocol.

Created fresh per turn. Accumulates embed state via SSE events,
debounces Discord API calls, and performs a clean replace on terminal success.

Design decisions:
- send/edit callables injected at construction (no discord.py imports required
  for testing)
- 10s debounce between intermediate embed edits (SPEC-R5)
- First SSE event causes immediate embed post (SPEC-R1)
- Terminal success replaces embed with plain text (SPEC-R6)
- Terminal failure shows red error embed that stays visible (SPEC-R7)
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import structlog
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.discord.embed import (
    EmbedData,
    EmbedEvent,
    EmbedState,
    TurnPhase,
    format_termination_notice,
    to_embed_data,
    update,
    update_activity,
)
from daimon.adapters.discord.errors import bound_request_id
from daimon.adapters.discord.split import split_for_discord_safe
from daimon.adapters.discord.tables import render_discord_tables
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.ops_alerts import alert_ops
from daimon.core.pricing import MODEL_PRICING, cost_of, format_cost
from daimon.core.stores import tenant_ledger
from daimon.core.turn.degraded import render_degraded_notice
from daimon.core.turn.lifecycle import Acknowledgment, InterruptSource, ReconnectReason
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.state import (
    ToolUseBlock,
    TurnState,
    extract_final_response,
    extract_sealed_responses,
)
from daimon.core.turn.termination import termination_reason
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger()

SendFn = Callable[..., Awaitable[discord.Message]]
EditFn = Callable[..., Awaitable[None]]
DeleteFn = Callable[[discord.Message], Awaitable[None]]

_DEBOUNCE_S = 10.0

# A text block sealed by a later tool use posts permanently once it reaches
# this size; shorter sealed blocks are pre-tool narration and stay in the
# ephemeral draft on the status card. Calibrated on real sessions: the
# largest narration block was 429 chars, the smallest swallowed answer 542.
_SEALED_RESPONSE_MIN_CHARS = 500


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
    return embed


class DiscordTurnLifecycle:
    """TurnLifecycle implementation for Discord. Created fresh per turn.

    Receives SSE events, accumulates embed state, debounces Discord API calls.
    send and edit callables are injected so the class is testable without
    a real discord.py connection.

    D-11: the embed flush (``_maybe_flush``) rides ``on_render``, not
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
        agent_name: str,
        model_id: str,
        cancel_view: discord.ui.View | None = None,
        requester_id: int | None = None,
        trigger_message: discord.Message | None = None,
        notify_on_completion: bool = False,
        render_tables: bool = False,
        clock: Callable[[], float] = time.monotonic,
        adopt_message_ref: discord.Message | None = None,
        delete: DeleteFn | None = None,
        unprompted: bool = False,
        on_first_post: Callable[[discord.Message], Awaitable[None]] | None = None,
        request_id: Callable[[], str] = bound_request_id,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        tenant_id: uuid.UUID | None = None,
        alert_webhook_url: SecretStr | None = None,
    ) -> None:
        self._requester_id = requester_id
        self._trigger_message = trigger_message
        self._notify_on_completion = notify_on_completion
        self._render_tables = render_tables
        self._send = send
        self._request_id = request_id
        self._sessionmaker = sessionmaker
        self._tenant_id = tenant_id
        self._alert_webhook_url = alert_webhook_url
        self._edit = edit
        self._delete = delete
        # Nobody asked for an unprompted turn, so it stays invisible until it
        # has something to show: no up-front thinking embed, no "Turn
        # cancelled." left behind, and every message it does post suppresses
        # the push notification.
        self._unprompted = unprompted
        self._agent_name = agent_name
        self._model_id = model_id
        self._clock = clock
        self._state = EmbedState(
            phase=TurnPhase.THINKING,
            agent_name=agent_name,
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
        self._last_flush: float = 0.0
        self._terminal: bool = False
        self._cancel_view = cancel_view
        self._on_first_post = on_first_post
        self._first_post_attempted: bool = False
        self._persisted_sealed_indices: set[int] = set()
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
        await self._trigger_message.add_reaction("👀" if phase == "accepted" else "✅")
        if phase == "done" and self._trigger_message.guild is not None:
            me = self._trigger_message.guild.me
            await self._trigger_message.remove_reaction("👀", me)

    @property
    def message_ref(self) -> discord.Message | None:
        """The message this lifecycle is rendering into, if it has posted one.

        Public so dead-session recovery can hand it to the replacement
        lifecycle via ``adopt_message_ref``; nothing else should need it.
        """
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
        await self._maybe_flush()

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        # Cheap local tap per the hardened TurnLifecycle contract (D-11):
        # the pump awaits this hook inline, so it stays a local reducer
        # call only. The embed flush (chat-API I/O) rides the render tick
        # instead, where a slow or rate-limited edit only delays the
        # render task, never the consume loop.
        embed_event = _map_sse_event(event)
        if embed_event is None:
            return
        self._state = update(self._state, embed_event)

    async def _send_message(self, **kwargs: Any) -> discord.Message:  # noqa: ANN401
        """Send through the injected callable, silencing unprompted turns.

        ``kwargs`` are forwarded verbatim to discord.py's overloaded ``send()``.

        `silent=True` is Discord's suppress-notification flag: a reply nobody
        asked for shows up in the thread without pinging anyone.
        """
        if self._unprompted:
            kwargs["silent"] = True
        return await self._send(**kwargs)

    def _build_embeds(self, now: float) -> list[discord.Embed]:
        """Render the one status embed: headline, tool lines and the latest draft."""
        return [build_discord_embed(to_embed_data(self._state, now=now))]

    async def _maybe_flush(self) -> None:
        """Post or edit the embeds, subject to debounce. No-op after terminal."""
        if self._terminal:
            return
        now = self._clock()
        if self._message_ref is None:
            # First post — immediate, no debounce
            self._first_post_attempted = True
            message = await self._send_message(
                embeds=self._build_embeds(now), view=self._cancel_view
            )
            self._message_ref = message
            self._card_message_ref = message
            self._last_flush = now
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
            # Debounce elapsed — edit
            await self._edit(
                self._message_ref, embeds=self._build_embeds(now), view=self._cancel_view
            )
            self._last_flush = now
        # else: within debounce window — skip

    def _apply_usage(self, state: TurnState) -> None:
        """Fold the turn's accumulated token totals + priced cost onto the
        embed state before a terminal flush.

        Reconstructs a per-turn ``BetaManagedAgentsSpanModelUsage`` from the four
        cache-split totals and prices it through the same ``cost_of`` the billing
        ledger uses, so the footer cost matches the ledger to the cent. The
        displayed input count stays merged (input + cache_creation + cache_read);
        only the cost math is stage-split, inside ``cost_of``. An unpriced model
        yields ``cost_of`` -> None -> ``cost_str`` None -> footer omits the cost.
        """
        t = state.usage_totals
        usage = BetaManagedAgentsSpanModelUsage(
            input_tokens=t.input_tokens,
            cache_creation_input_tokens=t.cache_creation_input_tokens,
            cache_read_input_tokens=t.cache_read_input_tokens,
            output_tokens=t.output_tokens,
            speed="standard",
        )
        cost = cost_of(usage, MODEL_PRICING.get(self._model_id))
        merged_in = t.input_tokens + t.cache_creation_input_tokens + t.cache_read_input_tokens
        self._state = dataclasses.replace(
            self._state,
            usage_in=merged_in,
            usage_out=t.output_tokens,
            cost_str=format_cost(cost),
        )

    async def _flush_terminal(self) -> None:
        """Unconditionally flush terminal state as a single collapsed embed,
        bypassing debounce."""
        if self._sessionmaker is not None and self._tenant_id is not None:
            try:
                async with self._sessionmaker() as session:
                    balance = await tenant_ledger.get_prepaid_balance(
                        session, tenant_id=self._tenant_id
                    )
                if balance is not None:
                    self._state = dataclasses.replace(
                        self._state, balance_str=f"${balance:.2f} left"
                    )
            except Exception:
                log.warning("turn.balance_footer_failed", exc_info=True)
        self._terminal = True
        now = self._clock()
        data = to_embed_data(self._state, now=now)
        embed = build_discord_embed(data)
        if self._message_ref is None:
            self._message_ref = await self._send_message(embeds=[embed], view=None)
            self._card_message_ref = self._message_ref
        else:
            await self._edit(self._message_ref, embeds=[embed], view=None)

    async def _persist_sealed_responses(self, state: TurnState) -> None:
        """Post sealed answers (text blocks a later tool call made immutable)
        as permanent messages, once each. Without this, an answer composed
        before a trailing tool call (e.g. a memory-repo write) is discarded by
        the final-response extraction and only the post-tool recap survives."""
        for index, text in extract_sealed_responses(
            state.content, min_chars=_SEALED_RESPONSE_MIN_CHARS
        ):
            if index in self._persisted_sealed_indices:
                continue
            self._persisted_sealed_indices.add(index)
            for chunk in split_for_discord_safe(text):
                await self._send_message(
                    content=chunk, allowed_mentions=discord.AllowedMentions.none()
                )
            log.info("turn.sealed_response_posted", block_index=index, chars=len(text))

    async def on_terminal_success(self, state: TurnState) -> None:
        await self._persist_sealed_responses(state)
        if self._unprompted and not extract_final_response(state.content):
            # No final answer on a turn nobody asked for: leave the thread as
            # it was. A tool trail with nothing to say is noise here, not a
            # "done" state worth keeping (unlike a mention, where the caller
            # watched the tools run); sealed text already posted stays.
            self._terminal = True
            await self._discard_embed()
            log.info("turn.terminal_success", has_text=False, unprompted=True)
            return
        self._apply_usage(state)
        self._state = update(self._state, EmbedEvent(kind="done", label=""))
        await self._flush_terminal()

        response_text = extract_final_response(state.content)
        if not response_text:
            # If tools ran but no final text, leave done embed visible.
            # If content is entirely empty (cancellation), show "Turn cancelled."
            has_tool_activity = any(isinstance(block, ToolUseBlock) for block in state.content)
            if has_tool_activity:
                self._was_answered = True
                # #79: a tool-only turn has no reply to hang the notice under,
                # so a dropped server is named on its own line.
                tool_only_notice = render_degraded_notice(state.mcp_failures)
                if tool_only_notice is not None:
                    await self._send_message(
                        content=tool_only_notice, allowed_mentions=discord.AllowedMentions.none()
                    )
                log.info("turn.terminal_success", has_text=False, tool_only=True)
                return
            await self._edit(self._message_ref, content="Turn cancelled.", embed=None, view=None)
            log.info("turn.terminal_success", has_text=False)
            return

        self._was_answered = True
        if self.answer_prefix is not None:
            response_text = f"{self.answer_prefix}\n\n{response_text}"
            self.answer_prefix_applied = True
        # #79: a server MA dropped this turn is named under the reply, so a
        # degraded answer never reads as a complete one.
        degraded_notice = render_degraded_notice(state.mcp_failures)
        if degraded_notice is not None:
            response_text = f"{response_text}\n\n{degraded_notice}"
        notify = (
            self._notify_on_completion and self._requester_id is not None and not self._unprompted
        )
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
        original_response_text = response_text
        response_text, table_files = await render_discord_tables(
            response_text, enabled=self._render_tables
        )
        chunks = split_for_discord_safe(response_text)

        async def deliver_first(content: str, files: list[discord.File]) -> None:
            if notify:
                self._message_ref = await self._send_message(
                    content=content,
                    allowed_mentions=mentions,
                    **({"files": files} if files else {}),
                )
            else:
                await self._edit(
                    self._message_ref,
                    content=content,
                    embed=None,
                    view=None,
                    allowed_mentions=mentions,
                    **({"attachments": files} if files else {}),
                )

        try:
            await deliver_first(chunks[0], table_files)
        except discord.HTTPException as exc:
            if not table_files:
                raise
            log.warning("turn.table_delivery_failed", error_type=type(exc).__name__)
            chunks = split_for_discord_safe(original_response_text)
            await deliver_first(chunks[0], [])
        self._revealed_first_chunk = chunks[0]
        # Overflow: subsequent chunks posted as new messages
        for chunk in chunks[1:]:
            await self._send_message(content=chunk, allowed_mentions=discord.AllowedMentions.none())

        log.info("turn.terminal_success")

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
        if self._revealed_first_chunk is None or self._message_ref is None:
            return False
        updated = f"{notice}\n\n{self._revealed_first_chunk}"
        if len(split_for_discord_safe(updated)) > 1:
            return False
        await self._edit(
            self._message_ref,
            content=updated,
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._revealed_first_chunk = updated
        self.answer_prefix_applied = True
        return True

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
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
        if self._unprompted and self._message_ref is None:
            # Same rule as an empty answer: an unprompted turn that never
            # spoke does not announce its own failure into the thread.
            self._terminal = True
            log.warning("turn.terminal_failure", error=str(err), unprompted=True)
            return
        self._apply_usage(state)
        label = str(err)[:100]
        body = ""
        reason = request_id = None
        # The notice is words on top of the red card, never a reason not to
        # draw it: if building it fails, the card falls back to the raw label.
        try:
            reason = state.termination or termination_reason(err)
            request_id = self._request_id()
            notice = render_termination_notice(
                reason, state=state, request_id=request_id, error=err
            )
            if notice is not None:
                label, body = notice.headline, format_termination_notice(notice)
        except Exception:
            log.warning("turn.terminal_notice_failed", exc_info=True)
        self._state = update(self._state, EmbedEvent(kind="error", label=label))
        self._state = dataclasses.replace(self._state, notice=body)
        await self._flush_terminal()
        log.warning(
            "turn.terminal_failure",
            error=str(err),
            reason=str(reason) if reason is not None else None,
            request_id=request_id,
        )

    async def on_render(self, state: TurnState) -> None:
        # Sole delivery path (D-11). Sealed answers first, then the embed
        # flush -- matches on_terminal_success's existing order, so a sealed
        # answer never trails behind an embed that already moved past it.
        if self._terminal:
            return
        self._state = update_activity(self._state, state)
        await self._persist_sealed_responses(state)
        if self._unprompted and self._message_ref is None and not _has_visible_output(state):
            return  # nothing to show yet, and nobody asked: stay invisible
        await self._maybe_flush()

    async def _discard_embed(self) -> None:
        """Delete the embed this turn posted, if any, and forget it. Best effort."""
        if self._message_ref is None or self._delete is None:
            return
        try:
            await self._delete(self._message_ref)
        except discord.HTTPException:
            # Already gone, or no permission: a stale embed beats failing a
            # turn that otherwise ended cleanly.
            log.info("turn.embed_discard_failed", exc_info=True)
        self._message_ref = None

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        pass

    async def on_rate_limited(self, until: datetime | None) -> None:
        pass

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        pass

    @property
    def card_message_id(self) -> str | None:
        """Original status card ID, used to retire its durable intent."""
        return str(self._card_message_ref.id) if self._card_message_ref is not None else None

    @property
    def final_message_id(self) -> str | None:
        """Discord message id of the last embed the bot posted this turn, or None
        if nothing was sent (the watermark source for session-per-thread reuse)."""
        if self._message_ref is None:
            return None
        return str(self._message_ref.id)

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

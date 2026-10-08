"""DaimonBot -- Discord adapter event-driven controller."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final, Literal

import anthropic as _anthropic
import sentry_sdk
import structlog
import structlog.contextvars
from daimon.adapters.discord import theme
from daimon.adapters.discord.attachments import build_attachment_url_prefix
from daimon.adapters.discord.budget_notice import with_budget_notifier
from daimon.adapters.discord.checks import is_member_guild_admin, member_role_ids
from daimon.adapters.discord.context import (
    build_channel_context_xml,
    build_context_xml,
    build_delta_xml,
)
from daimon.adapters.discord.continuation_dispatch import dispatch_pending_continuations
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.feedback_seed import seed_feedback_reactions
from daimon.adapters.discord.gating import is_participation_candidate, should_process_message
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.names import remember_guild_user
from daimon.adapters.discord.output_delivery import deliver_session_outputs
from daimon.adapters.discord.permissions import check_missing_permissions
from daimon.adapters.discord.post_transport import DiscordPostTransport, known_webhook_ids
from daimon.adapters.discord.routine_delivery import make_discord_routine_poster
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.thread_naming import generate_thread_name
from daimon.adapters.discord.thread_participation import ThreadParticipant
from daimon.adapters.discord.thread_send import safe_thread_send
from daimon.adapters.discord.tool_confirmation import discord_confirmation_hook
from daimon.adapters.discord.turn_card_recovery import (
    post_initial_turn_card,
    reconcile_turn_card_intent,
    retire_terminal_turn_card,
)
from daimon.adapters.discord.turn_posts import TurnPostRecorder, archive_thread_quietly
from daimon.adapters.discord.views import CancelView
from daimon.adapters.discord.vision import (
    build_image_url_prefix,
    build_skipped_image_prefix,
    download_as_image_blocks,
    is_vision_image_attachment,
)
from daimon.core.agent_identity import AgentIdentity, resolve_agent_identity
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.channel_budget_notice import drain_budget_notices
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.continuity.continuation import (
    ContinuationDecision,
    check_wake_responder,
    load_asking_agent_id,
)
from daimon.core.continuity.messages import (
    render_access_changed_try_again,
    render_current_work_must_finish,
    render_preparation_failed,
    render_replacement_summary,
    render_unexpected_loss,
)
from daimon.core.continuity.wakes import WakeThread, run_wake_poller, skip_thread_wakes
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.defaults.provisioning import provision_tenant, reconcile_tenant_defaults
from daimon.core.defaults.report import compose_failure_reason
from daimon.core.errors import DaimonError, TurnError
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.ops_alerts import alert_ops
from daimon.core.participation_gates import BATCH_MAX_MESSAGES, BATCH_MAX_QUIET_PERIODS
from daimon.core.routine_delivery import run_delivery_poller
from daimon.core.stores.agent_posts import get_post
from daimon.core.stores.domain import Role, TaskContinuationRow, TenantRow, TurnCardIntentRow
from daimon.core.stores.promo_codes import has_redeemable_promo_code
from daimon.core.stores.tenants import (
    get_tenant_liveness,
    get_turn_cap,
    list_tenants_by_platform,
    set_provision_status,
)
from daimon.core.stores.thread_agent_bindings import update_lifecycle
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    clear_active_turn_if_message_id,
    get_live_thread_session,
    list_orphaned_turns,
    mark_turn_active,
    update_watermark,
)
from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
from daimon.core.stores.turn_origins import get_active_origin, thread_archive_requested
from daimon.core.thread_naming import strip_mentions
from daimon.core.thread_participation import ParticipationMode
from daimon.core.turn.admission import AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.bookkeeping import recover_orphan_marker
from daimon.core.turn.ceiling import turn_deadline
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
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.protection import ProtectionState, protection_state
from daimon.core.turn.run import RunOutcome, run_prepared_turn
from daimon.core.turn.state import ToolUseBlock
from daimon.core.turn.thread_queue import (
    ThreadQueue,
    claim_dispatch,
    dispatch_and_drain,
    group_by_author,
    release_thread,
)
from daimon.core.turn_keys import list_mounted_key_names
from daimon.core.turn_origin import (
    HandoffNotice,
    SessionState,
    holds_current_channel_admin_grant,
    render_turn_origin,
    turn_origin,
)
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord
from discord.ext import commands

log = structlog.get_logger()


def log_anthropic_overload(
    exc: object, *, tenant_id: uuid.UUID, path: str, alert_webhook_url: SecretStr | None = None
) -> None:
    """Surface provider throttling that the SDK eventually gives up retrying."""
    if spend_limit_error(exc) is not None:
        return
    if isinstance(exc, TurnError):
        exc = exc.cause
    if isinstance(exc, _anthropic.APIStatusError) and exc.status_code in (429, 529):
        log.warning(
            "turn.anthropic_overloaded",
            tenant_id=str(tenant_id),
            path=path,
            status_code=exc.status_code,
        )
        alert_ops(
            alert_webhook_url,
            key="overloaded",
            message=f"Anthropic overloaded: HTTP {exc.status_code} (tenant {tenant_id})",
        )


_EMBED_COLOR = theme.COLOR_BLURPLE  # Blurple — repo standard (help.py D-FORMAT-01).
GLOBAL_CAP_NOTICE = "Daimon is at capacity right now — try again in a minute."

# Grace window for graceful shutdown drain. Must match the deployment's
# container kill/stop timeout of 60s. The drain polls _processing up to this
# many seconds before calling close(), ensuring in-flight turns are not cut
# mid-stream.
_DRAIN_GRACE_S: float = 60.0

# Bounded concurrency for the on_ready re-seed sweep. Each tenant reconcile
# issues roughly two dozen Skills API calls (a read per seeded skill, plus an
# upload per skill that changed), and that API is rate limited per ORG at 100
# requests/minute -- shared across every deployment on the operator's key. At 4
# a cold sweep of 16 tenants exceeded it and the promote of 2026-08-20 landed
# only 5 of 16 tenants' skills before 429ing; the agent reconcile then failed
# attaching skills the sweep had never created.
_SWEEP_CONCURRENCY = 2
_TURN_CARD_RECOVERY_CONCURRENCY = 4


async def _open_thread_with_notice(
    message: discord.Message,
    opening: Coroutine[Any, Any, discord.Thread],
    *,
    guild_id: str,
    after_s: float,
) -> discord.Thread:
    """Keep an opening mention visible while naming or Discord creation waits."""
    task = asyncio.create_task(opening)
    notice: discord.Message | None = None
    try:
        try:
            if after_s > 0:
                return await asyncio.wait_for(asyncio.shield(task), timeout=after_s)
        except TimeoutError:
            pass
        try:
            notice = await message.reply(
                "Opening your chat… Discord is busy, this can take a minute.",
                mention_author=False,
            )
        except discord.HTTPException as exc:
            log.warning("discord.thread_open_notice_failed", error=str(exc))
        try:
            thread = await task
        except Exception:
            if notice is not None:
                try:
                    await notice.edit(
                        content="I couldn't open your chat. Please try mentioning me again."
                    )
                except discord.HTTPException as exc:
                    log.warning("discord.thread_open_notice_edit_failed", error=str(exc))
            raise
        if notice is not None:
            try:
                await notice.edit(
                    content=(
                        f"Your chat is ready: https://discord.com/channels/{guild_id}/{thread.id}"
                    )
                )
            except discord.HTTPException as exc:
                log.warning("discord.thread_open_notice_edit_failed", error=str(exc))
        return thread
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _resolve_bot_display_name(settings: Settings) -> str:
    """Read the operator-configured bot presented name (SPEC req 9).

    Defaults to "daimon" when Discord settings are unset — the same default
    ``DiscordSettings.bot_display_name`` carries, kept here too so callers
    that only have a bare ``Settings`` (discord block optional) never crash.
    """
    return settings.discord.bot_display_name if settings.discord is not None else "daimon"


def _responder_handle(settings: Settings) -> str:
    """The handle the turn's own history shows people mentioning.

    `context.py` rewrites `<@bot_id>` to `@{bot_display_name}`, so this is the
    exact string the model reads in the message that summoned it. The turn
    controls carry it beside the MA agent name so a deployment whose bot is
    named `daimon-staging` is not read as a second agent.
    """
    return f"@{_resolve_bot_display_name(settings)}"


def _setting_up_message(bot_display_name: str) -> str:
    """Distinct from the MAResolverMissError "no longer exists" message so a
    still-provisioning state is never confused with a genuine misconfiguration."""
    return f"{bot_display_name.capitalize()} is setting up this server — try again in a moment."


async def _resolve_category(channel: object, *, fetch: bool = True) -> tuple[str | None, bool]:
    """``(category id, unresolved)`` for a channel, or for a thread's parent --
    what the access policy's protected categories are matched against.

    An uncached thread parent is fetched (``fetch=True``) and added to the
    guild cache, so the admission that follows reads it without a second REST
    call; with ``fetch=False``, or if the fetch fails, the category is
    unresolved, which fails closed when any category is protected."""
    if isinstance(channel, discord.Thread):
        parent: object = channel.parent
        if parent is None:
            if not fetch:
                return None, True
            try:
                fetched = await channel.guild.fetch_channel(channel.parent_id)
            except discord.HTTPException:
                return None, True
            if isinstance(fetched, discord.abc.GuildChannel):
                channel.guild._add_channel(fetched)  # pyright: ignore[reportPrivateUsage]  # cache it for the admission that follows
            parent = fetched
        channel = parent
    category_id = getattr(channel, "category_id", None)
    return (str(category_id) if category_id is not None else None), False


async def _channel_protection_state(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, channel: object
) -> ProtectionState:
    """May the agent post for a turn in this channel or thread? Never raises.
    The category (possibly a REST fetch) is looked up only when it matters."""
    if isinstance(channel, discord.Thread):
        channel_id, thread_id = str(channel.parent_id), str(channel.id)
    else:
        channel_id, thread_id = str(getattr(channel, "id", "")), None

    async def _category() -> tuple[str | None, bool]:
        return await _resolve_category(channel)

    return await protection_state(
        sessionmaker,
        tenant_id=tenant_id,
        channel_id=channel_id,
        thread_id=thread_id,
        resolve_category=_category,
    )


DISCORD_REFUSAL_NOUNS = RefusalNouns(scope="server", admin="a server admin", billing="`/billing`")


def admission_refusal_message(
    reason: AdmissionDenialReason, settings: Settings, *, in_dm: bool = False
) -> str:
    """The shared admission refusal in Discord's nouns, naming this deployment's bot."""
    return admission_refusal_text(
        reason,
        DISCORD_REFUSAL_NOUNS,
        bot_name=_resolve_bot_display_name(settings),
        in_dm=in_dm,
    )


async def _resolve_agent_display_name(
    anthropic: _anthropic.AsyncAnthropic, *, tenant_id: uuid.UUID, ma_agent_id: str
) -> str:
    """Best-effort display name for a concrete MA agent id, tenant-scoped.

    Used only for copy -- a lookup miss (agent gone since, API hiccup) falls
    back to a generic phrase rather than failing the render.
    """
    try:
        agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    except _anthropic.APIError:
        return "the previous agent"
    for candidate in agents:
        if candidate.id == ma_agent_id:
            name = candidate.metadata.get(MA_METADATA_KEY_NAME)
            if name:
                return str(name)
    return "the previous agent"


def _compose_queued_content(messages: list[discord.Message]) -> str:
    """Compose pending mention contents into a single composite user message.

    Single-author: contents joined by blank lines so the model sees them as
    one continuing thought from the same speaker. Multi-author: each prefixed
    with ``[display_name]: `` so the agent can attribute who said what.
    """
    if not messages:
        return ""
    author_ids = {m.author.id for m in messages}
    if len(author_ids) == 1:
        return "\n\n".join(m.content for m in messages)
    return "\n\n".join(f"[{m.author.display_name}]: {m.content}" for m in messages)


def _log_bg_task_exception(task: asyncio.Task[None]) -> None:
    """Done-callback: surface escaped background-task exceptions immediately
    instead of asyncio's GC-time 'Task exception was never retrieved'."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("bg_task_failed", task_name=task.get_name(), exc_info=exc)


def _build_welcome_embed(bot_display_name: str) -> discord.Embed:
    """Immediate "⏳ setting up…" welcome. Pure — no I/O."""
    embed = discord.Embed(
        title="⏳ Setting up…",
        description=(
            "Setting up this server — seeding your default agent, environment, and "
            "skills. This takes a few moments."
        ),
        color=_EMBED_COLOR,
    )
    embed.add_field(
        name="Once ready",
        value=(
            f"Mention `@{bot_display_name}` anywhere to chat, or run `/agent-setup` "
            "to see who answers here."
        ),
        inline=False,
    )
    return embed


def _build_ready_embed(*, promo_codes: bool = False) -> discord.Embed:
    """Terminal success follow-up. Pure — no I/O.

    ``promo_codes``: some code is redeemable now, so point admins at /billing.
    """
    description = "Mention me anywhere, or run `/agent-setup`."
    if promo_codes:
        description += "\nHave a promo code? Admins can redeem it in `/billing`."
    return discord.Embed(title="✅ Ready", description=description, color=theme.COLOR_GREEN)


def _build_snag_embed() -> discord.Embed:
    """Terminal non-success follow-up. NEVER the word "failed". Pure — no I/O."""
    return discord.Embed(
        title="⚠️ Setup hit a snag",
        description="Setup hit a snag — still working on it. Mention me to nudge it along.",
        color=_EMBED_COLOR,
    )


def _pick_post_channel(guild: discord.Guild) -> discord.abc.Messageable | None:
    """Channel fallback: system_channel (writable) → first sendable text channel.

    guild.me is None-guarded FIRST (pyright-strict completeness gate). Returns None when
    no in-guild channel is writable; the DM-owner step is handled by the async caller.
    """
    # guild.me is typed Member by discord.py's stub, but the gateway can transiently
    # return None before the member cache is populated — guard it at runtime.
    me = guild.me
    if me is None:  # pyright: ignore[reportUnnecessaryComparison]  # stub claims non-Optional; runtime disagrees
        return None
    ch = guild.system_channel
    if ch is not None and ch.permissions_for(me).send_messages:
        return ch
    return next((c for c in guild.text_channels if c.permissions_for(me).send_messages), None)


@dataclass
class _ParticipationBatch:
    """Unmentioned messages piling up in one followed thread, and the timer watching them.

    The timer restarts on every new message, so the batch is only judged once
    the thread has gone quiet: a rapid human back-and-forth costs one
    classifier call at the end, not one per message.
    """

    messages: list[discord.Message]
    first_at: float
    timer: asyncio.Task[None] | None = None


# Shared with every adapter that follows threads (see `daimon.core.participation_gates`).
_PARTICIPATION_BATCH_MAX_MESSAGES: Final[int] = BATCH_MAX_MESSAGES
_PARTICIPATION_BATCH_MAX_QUIET_PERIODS: Final[int] = BATCH_MAX_QUIET_PERIODS


async def _requester_role(guild: discord.Guild, external_user_id: str) -> tuple[Role, list[str]]:
    """The requester's live guild role and role ids, for a turn with no message to read.

    Same test a mention applies (`is_member_guild_admin`), against the member
    fetched from Discord now. Never the member cache: a cached member can
    still carry a role the requester has since lost, and a continuation can
    run long after the form was submitted. Anything short of a fetched member
    -- left the guild, a Discord error, a malformed id -- is USER with no
    roles: a continuation never runs with more than its requester provably holds.
    """
    try:
        member = await guild.fetch_member(int(external_user_id))
    except (discord.HTTPException, ValueError) as exc:
        log.warning("continuation.requester_role_lookup_failed", exc_info=exc)
        return Role.USER, []
    is_admin = is_member_guild_admin(member, guild_owner_id=guild.owner_id)
    return (Role.ADMIN if is_admin else Role.USER), member_role_ids(member)


class DaimonBot(commands.Bot):
    """Discord bot process. Slash commands + turn pipeline."""

    def __init__(self, *, runtime: DiscordRuntime, intents: discord.Intents) -> None:
        super().__init__(command_prefix=[], intents=intents)  # type: ignore[arg-type]  # discord.py expects Iterable but [] is valid
        self.runtime = with_budget_notifier(
            runtime, self.open_member_dm, self.is_closed, client=self
        )
        # Per-thread concurrency state. _processing: thread IDs with an active turn.
        # _pending: mentions queued behind an in-flight turn for that thread.
        # Drained after the current turn finishes into a single composite follow-up
        # turn so the user doesn't lose messages they fired while the bot was busy.
        self._processing: set[int] = set()
        self._pending: dict[int, list[discord.Message]] = {}
        # Continuation dispatches skipped because the thread was processing,
        # keyed by thread id: re-run when the thread is released (see
        # `_release_thread`). Last writer wins; a dispatch reads every pending
        # row for the thread, so one entry is enough.
        self._deferred_dispatch: dict[int, tuple[uuid.UUID, discord.Thread, str]] = {}
        # Per-tenant concurrency cap (SCALE-01): active turn count keyed by tenant_id.
        # Incremented before the turn starts; decremented in a finally that brackets
        # the whole drain loop so the slot is always released.
        self._inflight: dict[uuid.UUID, int] = {}
        self._global_inflight = 0
        # Organic thread participation: per-thread quiet-period batches, keyed
        # by thread id. Populated only for threads that resolved to `on`.
        self._participation_pending: dict[int, _ParticipationBatch] = {}
        # Built on first use, not here: it needs `self.user`, which the gateway
        # only supplies once the bot is ready.
        self._participant: ThreadParticipant | None = None
        # In-flight seed guard: tenant_ids with a reconcile in progress.
        self._seeding: set[uuid.UUID] = set()
        self._seed_sem = asyncio.Semaphore(_SWEEP_CONCURRENCY)
        # Gateway lifecycle callbacks run as separate tasks. Serialize only the
        # tenant provision/archive transitions so an earlier remove cannot
        # overwrite a later join's archive clear.
        self._guild_lifecycle_locks: dict[int, asyncio.Lock] = {}
        # Track spawned background tasks so they aren't GC'd; discard on done.
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._output_sweeps: dict[str, asyncio.Task[None]] = {}
        self._delivery_notice_thread_ids: set[int] = set()
        # Drain flag — set by _drain_and_close on SIGTERM/SIGINT.
        # While True, on_message rejects new mentions; existing turns finish.
        self.draining: bool = False
        # One-shot guard for the orphaned-turn sweep. on_ready re-fires on every
        # full gateway reconnect, and a second run would reap the turns THIS
        # process is currently rendering.
        self._orphans_retired: bool = False
        self._orphan_sweep_lock = asyncio.Lock()
        self._boot_turn_card_intents: list[TurnCardIntentRow] | None = None
        self._turn_card_recovery_started: bool = False
        # Set by setup_hook, which runs after login and before the gateway
        # connects, so it is set before any message or interaction can arrive.
        # From then on every turn entry passes the sweep barrier. Left unset
        # only by unit tests that build a bot without logging it in.
        self._orphan_recovery_armed: bool = False
        # Set by the process entrypoint, so the wake poller runs in the
        # deployed bot (started from setup_hook, once login has completed)
        # and never in a test that drives setup_hook directly.
        self.wake_poller_enabled: bool = False

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Fire-and-forget a background task, tracked so it isn't GC'd."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(_log_bg_task_exception)
        return task

    def _forget_output_sweep(self, session_id: str, task: asyncio.Task[None]) -> None:
        if self._output_sweeps.get(session_id) is task:
            del self._output_sweeps[session_id]

    async def _sweep_session_outputs(
        self,
        previous: asyncio.Task[None] | None,
        thread: discord.Thread,
        tenant_id: uuid.UUID,
        session_id: str,
    ) -> None:
        # A previous sweep owns post-then-delete for this MA session until it finishes.
        if previous is not None:
            with contextlib.suppress(Exception):
                await previous
        try:
            await deliver_session_outputs(
                self.runtime.turn_deps.anthropic,
                thread,
                session_id=session_id,
                may_post=lambda: self._may_post_in(tenant_id=tenant_id, channel=thread),
                notice_thread_ids=self._delivery_notice_thread_ids,
            )
        except Exception as exc:  # detached sweep must not fail the completed turn
            log.warning(
                "discord.output_delivery.unhandled_error",
                session_id=session_id,
                thread_id=thread.id,
                error=str(exc)[:300],
            )

    async def _archive_requested(self, origin_id: uuid.UUID) -> bool:
        """Whether the agent asked, during this turn, to archive its own thread.

        Read when the run has ended; a failed read leaves the thread open
        rather than failing a turn that already answered.
        """
        try:
            async with self.runtime.sessionmaker() as session:
                return await thread_archive_requested(session, origin_id=origin_id)
        except Exception as exc:
            log.warning("turn.archive_request_read_failed", error_type=type(exc).__name__)
            return False

    async def _archive_after_outputs(self, outcome: RunOutcome, thread: discord.Thread) -> None:
        """Archive the turn's thread once nothing more will be posted for the turn.

        Runs after the turn's last edit and reaction. A session-output sweep may
        still post files (a post reopens a thread), so the archive waits for it,
        and by then a newer turn may own the thread: `_archive_when_idle`.
        """
        sweep = self._output_sweeps.get(outcome.ma_session_id)
        if sweep is None:
            await archive_thread_quietly(thread)
            return

        async def after_sweep() -> None:
            with contextlib.suppress(Exception):
                await sweep
            await self._archive_when_idle(thread)

        self._spawn(after_sweep())

    async def _archive_when_idle(self, thread: discord.Thread) -> None:
        """Archive unless a newer turn has the thread; hold the thread meanwhile.

        A newer turn in flight or queued wins: archiving under it would fail its
        card edits, the bug the deferral exists to avoid, and the agent can be
        asked again. The check and the claim have no await between them, so a
        mention arriving during the archive queues; it is handed back to
        `on_message` afterwards, which gates and counts it like any mention.
        """
        if (
            thread.id in self._processing
            or self._pending.get(thread.id)
            or thread.id in self._deferred_dispatch
        ):
            log.info("turn.thread_archive_skipped_busy", thread_id=thread.id)
            return
        self._processing.add(thread.id)
        try:
            await archive_thread_quietly(thread)
        finally:
            # Resumes a continuation deferred behind the claim, like any release.
            self._release_thread(thread.id)
            for queued in self._pending.pop(thread.id, []):
                self._spawn(self.on_message(queued))

    def _schedule_output_sweep(
        self, outcome: RunOutcome, *, thread: discord.Thread, tenant_id: uuid.UUID
    ) -> None:
        if not any(isinstance(block, ToolUseBlock) for block in outcome.state.content):
            return
        session_id = outcome.ma_session_id
        previous = self._output_sweeps.get(session_id)
        task = self._spawn(self._sweep_session_outputs(previous, thread, tenant_id, session_id))
        self._output_sweeps[session_id] = task
        task.add_done_callback(functools.partial(self._forget_output_sweep, session_id))

    async def _drain_and_close(self) -> None:
        """Graceful shutdown drain.

        Flips draining=True so on_message rejects new mentions, then polls the
        existing _processing set until it empties or the grace window elapses.
        Any cut turn surfaces as a retryable error (acceptable). Waits for
        pending budget notices, then calls bot.close() unconditionally so the
        gateway disconnects cleanly.
        """
        self.draining = True
        # Pending auto batches are unasked-for turns that have not started;
        # dropping them is free, whereas letting a timer fire mid-drain would
        # admit a new turn the drain is trying to stop.
        for thread_id in list(self._participation_pending):
            self._cancel_participation_batch(thread_id)
        log.info("discord.draining", inflight_threads=len(self._processing))
        deadline = asyncio.get_running_loop().time() + _DRAIN_GRACE_S
        while (
            self._processing or self._global_inflight
        ) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        log.info("discord.drain_complete", remaining=len(self._processing))
        # Before close(): it closes the HTTP session a notice still DMs through.
        await drain_budget_notices()
        await self.close()

    async def setup_hook(self) -> None:
        """Arm orphan recovery and load command Cogs before on_ready syncs the tree."""
        self.start_orphan_recovery()
        if self.wake_poller_enabled:
            self._spawn(
                run_wake_poller(
                    self.runtime.sessionmaker,
                    platform="discord",
                    open_thread=self._open_wake_thread,
                    should_stop=lambda: self.draining or self.is_closed(),
                )
            )
            # FEAT-085: post routine results to their destinations. Same
            # switch as the wake poller: one process per platform posts.
            self._spawn(
                run_delivery_poller(
                    self.runtime.sessionmaker,
                    platform="discord",
                    post=make_discord_routine_poster(
                        self.runtime.sessionmaker,
                        fetch_channel=self._channel_by_id,
                        open_dm=self.open_member_dm,
                        dm_policy=lambda row: self.runtime.settings.direct_message_policies.get(
                            row.tenant_id, DirectMessagePolicy()
                        ),
                    ),
                    should_stop=lambda: self.draining or self.is_closed(),
                )
            )
        from daimon.adapters.discord.commands.agent_setup import AgentSetupCog
        from daimon.adapters.discord.commands.billing import BillingCog
        from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
        from daimon.adapters.discord.commands.help import HelpCog
        from daimon.adapters.discord.commands.here import HereCog
        from daimon.adapters.discord.commands.memory import MemoryCog
        from daimon.adapters.discord.commands.privacy import PrivacyCog
        from daimon.adapters.discord.commands.routines import RoutinesCog
        from daimon.adapters.discord.feedback_reactions import FeedbackReactionCog

        await self.add_cog(HelpCog(self))
        await self.add_cog(HereCog(self))
        await self.add_cog(DirectMessageCog(self))
        await self.add_cog(AgentSetupCog(self))
        await self.add_cog(RoutinesCog(self))
        await self.add_cog(BillingCog(self))
        await self.add_cog(PrivacyCog(self))
        await self.add_cog(MemoryCog(self))
        await self.add_cog(FeedbackReactionCog(self))

        # One-time CLASS registration (not per-button) for the chat-initiated
        # credential-request button. Imported here, not at module level, since
        # credential_button.py imports DaimonBot at module level -- importing
        # it up top here would be a cycle. Needed because a button posted by
        # the separate MCP process still has to dispatch in THIS process:
        # Discord routes interactions by bot application id, not by which
        # process sent the message that carried the button.
        from daimon.adapters.discord.credential_button import CredentialRequestButton

        self.add_dynamic_items(CredentialRequestButton)

        # Same rationale as CredentialRequestButton above: the wizard's
        # first screen is posted by the MCP process, and every tap on it
        # lands here. wizard.py imports DaimonBot at module level -- local
        # import avoids the same cycle credential_button.py would hit.
        from daimon.adapters.discord.wizard import WizardNavButton, WizardSelect

        self.add_dynamic_items(WizardNavButton, WizardSelect)

        # The Submit button gets its own dispatch class (claiming the
        # submission and starting a billed turn) rather than sharing
        # WizardNavButton/WizardSelect's registration -- see
        # wizard_submit.py's module docstring. Same local-import rationale.
        from daimon.adapters.discord.wizard_submit import WizardSubmitButton

        self.add_dynamic_items(WizardSubmitButton)

        # A feedback button is delivered into a direct message and must
        # still dispatch after this process restarts, so it needs the same
        # class-level registration rather than a live view. Local import for
        # the same cycle reason as above.
        from daimon.adapters.discord.feedback_button import FeedbackButton
        from daimon.adapters.discord.support_escalation import SupportEscalateButton

        self.add_dynamic_items(FeedbackButton)
        self.add_dynamic_items(SupportEscalateButton)

        # The Hand over button rides a notice posted by an earlier process
        # too, so it is a class-level registration as well.
        from daimon.adapters.discord.thread_handoff import HandOverButton

        self.add_dynamic_items(HandOverButton)

    async def _post_to_guild(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """Post an embed via the fallback chain: text channel → DM owner → skip."""
        channel = _pick_post_channel(guild)
        if channel is not None:
            try:
                await channel.send(embed=embed)
                return
            except discord.HTTPException as exc:
                log.warning("guild_post_channel_failed", guild_id=str(guild.id), error=str(exc))
        # DM-owner fallback.
        owner = guild.owner
        if owner is None and guild.owner_id is not None:
            try:
                owner = await guild.fetch_member(guild.owner_id)
            except discord.HTTPException:
                owner = None
        if owner is not None:
            try:
                await owner.send(embed=embed)
                return
            except discord.HTTPException as exc:
                log.warning("guild_post_dm_failed", guild_id=str(guild.id), error=str(exc))
        log.warning("guild_post_skipped", guild_id=str(guild.id))

    async def _flip_failed_best_effort(
        self, tenant_id: uuid.UUID, *, reason: str | None, was_ready: bool
    ) -> None:
        """Best-effort pending/failed→failed flip. A DB hiccup can make the flip
        itself raise; swallowing it here keeps the snag embed posting and the
        seed handler alive. The on_ready sweep is the designed backstop if the
        flip is lost.

        A tenant that was already `ready` keeps that status -- an exception
        raised out of the reconcile is the same "transient failure" case
        `_seed_tenant_defaults`'s FAILED-outcome branch guards, just reached via
        the exception path instead. Only the failure reason is recorded."""
        try:
            if was_ready:
                await set_provision_status(
                    self.runtime.sessionmaker, tenant_id=tenant_id, reason=reason
                )
            else:
                await set_provision_status(
                    self.runtime.sessionmaker, tenant_id=tenant_id, status="failed", reason=reason
                )
        except SQLAlchemyError:
            log.exception("guild_seed_status_flip_failed", tenant_id=str(tenant_id))

    async def _seed_tenant_defaults(
        self, *, tenant_id: uuid.UUID, guild: discord.Guild, was_ready: bool
    ) -> None:
        """Background MA seed. Owns the pending/failed→ready/failed status flip.
        Posts the ✅/⚠️ follow-up on terminal state ONLY when the tenant was not
        already ready -- a fresh install or one recovering from `failed`. An
        already-ready tenant gets neither embed, no matter what the reconcile
        changed or how it failed: every deploy that edits `defaults/`
        reconciles every guild, and announcing that in each guild's channel is
        noise nobody asked for. The embeds answer "am I installed?", which only
        changes on install. That gate covers the raising paths too -- a single
        provider error during a boot sweep would otherwise put a snag embed in
        every channel at once. In-flight guard prevents duplicate seeds.

        `was_ready`: the tenant's provision_status immediately before this call,
        passed explicitly by the caller (which already has the row) rather than
        re-read here -- a first-run or recovering install (was_ready=False)
        always gets its confirmation regardless of what the reconcile reports.
        A tenant that WAS ready is never demoted by a failed reconcile here: a
        transient provider failure during the boot sweep must not take a
        working guild's turns offline, so only the failure reason is recorded
        and the tenant stays `ready`.
        """
        if tenant_id in self._seeding:
            return
        self._seeding.add(tenant_id)
        # Without public_url, reconcile's daimon-mcp merge is a no-op and the
        # seeded agent gets none of the MCP tools its system prompt advertises.
        public_url = (
            str(self.runtime.settings.mcp.public_url)
            if self.runtime.settings.mcp.public_url is not None
            else None
        )
        try:
            async with self._seed_sem:
                report = await reconcile_tenant_defaults(
                    self.runtime.anthropic,
                    self.runtime.sessionmaker,
                    self.runtime.settings.defaults_root,
                    tenant_id=tenant_id,
                    public_url=public_url,
                )
            seed_ok = not report.is_failure()
            roster_failure_reason: str | None = None
            if seed_ok:
                agent_name = self.runtime.deployment_default.agent_name
                if agent_name is None:
                    log.info("guild_seed_roster_check_skipped", tenant_id=str(tenant_id))
                else:
                    default_agent = await find_agent_by_daimon_tag(
                        self.runtime.anthropic, tenant_id=tenant_id, name=agent_name
                    )
                    if default_agent is None:
                        seed_ok = False
                        roster_failure_reason = (
                            f"agent {agent_name!r}: default agent missing from roster "
                            "after reconcile"
                        )
                        log.warning(
                            "guild_seed_default_agent_missing",
                            tenant_id=str(tenant_id),
                            agent_name=agent_name,
                        )
            if seed_ok:
                await set_provision_status(
                    self.runtime.sessionmaker,
                    tenant_id=tenant_id,
                    status="ready",
                    clear_reason=True,
                )
                if not was_ready:
                    try:
                        async with self.runtime.sessionmaker() as session:
                            promo_codes = await has_redeemable_promo_code(
                                session, now=datetime.now(UTC)
                            )
                    except SQLAlchemyError as exc:
                        # The guild is ready; a failed lookup only drops the promo line.
                        log.warning(
                            "guild_seed_promo_lookup_failed",
                            tenant_id=str(tenant_id),
                            error=str(exc),
                        )
                        promo_codes = False
                    await self._post_to_guild(guild, _build_ready_embed(promo_codes=promo_codes))
            else:
                reason = roster_failure_reason or compose_failure_reason(report)
                if was_ready:
                    # A previously-ready install stays ready: a transient reconcile
                    # failure must not take a working guild's turns offline. Record
                    # the reason so it's visible to an operator, but don't flip the
                    # gate `on_message` checks.
                    await set_provision_status(
                        self.runtime.sessionmaker, tenant_id=tenant_id, reason=reason
                    )
                    log.warning(
                        "guild_reconcile_failed_ready_tenant",
                        tenant_id=str(tenant_id),
                        reason=reason,
                    )
                else:
                    await set_provision_status(
                        self.runtime.sessionmaker,
                        tenant_id=tenant_id,
                        status="failed",
                        reason=reason,
                    )
                    await self._post_to_guild(guild, _build_snag_embed())
        except (DaimonError, _anthropic.APIError, discord.HTTPException) as exc:
            log.warning("guild_seed_failed", tenant_id=str(tenant_id), error=str(exc))
            # Best-effort flip before posting so the tenant is never left wedged in
            # 'pending' — a raise inside this handler would NOT be caught by the
            # sibling except clause below and would skip the snag embed entirely.
            await self._flip_failed_best_effort(
                tenant_id, reason=f"{type(exc).__name__}: {exc}", was_ready=was_ready
            )
            if not was_ready:
                await self._post_to_guild(guild, _build_snag_embed())
        except Exception as exc:  # background-task supervisor boundary
            log.exception("guild_seed_unexpected", tenant_id=str(tenant_id))
            # This branch's message body may carry anything (an unclassified bug,
            # not a known API/Daimon error), and the reason column is read by an
            # operator and an alerting pass -- record only the type name, never
            # the message, so an unexpected exception can't smuggle request/response
            # content into the persisted reason.
            await self._flip_failed_best_effort(
                tenant_id, reason=f"unexpected error: {type(exc).__name__}", was_ready=was_ready
            )
            if not was_ready:
                await self._post_to_guild(guild, _build_snag_embed())
        finally:
            self._seeding.discard(tenant_id)

    def _guild_lifecycle_lock_for(self, guild_id: int) -> asyncio.Lock:
        """Return the process-local lifecycle lock for one Discord guild."""
        lock = self._guild_lifecycle_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_lifecycle_locks[guild_id] = lock
        return lock

    async def _provision_joined_guild(self, guild: discord.Guild) -> uuid.UUID | None:
        """Provision and unarchive a guild that is still present in the cache."""
        guild_id = str(guild.id)
        async with self._guild_lifecycle_lock_for(guild.id):
            # Boot sweeps and hot-path recovery may carry a snapshot from before
            # a remove callback updated the gateway cache. Do not revive it.
            if self.get_guild(guild.id) is None:
                return None
            result = await provision_tenant(
                self.runtime.sessionmaker,
                platform="discord",
                workspace_id=guild_id,
                signup_credit=self.runtime.settings.billing.signup_credit,
            )
            await set_provision_status(
                self.runtime.sessionmaker,
                tenant_id=result.tenant_id,
                status="pending",
                clear_archive=True,
            )
        return result.tenant_id

    async def _ensure_provisioning(self, guild: discord.Guild) -> None:
        """Self-heal an unprovisioned/archived guild: provision + un-archive + bg seed."""
        tenant_id = await self._provision_joined_guild(guild)
        if tenant_id is None:
            return
        self._spawn(self._seed_tenant_defaults(tenant_id=tenant_id, guild=guild, was_ready=False))

    def start_orphan_recovery(self) -> None:
        """Start the boot sweep before the gateway can deliver a turn.

        Mirrors Slack's `start_orphan_recovery`, which runs before Socket Mode
        connects. discord.py dispatches messages before `on_ready` (which
        waits for every guild to stream in), so a barrier gated on
        `is_ready()` let a turn start and write its marker before the first
        sweep, and that sweep then retired -- and interrupted on MA -- the
        live turn. Arming here makes every turn entry wait for the sweep from
        the first event on. The sweep needs only REST calls (`fetch_channel`),
        which work once login has completed.
        """
        self._orphan_recovery_armed = True
        self._spawn(self._retire_orphaned_turns())

    async def _wait_for_orphan_recovery(self) -> None:
        """The turn-admission barrier: returns once the boot sweep has run.

        A sweep that failed is retried here, so a transient failure delays
        turns rather than letting a later sweep mistake their markers for
        orphans.
        """
        if self._orphan_recovery_armed:
            await self._retire_orphaned_turns()

    async def _retire_orphaned_turns(self) -> None:
        async with self._orphan_sweep_lock:
            await self._retire_orphaned_turns_once()

    async def _retire_orphaned_turns_once(self) -> None:
        """Lay to rest every embed whose turn died with the previous process.

        A turn's render loop lives in the process that started it, so a deploy
        mid-turn freezes the embed on 'thinking' forever while MA completes and
        bills the answer server-side. The user sees a spinner that never stops
        and has no way to tell it is dead.

        Marking it failed is honest and cheap, and the alternative -- draining
        in-flight turns before the container exits -- needs a lameduck story the
        compose refresh does not have. It starts from setup_hook, before the
        gateway connects, and every turn waits for it, so a user reading the
        thread sees the truth before anything else happens.

        Failures to edit are swallowed per row: the message may be deleted, the
        thread archived, or permissions changed since. One unreachable embed
        must not stop the sweep clearing the rest, and the row is cleared either
        way so a permanently unreachable message is not retried on every boot.

        Runs at most once per process: on_ready re-fires on every full gateway
        reconnect, and a marker set by this process is a LIVE turn, not an
        orphan. That only holds because no turn can write a marker before the
        first run finishes (`_wait_for_orphan_recovery`); the MA interrupt
        below would otherwise stop a live turn of this very process.
        """
        if self._orphans_retired:
            return
        async with self.runtime.sessionmaker() as session:
            if self._boot_turn_card_intents is None:
                self._boot_turn_card_intents = await list_recoverable_turn_card_intents(
                    session, platform="discord"
                )
            orphans = await list_orphaned_turns(session, platform="discord")
        if not orphans:
            self._orphans_retired = True
            self._start_turn_card_recovery()
            return
        log.info("turn.orphans_found", count=len(orphans))

        for row in orphans:
            if row.active_turn_message_id is None:  # pragma: no cover - filtered by the query
                continue
            try:
                channel = self.get_channel(int(row.thread_id)) or await self.fetch_channel(
                    int(row.thread_id)
                )
                if isinstance(channel, discord.abc.Messageable):
                    message = await channel.fetch_message(int(row.active_turn_message_id))
                    transport = DiscordPostTransport(
                        self, channel, name="Daimon", avatar_url=None, builtin=False
                    )
                    embed = discord.Embed(
                        color=theme.COLOR_RED,
                        title="Stopped: Daimon restarted.",
                        description="Mention me to try again.",
                    )
                    if transport._destination() is not None:  # pyright: ignore[reportPrivateUsage]
                        await transport.edit(message, embed=embed, view=None)
                    elif not isinstance(message.webhook_id, int):
                        await message.edit(embed=embed, view=None)
                    log.info(
                        "turn.orphan_retired",
                        thread_id=row.thread_id,
                        message_id=row.active_turn_message_id,
                        # How long the user stared at a spinner. The only place
                        # this is visible -- the turn's own logs died with its
                        # container.
                        frozen_for_s=(
                            (datetime.now(UTC) - row.active_turn_started_at).total_seconds()
                            if row.active_turn_started_at is not None
                            else None
                        ),
                    )
            except (discord.HTTPException, discord.ClientException, ValueError) as err:
                log.warning(
                    "turn.orphan_retire_failed",
                    thread_id=row.thread_id,
                    message_id=row.active_turn_message_id,
                    error=str(err),
                )
            cleared = await recover_orphan_marker(
                self.runtime.sessionmaker,
                row,
                clear=clear_active_turn_if_message_id,
                interrupt=partial(interrupt_orphaned_session, self.runtime.anthropic),
            )
            if not cleared:
                log.info(
                    "turn.orphan_marker_moved",
                    thread_id=row.thread_id,
                    message_id=row.active_turn_message_id,
                )
        self._orphans_retired = True
        self._start_turn_card_recovery()

    def _start_turn_card_recovery(self) -> None:
        """Reconcile the pre-admission intent snapshot after gateway readiness."""
        if (
            not self._orphan_recovery_armed
            or self._turn_card_recovery_started
            or self._boot_turn_card_intents is None
        ):
            return
        self._turn_card_recovery_started = True
        self._spawn(self._reconcile_boot_turn_cards(self._boot_turn_card_intents))

    async def _reconcile_boot_turn_cards(self, intents: list[TurnCardIntentRow]) -> None:
        """Run a fixed number of workers over the startup intent snapshot."""
        if not intents:
            return
        await self.wait_until_ready()
        intent_iter = iter(intents)

        async def worker() -> None:
            for intent in intent_iter:
                await self._reconcile_turn_card_intent(intent)

        await asyncio.gather(
            *(worker() for _ in range(min(_TURN_CARD_RECOVERY_CONCURRENCY, len(intents))))
        )

    async def _reconcile_turn_card_intent(
        self,
        intent: TurnCardIntentRow,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Recover one intent independently so a delayed search cannot block others."""
        await self.wait_until_ready()
        for attempt in range(3):
            try:
                channel = self.get_channel(int(intent.thread_id)) or await self.fetch_channel(
                    int(intent.thread_id)
                )
                if not isinstance(channel, discord.Thread):
                    log.warning(
                        "turn.card_intent_thread_unavailable",
                        intent_id=str(intent.id),
                        thread_id=intent.thread_id,
                        channel_type=type(channel).__name__,
                    )
                    return
                await reconcile_turn_card_intent(
                    self.runtime.sessionmaker,
                    intent=intent,
                    thread=channel,
                    client=self,
                )
                return
            except (discord.HTTPException, discord.ClientException, ValueError) as err:
                if attempt < 2:
                    await sleep(5.0)
                    continue
                log.warning(
                    "turn.card_intent_thread_fetch_failed",
                    intent_id=str(intent.id),
                    thread_id=intent.thread_id,
                    error=str(err),
                )

    async def on_ready(self) -> None:
        """Forward-only reconcile sweep: provision-if-missing, re-seed pending/failed,
        sync the command tree. NO archive-on-absence."""
        log.info("bot_ready", user=str(self.user))
        await self._retire_orphaned_turns()
        tenants = await list_tenants_by_platform(self.runtime.sessionmaker, platform="discord")
        known_tenants = {tr.external_id: tr for tr in tenants}
        recovered_tenant_ids: set[uuid.UUID] = set()
        # Provision guilds joined while the bot was down. A known archived tenant
        # means the bot left and rejoined while this process was stopped: revive
        # it and reseed without sending a second welcome or signup credit.
        for guild in self.guilds:
            ws_id = str(guild.id)
            known_tenant = known_tenants.get(ws_id)
            if known_tenant is not None and known_tenant.archived_at is None:
                continue
            tenant_id = await self._provision_joined_guild(guild)
            if tenant_id is None:
                continue
            if known_tenant is not None:
                recovered_tenant_ids.add(tenant_id)
                self._spawn(
                    self._seed_tenant_defaults(tenant_id=tenant_id, guild=guild, was_ready=False)
                )
                continue
            await self._post_to_guild(
                guild, _build_welcome_embed(_resolve_bot_display_name(self.runtime.settings))
            )
            self._spawn(
                self._seed_tenant_defaults(tenant_id=tenant_id, guild=guild, was_ready=False)
            )

        # Reconcile every registered, joined tenant against the shipped defaults on
        # every boot, not just the ones stuck in pending/failed. Because every
        # deploy restarts this process, this loop is how a defaults edit (a prompt
        # rewrite, a new skill) reaches an already-provisioned install without any
        # hand-run command. An in-sync tenant costs roughly 13-15 provider read
        # calls here and zero writes -- the reconcile's own per-resource fingerprint
        # gate turns a hash match into a skip -- bounded by the sweep's concurrency
        # cap above. Per-guild permission check + tree sync for joined guilds.
        for tr in tenants:
            guild = self.get_guild(int(tr.external_id))
            if guild is None:
                log.warning("registered_guild_not_joined", external_id=tr.external_id)
                continue
            if tr.id not in recovered_tenant_ids:
                self._spawn(
                    self._seed_tenant_defaults(
                        tenant_id=tr.id, guild=guild, was_ready=tr.provision_status == "ready"
                    )
                )
            missing = check_missing_permissions(guild.me.guild_permissions)
            if missing:
                log.warning(
                    "missing_permissions",
                    guild_id=tr.external_id,
                    guild_name=guild.name,
                    missing=missing,
                )
            else:
                log.info("permissions_ok", guild_id=tr.external_id, guild_name=guild.name)
            try:
                guild_obj = discord.Object(id=int(tr.external_id))
                # Clear any guild-scoped command copies so commands live ONLY at
                # global scope (synced below). A command registered both globally
                # and per-guild renders twice in the guild; this also self-heals
                # guilds that accumulated copies from the old copy_global_to path.
                self.tree.clear_commands(guild=guild_obj)
                await self.tree.sync(guild=guild_obj)
                log.info("tree_synced", guild_id=tr.external_id)
            except discord.HTTPException as exc:
                log.warning("tree_sync_failed", guild_id=tr.external_id, error=str(exc))
        # Global sync so dm_permission=True commands (e.g. /privacy) appear in DMs.
        # DM-capable commands must be registered at the global scope. Note: global
        # sync has propagation latency (up to ~1h); operators triggering UAT in
        # the test guild should wait or use a guild copy if iterating rapidly.
        try:
            await self.tree.sync()
            log.info("tree_synced_global")
        except discord.HTTPException as exc:
            log.warning("tree_sync_global_failed", error=str(exc))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Async two-phase provisioning: provision (pending) → immediate welcome →
        per-guild tree sync → background seed that flips ready/failed + posts the follow-up."""
        guild_id = str(guild.id)
        try:
            tenant_id = await self._provision_joined_guild(guild)
            if tenant_id is None:
                return
            alert_ops(
                self.runtime.settings.ops.alert_webhook_url,
                key=f"install:discord:{guild_id}",
                message=f"New install: Discord {guild.name} ({guild_id})",
            )
            await self._post_to_guild(
                guild, _build_welcome_embed(_resolve_bot_display_name(self.runtime.settings))
            )
            try:
                guild_obj = discord.Object(id=guild.id)
                # Keep the guild command scope empty — global commands already
                # apply to a newly-joined guild immediately. Copying globals into
                # the guild scope would render every command twice.
                self.tree.clear_commands(guild=guild_obj)
                await self.tree.sync(guild=guild_obj)
                log.info("synced_commands_on_join", guild_id=guild_id, guild_name=guild.name)
            except discord.HTTPException as exc:
                log.warning("tree_sync_failed_on_join", guild_id=guild_id, error=str(exc))
        except (DaimonError, _anthropic.APIError, discord.HTTPException) as exc:
            log.warning("guild_join_failed", guild_id=guild_id, error=str(exc))
            return
        # Background seed → flips status + posts the ✅/⚠️ follow-up.
        self._spawn(self._seed_tenant_defaults(tenant_id=tenant_id, guild=guild, was_ready=False))

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Soft-archive: stamp archived_at=now(). NO row delete."""
        guild_id = str(guild.id)
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        async with self._guild_lifecycle_lock_for(guild.id):
            # discord.py updates its guild cache before dispatching gateway
            # callbacks. A delayed removal therefore sees a later rejoin here.
            if self.get_guild(guild.id) is not None:
                log.info("stale_guild_remove_skipped", guild_id=guild_id, guild_name=guild.name)
                return
            await set_provision_status(self.runtime.sessionmaker, tenant_id=tenant_id, archive=True)
        log.warning("guild_removed", guild_id=guild_id, guild_name=guild.name)

    def _release_inflight(self, tenant_id: uuid.UUID) -> None:
        """Release one per-tenant in-flight slot, dropping the key at zero."""
        self._inflight[tenant_id] = self._inflight.get(tenant_id, 1) - 1
        if self._inflight[tenant_id] <= 0:
            self._inflight.pop(tenant_id, None)

    def try_claim_global_turn(self) -> bool:
        """Claim a process-wide slot without yielding between check and increment."""
        settings = self.runtime.settings.discord
        cap = settings.max_concurrent_turns if settings is not None else None
        if self.draining or (isinstance(cap, int) and self._global_inflight >= cap):
            return False
        self._global_inflight += 1
        return True

    def release_global_turn(self) -> None:
        self._global_inflight -= 1

    def _cancel_participation_batch(self, thread_id: int) -> None:
        """Drop a thread's pending auto batch and its timer, if any."""
        batch = self._participation_pending.pop(thread_id, None)
        if batch is not None and batch.timer is not None:
            batch.timer.cancel()

    def _thread_participant(self, *, bot_user_id: int, bot_display_name: str) -> ThreadParticipant:
        """The bot's one participant, built on first use and reused after that."""
        if self._participant is None:
            self._participant = ThreadParticipant(
                settings=self.runtime.settings.thread_participation,
                sessionmaker=self.runtime.sessionmaker,
                anthropic=self.runtime.anthropic,
                bot_user_id=bot_user_id,
                application_id=self.application_id,
                bot_display_name=bot_display_name,
                billing_config=self.runtime.billing_config,
                markup=self.runtime.settings.billing.markup,
                deployment_default=self.runtime.deployment_default,
            )
        return self._participant

    async def _maybe_participate(self, message: discord.Message) -> None:
        """Organic thread participation: an unmentioned human message in a guild thread.

        Reached only after the mention gate said no. Where the mention path
        posts a notice (tenant still provisioning, decision failure), this
        path stays silent: nobody asked, so nothing is owed, and a chatty
        failure mode would be worse than the missing reply.

        Nothing is decided here: the message joins the thread's batch and the
        quiet timer restarts. A thread the cascade resolves to anything but
        `on` costs exactly one indexed read and returns -- no liveness read,
        no classifier, no timer, no turn.
        """
        if self.draining or self.user is None:
            return
        if message.guild is None or not isinstance(message.channel, discord.Thread):
            return
        thread = message.channel
        thread_id = thread.id
        if thread_id in self._processing:
            # The in-flight turn's delta context already carries this message.
            return
        guild_id = str(message.guild.id)
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        discord_settings = self.runtime.settings.discord
        if discord_settings is None:
            return
        responder = self._thread_participant(
            bot_user_id=self.user.id, bot_display_name=discord_settings.bot_display_name
        )
        try:
            # Cascade first: it says `off` for almost every message, so the
            # tenant liveness read is only paid by threads actually followed.
            resolved = await responder.resolve(tenant_id=tenant_id, thread=thread)
            if resolved.mode is not ParticipationMode.ON:
                log.debug(
                    "thread_participation.not_following",
                    thread_id=str(thread_id),
                    mode=resolved.mode.value,
                    tier=resolved.tier,
                )
                return
            tr = await get_tenant_liveness(self.runtime.sessionmaker, tenant_id)
            if tr is None or tr.archived_at is not None or tr.provision_status != "ready":
                return
        except Exception as exc:  # unasked-for turn: log, stay silent
            log.exception("thread_participation.decision_failed", thread_id=str(thread_id))
            sentry_sdk.capture_exception(exc)
            return

        now = asyncio.get_running_loop().time()
        quiet_seconds = self.runtime.settings.thread_participation.quiet_seconds
        batch = self._participation_pending.setdefault(
            thread_id, _ParticipationBatch(messages=[], first_at=now)
        )
        batch.messages.append(message)
        del batch.messages[:-_PARTICIPATION_BATCH_MAX_MESSAGES]
        if batch.timer is not None:
            if now - batch.first_at >= quiet_seconds * _PARTICIPATION_BATCH_MAX_QUIET_PERIODS:
                return  # waited long enough: let the running timer fire as scheduled
            batch.timer.cancel()
        batch.timer = self._spawn(
            self._participation_quiet_timer(
                quiet_seconds, thread, guild_id=guild_id, tenant_id=tenant_id, responder=responder
            )
        )

    async def _participation_quiet_timer(
        self,
        quiet_seconds: float,
        thread: discord.Thread,
        *,
        guild_id: str,
        tenant_id: uuid.UUID,
        responder: ThreadParticipant,
    ) -> None:
        """Wait out the quiet period, then judge the batch. Cancelled = a newer message won."""
        await asyncio.sleep(quiet_seconds)
        await self._participation_fire(
            thread, guild_id=guild_id, tenant_id=tenant_id, responder=responder
        )

    async def _participation_fire(
        self,
        thread: discord.Thread,
        *,
        guild_id: str,
        tenant_id: uuid.UUID,
        responder: ThreadParticipant,
    ) -> None:
        """The thread went quiet: judge the whole batch once, then run at most one turn."""
        thread_id = thread.id
        batch = self._participation_pending.pop(thread_id, None)
        if batch is None or not batch.messages:
            return
        if self.draining or thread_id in self._processing:
            return
        discord_settings = self.runtime.settings.discord
        if discord_settings is None:
            return
        # One turn = one caller (see _drain_pending_mentions): the newest
        # message's author is the caller, and only their messages are judged.
        # Coalescing other authors' text onto this caller's session would
        # reopen the confused-deputy hole the mention path closed.
        trigger = batch.messages[-1]
        candidates = [m for m in batch.messages if m.author.id == trigger.author.id]
        try:
            # Re-resolved rather than carried from the batch: the quiet window
            # is long enough for someone to turn the thread off mid-burst.
            resolved = await responder.resolve(tenant_id=tenant_id, thread=thread)
            if not await responder.should_respond(
                thread, candidates, trigger=trigger, tenant_id=tenant_id, resolved=resolved
            ):
                return
        except Exception as exc:  # unasked-for turn: log, stay silent
            log.exception("thread_participation.decision_failed", thread_id=str(thread_id))
            sentry_sdk.capture_exception(exc)
            return
        if self.draining or thread_id in self._processing:
            return  # a drain or a mention landed while the classifier was deciding
        post_state = await _channel_protection_state(
            self.runtime.sessionmaker, tenant_id=tenant_id, channel=thread
        )
        if not post_state.may_post:
            log.info(
                "thread_participation.skipped",
                reason="writers_none",
                state=post_state.value,
                thread_id=str(thread_id),
            )
            return
        cap = await get_turn_cap(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            default=discord_settings.max_concurrent_turns_per_tenant,
        )
        if self.draining or thread_id in self._processing:
            return  # protection and cap reads both awaited
        count = self._inflight.get(tenant_id, 0)
        if not should_admit_turn(current_in_flight=count, cap=cap):
            record_refusal(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="discord",
                channel_id=str(thread_id),
                thread_id=str(thread_id),
            )
            log.info(
                "turn.skipped.concurrency_shed",
                tenant_id=str(tenant_id),
                guild_id=guild_id,
                channel_id=str(thread_id),
                in_flight=count,
                cap=cap,
            )
            return
        if not self.try_claim_global_turn():
            log.info(
                "turn.skipped.global_concurrency_shed",
                tenant_id=str(tenant_id),
                guild_id=guild_id,
                channel_id=str(thread_id),
            )
            return  # unprompted participation follows the per-tenant silent refusal
        self._inflight[tenant_id] = count + 1
        self._processing.add(thread_id)
        try:
            # The ledger row is written when the turn is admitted, not when it
            # answers: spend starts here, and a turn the agent ends in silence
            # (or one that fails) must still count against the hourly cap.
            try:
                await responder.record(
                    tenant_id=tenant_id, thread_id=thread_id, message_id=str(trigger.id)
                )
            except Exception:  # best-effort ledger: a miss loosens the cap by one
                log.exception("thread_participation.record_failed", thread_id=str(thread_id))
            # The newest message is the trigger; the delta context carries the
            # rest of the batch, since they all landed after the watermark.
            await self._handle_mention(trigger, guild_id, tenant_id, unprompted=True)
            await self._drain_pending_mentions(thread_id, guild_id, tenant_id)
        finally:
            self._release_thread(thread_id)
            self._pending.pop(thread_id, None)
            self._release_inflight(tenant_id)
            self.release_global_turn()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Remember the clicker's name for /billing; the command tree handles the rest."""
        remember_guild_user(
            self.runtime.sessionmaker, guild_id=interaction.guild_id, user=interaction.user
        )

    async def on_message(self, message: discord.Message) -> None:
        """Gate on mention, resolve TenantContext once + run the non-ready self-heal gate,
        then orchestrate a turn in a thread."""
        discord_settings = self.runtime.settings.discord
        # Explicit @-mention only. `mentioned_in` returns True for @everyone/@here
        # (it short-circuits on message.mention_everyone), which would make the bot
        # reply to every mass ping. message.mentions excludes @everyone/@here and
        # role mentions, so this triggers only on a direct user mention of the bot.
        bot_mentioned = self.user is not None and any(
            user.id == self.user.id for user in message.mentions
        )
        if self.draining:
            return
        is_webhook_post = isinstance(message.webhook_id, int)
        reply_to_recorded_post = False
        reference = message.reference
        resolved = reference.resolved if isinstance(reference, discord.MessageReference) else None
        resolved_is_ours = isinstance(resolved, discord.Message) and (
            (self.user is not None and resolved.author.id == self.user.id)
            or (
                isinstance(resolved.webhook_id, int)
                and (
                    (
                        self.application_id is not None
                        and resolved.application_id == self.application_id
                    )
                    or resolved.webhook_id in known_webhook_ids()
                )
            )
        )
        if (
            self.runtime.settings.agent_identity.enabled
            and not bot_mentioned
            and isinstance(reference, discord.MessageReference)
            and reference.type is discord.MessageReferenceType.reply
            and reference.message_id is not None
            and resolved_is_ours
            and message.guild is not None
            and (
                not message.author.bot
                or (
                    discord_settings is not None
                    and str(message.author.id) in discord_settings.qa_bot_user_ids
                )
            )
            and not is_webhook_post
        ):
            reply_tenant = derive_tenant_uuid(
                platform="discord", workspace_id=str(message.guild.id)
            )
            try:
                async with self.runtime.sessionmaker() as session:
                    post = await get_post(
                        session,
                        tenant_id=reply_tenant,
                        platform="discord",
                        channel_id=str(message.channel.id),
                        message_id=str(reference.message_id),
                    )
                reply_to_recorded_post = post is not None and post.source != "auto_thread"
            except Exception as exc:
                log.warning("reply_gate.lookup_failed", error_type=type(exc).__name__)
        if not should_process_message(
            author_is_bot=message.author.bot,
            author_id=str(message.author.id),
            bot_mentioned=bot_mentioned,
            reply_to_recorded_post=reply_to_recorded_post,
            identity_enabled=self.runtime.settings.agent_identity.enabled,
            author_is_webhook=is_webhook_post,
            guild_id=str(message.guild.id) if message.guild else None,
            self_user_id=str(self.user.id) if self.user is not None else None,
            qa_bot_user_ids=discord_settings.qa_bot_user_ids if discord_settings else (),
        ):
            # Not a mention. The only other way a message starts a turn is
            # organic thread participation, which has its own gates.
            if is_participation_candidate(
                # No Discord settings block means no bot to follow anything.
                deployment_mode=(
                    self.runtime.settings.thread_participation.mode
                    if discord_settings is not None
                    else ParticipationMode.DISABLED
                ),
                author_is_bot=message.author.bot,
                bot_mentioned=bot_mentioned,
                in_thread=isinstance(message.channel, discord.Thread),
                guild_id=str(message.guild.id) if message.guild else None,
            ):
                await self._maybe_participate(message)
            return
        assert message.guild is not None
        guild = message.guild
        guild_id = str(guild.id)
        bot_display_name = _resolve_bot_display_name(self.runtime.settings)
        # For /billing's top spenders; in the background, never failing the turn.
        remember_guild_user(self.runtime.sessionmaker, guild_id=guild.id, user=message.author)

        # --- Unified non-ready self-heal gate through turn completion,
        # guarded end-to-end. A DB hiccup or an unclassified bug
        # anywhere in this block must never leave the mention silently dropped —
        # it always produces a best-effort error message and never re-raises out
        # of the event handler. In practice this backstop only fires for
        # liveness-read/mutex-bookkeeping failures, since the turn-execution path
        # already has its own boundary in _handle_mention.
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        # May the agent post here at all? Decided FIRST -- before the liveness
        # read, provisioning or any notice -- and never raises: a protected
        # channel, or one whose protection can't be established, hears nothing
        # from this turn, not even the prologue's error reply.
        post_state = await _channel_protection_state(
            self.runtime.sessionmaker, tenant_id=tenant_id, channel=message.channel
        )
        if not post_state.may_post:
            log.info(
                "turn.skipped.writers_none",
                guild_id=guild_id,
                channel_id=str(message.channel.id),
                user_id=str(message.author.id),
                state=post_state.value,
            )
            return
        try:
            tr: TenantRow | None = await get_tenant_liveness(self.runtime.sessionmaker, tenant_id)
            if tr is None or tr.archived_at is not None:
                # Unprovisioned OR archived → provision + un-archive + seed in background.
                await self._ensure_provisioning(guild)
                await message.channel.send(_setting_up_message(bot_display_name))
                return
            if tr.provision_status == "failed":
                # Self-heal: re-seed if idle (in-flight guard). NEVER show "failed" to the user.
                self._spawn(
                    self._seed_tenant_defaults(tenant_id=tr.id, guild=guild, was_ready=False)
                )
                await message.channel.send(_setting_up_message(bot_display_name))
                return
            if tr.provision_status == "pending":
                await message.channel.send(_setting_up_message(bot_display_name))
                return
            # Only 'ready' proceeds.

            log.info(
                "mention_received",
                guild_id=guild_id,
                channel_id=str(message.channel.id),
                author_id=str(message.author.id),
            )

            assert self.runtime.settings.discord is not None, (
                "DaimonBot requires discord settings; "
                "the __main__.py entrypoint validates this at boot time"
            )

            # An explicit mention supersedes any batch waiting out its quiet
            # period in this thread: this turn's context carries those messages
            # anyway, so judging them separately would only duplicate the reply.
            if isinstance(message.channel, discord.Thread):
                self._cancel_participation_batch(message.channel.id)

            # Per-thread mention queueing. Check before claiming an in-flight slot
            # so that queued mentions never consume a slot they won't use.
            thread_id = message.channel.id
            if thread_id in self._processing:
                await self._queue_behind_inflight_turn(thread_id, message)
                return

            # --- Per-tenant concurrency cap (SCALE-01) ---
            # Read-check-increment in one synchronous span (no await between read
            # and increment) to avoid a race where two coroutines both read 0 and
            # both increment past the cap. The queue check above is also synchronous,
            # so there is exactly one increment per coroutine that reaches this point
            # and one matching decrement in the finally block below.
            cap = (
                tr.turn_cap
                if tr.turn_cap is not None
                else self.runtime.settings.discord.max_concurrent_turns_per_tenant
            )
            count = self._inflight.get(tenant_id, 0)
            if not should_admit_turn(current_in_flight=count, cap=cap):
                # Mirror of the Slack shed log — here the notice is a visible
                # channel message, but the log keeps shed counts greppable
                # across both adapters.
                record_refusal(
                    self.runtime.sessionmaker,
                    tenant_id=tenant_id,
                    platform="discord",
                    channel_id=str(thread_id),
                    thread_id=str(thread_id),
                )
                log.info(
                    "turn.skipped.concurrency_shed",
                    tenant_id=str(tenant_id),
                    guild_id=str(message.guild.id) if message.guild else None,
                    channel_id=str(message.channel.id),
                    in_flight=count,
                    cap=cap,
                )
                await message.channel.send(
                    "This server has too many chats in flight right now — try again in a moment."
                )
                return
            if not self.try_claim_global_turn():
                log.info(
                    "turn.skipped.global_concurrency_shed",
                    tenant_id=str(tenant_id),
                    guild_id=guild_id,
                    channel_id=str(thread_id),
                )
                await message.channel.send(GLOBAL_CAP_NOTICE)
                return
            self._inflight[tenant_id] = count + 1

            # Channel-level mentions each open their own thread + MA session, so
            # they run in parallel — no serialization (bounded by the
            # per-tenant and optional process-wide caps claimed above).
            # Serializing them by channel id wedged the whole channel whenever
            # a single turn stalled (e.g. an
            # upstream overload backoff with no SSE events for minutes).
            #
            # Channel mentions still parallelize per-mention (each opens its own
            # thread + MA session up front). But the bot-created thread is
            # registered in self._processing at creation time (inside
            # _orchestrate, immediately after create_thread), so an in-thread
            # follow-up mention that arrives during the *same* originating turn
            # queues instead of racing a second turn onto that thread's session —
            # this is the actual fix for the in-thread queue race. The earlier closure of
            # #163 was documentation-only; its regression test covered parallel
            # channel mentions, not the channel→in-thread sequence this closes.
            #
            # Only follow-up mentions *within an existing thread* are queued and
            # coalesced: a thread is one conversation on one MA session, and
            # overlapping turns on the same session must not interleave. After the
            # in-flight thread turn completes, the queue drains once into a single
            # composite follow-up turn.
            if not isinstance(message.channel, discord.Thread):
                created_thread_ids: list[int] = []
                try:
                    await self._handle_mention(
                        message, guild_id, tenant_id, created_thread_ids=created_thread_ids
                    )
                    # Drain-always: _handle_mention never raises after its own
                    # boundary, so this runs on both success and turn failure —
                    # a follow-up queued behind a failing originating turn still
                    # gets its drain turn instead of being silently discarded.
                    if created_thread_ids:
                        await self._drain_pending_mentions(
                            created_thread_ids[0], guild_id, tenant_id
                        )
                finally:
                    # No-op in the normal case (the drain above already emptied
                    # the queue) — this only catches messages that arrive after
                    # the final drain iteration, the same residual window the
                    # thread branch below has.
                    for created_id in created_thread_ids:
                        self._release_thread(created_id)
                        self._pending.pop(created_id, None)
                    self._release_inflight(tenant_id)
                    self.release_global_turn()
                return

            thread_id = message.channel.id
            self._processing.add(thread_id)
            try:
                await self._handle_mention(message, guild_id, tenant_id)
                await self._drain_pending_mentions(thread_id, guild_id, tenant_id)
            finally:
                self._release_thread(thread_id)
                self._pending.pop(thread_id, None)
                self._release_inflight(tenant_id)
                self.release_global_turn()
        except (DaimonError, _anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await self._handle_prologue_failure(message, exc, guild_id, post_state=post_state)
        except Exception as exc:  # on_message event-handler boundary
            await self._handle_prologue_failure(message, exc, guild_id, post_state=post_state)

    async def _queue_behind_inflight_turn(self, thread_id: int, message: discord.Message) -> None:
        """Queue ``message`` behind the thread's in-flight turn, then mark it ⌛.

        The append happens before the first await. The in-flight turn's drain
        loop and ``finally`` can run while this coroutine is suspended in
        ``add_reaction``; a message appended only after the reaction would land
        in ``_pending`` after the last drain, stranded until some later mention
        in the thread. Slack fixed the same ordering as WR-05.

        The reaction is a cosmetic hint, so its failure (missing Add Reactions
        permission, rate limit, a connection error surviving discord.py's
        retry) is logged and swallowed rather than dropping the queued message
        into the prologue error path.
        """
        self._thread_queue.enqueue(thread_id, message)
        try:
            await message.add_reaction("⌛")
        except Exception as exc:
            # best-effort queue marker after the message is already queued; see docstring
            log.warning(
                "mention_queue.reaction_failed",
                thread_id=str(thread_id),
                err_type=type(exc).__name__,
            )

    async def _handle_prologue_failure(
        self,
        message: discord.Message,
        exc: Exception,
        guild_id: str,
        *,
        post_state: ProtectionState,
    ) -> None:
        """Best-effort error render for on_message prologue failures (#170 backstop).

        Never raises — a failure here would defeat the whole point of the
        boundary it's called from. Mirrors _flip_failed_best_effort's
        try/log-only shape for the send itself. Posts only where the agent may
        post (``post_state``); otherwise the log is all there is.
        """
        log.exception(
            "mention_prologue_failed", guild_id=guild_id, channel_id=str(message.channel.id)
        )
        sentry_sdk.capture_exception(exc)
        if not post_state.may_post:
            return
        rid = generate_request_id()
        try:
            await message.channel.send(render_error(exc, request_id=rid))
        except discord.HTTPException:
            log.exception("mention_prologue_error_send_failed", guild_id=guild_id)

    @property
    def _thread_queue(self) -> ThreadQueue[int, discord.Message]:
        return ThreadQueue(self._processing, self._pending)

    async def _drain_pending_mentions(
        self, thread_id: int, guild_id: str, tenant_id: uuid.UUID
    ) -> None:
        async def run(messages: list[discord.Message]) -> None:
            await self._handle_mention(
                messages[0],
                guild_id,
                tenant_id,
                content_override=_compose_queued_content(messages),
                attachments_override=[a for m in messages for a in m.attachments],
            )

        await self._thread_queue.drain(
            thread_id,
            compose=lambda queued: group_by_author(queued, lambda m: m.author.id),
            run=run,
        )

    async def _handle_mention(
        self,
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        unprompted: bool = False,
    ) -> None:
        """Orchestrate thread creation/lookup, session lifecycle, and turn execution.

        When ``content_override`` is provided (drain path for queued mentions in a
        non-thread channel), it replaces ``message.content`` as the user message
        for the turn. Everything else (author, channel, attachments, thread
        history) still comes from ``message``.

        ``created_thread_ids``, when provided, receives the id of a bot-created
        thread even if the turn subsequently fails — the return value dies with
        the exception, and the caller (on_message's channel branch) needs the id
        to drain any follow-up mentions queued during the (still-registered)
        turn.

        ``attachments_override``, when provided (drain path), replaces
        ``message.attachments`` wholesale for the turn -- it carries the merged
        attachments from ALL of the queued author's messages, not just
        ``message``'s own.

        ``unprompted`` marks a turn nobody @mentioned (organic thread
        participation) so the context tells the agent it chose to speak.
        """
        rid = generate_request_id()
        structlog.contextvars.bind_contextvars(rid=rid)
        try:
            await self._orchestrate(
                message,
                guild_id,
                tenant_id,
                content_override=content_override,
                created_thread_ids=created_thread_ids,
                attachments_override=attachments_override,
                unprompted=unprompted,
            )
        except (DaimonError, _anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            log_anthropic_overload(
                exc,
                tenant_id=tenant_id,
                path="mention",
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
            )
            log.warning("turn.failed", error=str(exc), channel_id=str(message.channel.id))
            await self._render_turn_error(message, tenant_id, guild_id, rid, exc)
        except Exception as exc:  # mention-turn adapter boundary
            log.exception(
                "turn.failed.unexpected", error=str(exc), channel_id=str(message.channel.id)
            )
            await self._render_turn_error(message, tenant_id, guild_id, rid, exc)
        finally:
            structlog.contextvars.unbind_contextvars("rid")

    async def _render_turn_error(
        self,
        message: discord.Message,
        tenant_id: uuid.UUID,
        guild_id: str,
        rid: str,
        exc: Exception,
    ) -> None:
        """Sentry-tag + post a rendered error for a turn failure caught in _handle_mention."""
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("rid", rid)
            scope.set_tag("tenant_id", str(tenant_id))
            scope.set_tag("guild_id", guild_id)
            sentry_sdk.capture_exception(exc)
        error_text = render_error(exc, request_id=rid)
        target = message.channel
        transport = DiscordPostTransport(
            self,
            target,
            name="Daimon",
            avatar_url=None,
            builtin=True,
        )
        if isinstance(target, discord.Thread):
            await safe_thread_send(target, error_text, transport=transport)
        else:
            await transport.send(error_text)

    async def _dispatch_continuations(
        self, *, tenant_id: uuid.UUID, thread: discord.Thread, guild_id: str
    ) -> None:
        """Claim and run any pending continuations for `thread`, guard already held.

        Callers must already own this thread's `_processing` slot. The
        turn-completion path does (`on_message` holds it for the whole turn);
        anything outside a turn goes through
        `dispatch_continuations_in_thread`, which takes the guard first.
        """
        if self.draining:
            return
        await dispatch_pending_continuations(
            self.runtime.sessionmaker,
            self.runtime.anthropic,
            tenant_id=tenant_id,
            thread=thread,
            run_follow_up=lambda row, decision: self._run_continuation_turn(
                row, decision, thread=thread, tenant_id=tenant_id, guild_id=guild_id
            ),
            may_post=lambda: self._may_post_in(tenant_id=tenant_id, channel=thread),
            client=self,
            public_base_url=self.runtime.settings.mcp.app_root_url,
            identity_enabled=self.runtime.settings.agent_identity.enabled,
        )

    async def _may_post_in(self, *, tenant_id: uuid.UUID, channel: object) -> bool:
        """The access policy's may-post decision for a channel or thread."""
        state = await _channel_protection_state(
            self.runtime.sessionmaker, tenant_id=tenant_id, channel=channel
        )
        return state.may_post

    async def dispatch_continuations_in_thread(
        self, *, tenant_id: uuid.UUID, thread: discord.Thread, guild_id: str
    ) -> None:
        await self._wait_for_orphan_recovery()
        if not claim_dispatch(
            self._processing,
            thread.id,
            self._deferred_dispatch,
            thread.id,
            (tenant_id, thread, guild_id),
        ):
            return
        try:
            await dispatch_and_drain(
                lambda: self._dispatch_continuations(
                    tenant_id=tenant_id, thread=thread, guild_id=guild_id
                ),
                lambda: self._drain_pending_mentions(thread.id, guild_id, tenant_id),
                drain_on_error=True,
            )
        finally:
            self._release_thread(thread.id)

    async def _channel_by_id(self, channel_id: int) -> object:
        """Cached channel, else a REST fetch (raises NotFound/Forbidden)."""
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def open_member_dm(self, guild_id: int, user_id: int) -> discord.abc.Messageable:
        """A DM with a human member of `guild_id` (FEAT-085's delivery fallback).

        Same membership rule as the direct-message tool: the recipient must be
        a current, non-bot member of the tenant's guild.
        """
        guild = self.get_guild(guild_id) or await self.fetch_guild(guild_id)
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if member.bot:
            raise LookupError("the routine's creator is not a human member")
        return await member.create_dm()

    async def _open_wake_thread(self, wake: WakeThread) -> bool:
        """The wake poller's hook: dispatch a thread's due wakes, spawned.

        Goes through `dispatch_continuations_in_thread`, so a wake takes the
        same per-thread guard and the same admit -> bind -> run path as any
        continuation. A thread Discord says is gone or forbidden has its wakes
        settled; any other failure returns False and the poller pushes the
        thread back. While draining, the rows are left for the next process.
        """
        if self.draining:
            return True
        try:
            channel = self.get_channel(int(wake.thread_id)) or await self.fetch_channel(
                int(wake.thread_id)
            )
        except (discord.NotFound, discord.Forbidden):
            channel = None
        except discord.HTTPException as exc:
            log.warning("wake.thread_fetch_failed", thread_id=wake.thread_id, error=str(exc))
            return False
        if not isinstance(channel, discord.Thread):
            await skip_thread_wakes(
                self.runtime.sessionmaker,
                thread=wake,
                reason="thread_unavailable",
                now=datetime.now(UTC),
            )
            return True
        self._spawn(
            self.dispatch_continuations_in_thread(
                tenant_id=wake.tenant_id, thread=channel, guild_id=str(channel.guild.id)
            )
        )
        return True

    def _release_thread(self, thread_id: int) -> None:
        def resume(_key: int, request: tuple[uuid.UUID, discord.Thread, str]) -> None:
            tenant_id, thread, guild_id = request
            self._spawn(
                self.dispatch_continuations_in_thread(
                    tenant_id=tenant_id,
                    thread=thread,
                    guild_id=guild_id,
                )
            )

        release_thread(
            self._processing,
            thread_id,
            self._deferred_dispatch,
            dispatch_keys=lambda: [thread_id],
            draining=self.draining,
            resume=resume,
        )

    async def _run_continuation_turn(
        self,
        row: TaskContinuationRow,
        decision: ContinuationDecision,
        *,
        thread: discord.Thread,
        tenant_id: uuid.UUID,
        guild_id: str,
    ) -> None:
        try:
            with observe_turn(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="discord",
                channel_id=str(thread.parent_id),
                thread_id=str(thread.id),
                origin="handoff",
            ):
                return await self._run_continuation_turn_observed(
                    row, decision, thread=thread, tenant_id=tenant_id, guild_id=guild_id
                )
        except _anthropic.APIError as exc:
            log_anthropic_overload(
                exc,
                tenant_id=tenant_id,
                path="continuation",
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
            )
            raise

    async def _run_continuation_turn_observed(
        self,
        row: TaskContinuationRow,
        decision: ContinuationDecision,
        *,
        thread: discord.Thread,
        tenant_id: uuid.UUID,
        guild_id: str,
    ) -> None:
        """Run the destination agent's first turn for one dispatched continuation.

        Same admit -> bind_session -> run_prepared_turn path an ordinary
        mention takes, seeded with the requester's own words
        (`decision.seed_user_message`) instead of a Discord message, and
        framed with a one-time `HandoffNotice` since this is the receiving
        agent's first turn in the thread. Raises on failure (propagates to
        `continuation_dispatch`'s caller, which settles the row) rather than
        rendering its own error -- there is no lifecycle message to attach
        one to before `bind_session` has even run.
        """
        _ = guild_id  # kept for parity with _handle_mention's error-context signature
        # Read the predecessor BEFORE bind_session decides the replacement --
        # once it runs, the old row is superseded and this is the only chance
        # to read what it was running.
        async with self.runtime.sessionmaker() as session:
            predecessor = await get_live_thread_session(
                session,
                tenant_id=tenant_id,
                platform="discord",
                thread_id=row.thread_id,
                account_id=row.requester_account_id,
            )
            from_ma_agent_id = predecessor.ma_agent_id if predecessor is not None else None
            asking_ma_agent_id = await load_asking_agent_id(
                session, row, live_ma_agent_id=from_ma_agent_id
            )
        from_name = (
            predecessor.effective_config.agent_name
            if predecessor is not None and predecessor.effective_config is not None
            else None
        )

        # The follow-up runs as the requester, so it carries their role as it
        # stands NOW -- re-read from the guild, never from anything the form
        # or the row recorded. A lookup that fails runs the turn as USER.
        role, role_ids = await _requester_role(thread.guild, row.requester_external_user_id)
        # A continuation posts into the thread too: a protected one is skipped
        # (the dispatcher logs it) before admission, which then reads the
        # parent the check cached.
        post_state = await _channel_protection_state(
            self.runtime.sessionmaker, tenant_id=tenant_id, channel=thread
        )
        if not post_state.may_post:
            raise AdmissionDenied(reason="writers_none")
        category_id, category_unresolved = await _resolve_category(thread, fetch=False)
        admission = await admit(
            self.runtime.turn_deps,
            tenant_id=tenant_id,
            platform="discord",
            external_user_id=row.requester_external_user_id,
            channel_id=row.parent_channel_id,
            thread_id=row.thread_id,
            role=role,
            platform_role_ids=role_ids,
            now=datetime.now(UTC),
            category_id=category_id,
            category_unresolved=category_unresolved,
        )
        # A wake runs only as the agent it was queued for; a thread rerouted in
        # the meantime refuses it here, before any card, bind or billed turn.
        check_wake_responder(
            reason=row.reason,
            target_ma_agent_id=row.target_ma_agent_id,
            target_name=row.target_name,
            admitted_ma_agent_id=admission.agent.id,
            admitted_name=admission.agent.name,
            asking_ma_agent_id=asking_ma_agent_id,
        )
        turn_deadline_at = turn_deadline(now=datetime.now(UTC))
        agent = admission.agent
        try:
            async with self.runtime.sessionmaker.begin() as identity_session:
                identity = await resolve_agent_identity(
                    identity_session,
                    tenant_id=tenant_id,
                    agent_name=agent.name,
                    is_builtin=agent.name.casefold() == "daimon",
                    public_base_url=self.runtime.settings.mcp.app_root_url,
                    enabled=self.runtime.settings.agent_identity.enabled,
                )
        except Exception as exc:
            log.warning("discord.identity_resolution_failed", error_type=type(exc).__name__)
            identity = AgentIdentity(name=agent.name, avatar_url=None, builtin=True)

        # kwargs are forwarded verbatim to discord.py's overloaded send()/edit().
        recorder = TurnPostRecorder(
            sessionmaker=self.runtime.sessionmaker,
            tenant_id=tenant_id,
            ma_agent_id=agent.id,
            requester_id=int(row.requester_external_user_id),
        )
        transport = DiscordPostTransport(
            self,
            thread,
            name=identity.name,
            avatar_url=identity.avatar_url,
            builtin=identity.builtin,
            identity_enabled=self.runtime.settings.agent_identity.enabled,
        )

        async def _edit_message(
            msg: discord.Message,
            **kwargs: Any,  # noqa: ANN401
        ) -> discord.Message | None:
            return await transport.edit(msg, **kwargs)

        async def _delete_message(msg: discord.Message) -> None:
            await transport.delete(msg)

        cancel = asyncio.Event()

        def _make_lifecycle(
            turn_id: uuid.UUID, on_first_post: Callable[[discord.Message], Awaitable[None]]
        ) -> DiscordTurnLifecycle:
            return DiscordTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                tenant_id=tenant_id,
                budget_channel_id=admission.budget_channel_id,
                requester_id=int(row.requester_external_user_id),
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                render_tables=self.runtime.settings.table_rendering.get(tenant_id, False) is True,
                send=recorder.sender(thread, turn_card_intent_id=turn_id, transport=transport),
                edit=_edit_message,
                delete=_delete_message,
                agent_name=agent.name,
                fallback_active=lambda: transport.fallback_used,
                model_id=agent.model.id,
                cancel_view=CancelView(
                    allowed_user_id=int(row.requester_external_user_id),
                    cancel=cancel,
                    turn_id=turn_id,
                ),
                unprompted=False,
                on_first_post=on_first_post,
                on_replacement=lambda msg: recorder.message(
                    thread, msg, turn_card_intent_id=turn_id
                ),
            )

        turn_card_intent, lifecycle = await post_initial_turn_card(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            thread_id=row.thread_id,
            make_lifecycle=_make_lifecycle,
        )

        session_account_id = admission.account_id
        prepared = await bind_session(
            self.runtime.turn_deps,
            admission,
            tenant_id=tenant_id,
            platform="discord",
            external_user_id=row.requester_external_user_id,
            thread_id=row.thread_id,
            session_account_id=session_account_id,
            reuse_existing=True,
            deadline=turn_deadline_at,
        )

        if prepared.mapping_id is not None and lifecycle.final_message_id is not None:
            async with self.runtime.sessionmaker() as session:
                await mark_turn_active(
                    session,
                    id=prepared.mapping_id,
                    active_turn_message_id=lifecycle.final_message_id,
                    now=datetime.now(UTC),
                )
                await session.commit()

        transfer_kind = prepared.continuity.transfer_kind
        workspace: Literal["transferred", "transcript_only", "history_only"] = (
            "transferred"
            if transfer_kind == "full"
            else "transcript_only"
            if transfer_kind == "transcript"
            else "history_only"
        )
        # Only a handoff hands the task to a different agent, so only a handoff
        # gets the one-time notice. `private_input_applied` re-runs the SAME
        # agent that just asked for the value, so framing it as "X handed you
        # this" would describe a transfer that never happened.
        handoff_notice = (
            HandoffNotice(
                from_name=from_name or "the previous agent",
                from_ma_agent_id=from_ma_agent_id or "",
                requested_by=f"<@{row.requester_external_user_id}>",
                requested_work=decision.seed_user_message,
                workspace=workspace,
            )
            if row.reason == "task_handoff"
            else None
        )
        session_state = (
            None
            if prepared.continuity.state == "continued"
            else SessionState(
                state=prepared.continuity.state, applied=prepared.continuity.applied, lost=()
            )
        )
        seed_message = decision.seed_user_message or ""

        lifecycle_holder: list[DiscordTurnLifecycle] = [lifecycle]

        async def _reseed_continuation_message() -> str:
            async with self.runtime.sessionmaker() as session:
                recovery_origin = await get_active_origin(
                    session,
                    origin_id=origin.id,
                    tenant_id=tenant_id,
                    account_id=admission.account_id,
                    platform="discord",
                    now=datetime.now(UTC),
                )
            if recovery_origin is None:
                raise DaimonError(
                    "This turn's setup context expired. Mention me again to continue."
                )
            return (
                render_turn_origin(
                    recovery_origin,
                    responder_handle=_responder_handle(self.runtime.settings),
                    session_state=session_state,
                    handoff=handoff_notice,
                    is_channel_admin=is_channel_admin,
                )
                + "\n"
                + seed_message
            )

        def _recovery_lifecycle(cancel_event: asyncio.Event) -> TurnLifecycle:
            new_lifecycle = DiscordTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                tenant_id=tenant_id,
                budget_channel_id=admission.budget_channel_id,
                requester_id=int(row.requester_external_user_id),
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                render_tables=self.runtime.settings.table_rendering.get(tenant_id, False) is True,
                send=recorder.sender(
                    thread, turn_card_intent_id=turn_card_intent.id, transport=transport
                ),
                edit=_edit_message,
                delete=_delete_message,
                agent_name=agent.name,
                fallback_active=lambda: transport.fallback_used,
                model_id=agent.model.id,
                cancel_view=CancelView(
                    allowed_user_id=int(row.requester_external_user_id),
                    cancel=cancel_event,
                    turn_id=turn_card_intent.id,
                ),
                adopt_message_ref=lifecycle.message_ref,
                unprompted=False,
                on_replacement=lambda msg: recorder.message(
                    thread, msg, turn_card_intent_id=turn_card_intent.id
                ),
            )
            lifecycle_holder[0] = new_lifecycle
            return new_lifecycle

        is_channel_admin = await holds_current_channel_admin_grant(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            platform="discord",
            parent_channel_id=row.parent_channel_id,
            role=role,
        )
        outcome: RunOutcome | None = None
        archive_after = False
        try:
            async with turn_origin(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                account_id=admission.account_id,
                platform="discord",
                parent_channel_id=row.parent_channel_id,
                thread_id=row.thread_id,
                responder_ma_agent_id=str(agent.id),
                responder_name=admission.config.agent_name or agent.name,
                configuration_target_ma_agent_id=admission.config.configuration_target_ma_agent_id,
                configuration_target_name=admission.config.configuration_target_name,
                role=role,
                is_setup=admission.config.thread_binding_kind == "setup",
            ) as origin:
                outcome = await run_prepared_turn(
                    self.runtime.turn_deps,
                    prepared,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=row.thread_id,
                    external_user_id=row.requester_external_user_id,
                    origin="handoff" if handoff_notice is not None else "chat",
                    user_message=(
                        render_turn_origin(
                            origin,
                            responder_handle=_responder_handle(self.runtime.settings),
                            session_state=session_state,
                            handoff=handoff_notice,
                            is_channel_admin=is_channel_admin,
                        )
                        + "\n"
                        + seed_message
                    ),
                    lifecycle=lifecycle,
                    cancel=cancel,
                    reseed_user_message=_reseed_continuation_message,
                    recovery_lifecycle=_recovery_lifecycle,
                    render_interval_s=2.0,
                    deadline=turn_deadline_at,
                    confirm_write=discord_confirmation_hook(thread),
                )
                archive_after = await self._archive_requested(origin.id)
        finally:
            done_ids = {prepared.mapping_id}
            if outcome is not None:
                done_ids.add(outcome.mapping_id)
            for done_id in done_ids - {None}:
                if done_id is None:  # pragma: no cover - set difference guarantees this
                    continue
                async with self.runtime.sessionmaker() as session:
                    await clear_active_turn(session, id=done_id)
                    await session.commit()
            if outcome is not None and not lifecycle_holder[0].card_discard_failed:
                await retire_terminal_turn_card(
                    self.runtime.sessionmaker,
                    intent_id=turn_card_intent.id,
                    expected_message_id=lifecycle_holder[0].card_message_id,
                    allow_prepared_without_message=not lifecycle_holder[0].first_post_attempted,
                )

        assert outcome is not None
        log_anthropic_overload(
            outcome.state.error.cause if outcome.state.error is not None else None,
            tenant_id=tenant_id,
            path="continuation",
            alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
        )
        final_lifecycle = lifecycle_holder[0]
        if outcome.state.error is None and prepared.mapping_id is not None:
            if final_lifecycle.final_message_id is not None:
                async with self.runtime.sessionmaker() as session:
                    await update_watermark(
                        session,
                        id=prepared.mapping_id,
                        watermark_message_id=final_lifecycle.final_message_id,
                    )
                    await session.commit()
            if final_lifecycle.was_answered and final_lifecycle.final_message_id is not None:
                await seed_feedback_reactions(thread, message_id=final_lifecycle.final_message_id)
        self._schedule_output_sweep(outcome, thread=thread, tenant_id=tenant_id)
        if archive_after:
            await self._archive_after_outputs(outcome, thread)

    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent) -> None:
        metadata = payload.data["thread_metadata"]
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(payload.guild_id))
        async with self.runtime.sessionmaker() as session:
            await update_lifecycle(
                session,
                tenant_id=tenant_id,
                platform="discord",
                parent_channel_id=str(payload.parent_id),
                thread_id=str(payload.thread_id),
                archived=metadata.get("archived"),
                locked=metadata.get("locked"),
            )
            await session.commit()

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(payload.guild_id))
        async with self.runtime.sessionmaker() as session:
            await update_lifecycle(
                session,
                tenant_id=tenant_id,
                platform="discord",
                parent_channel_id=str(payload.parent_id),
                thread_id=str(payload.thread_id),
                deleted=True,
            )
            await session.commit()

    async def _orchestrate(
        self,
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        unprompted: bool = False,
    ) -> None:
        with observe_turn(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="discord",
            channel_id=str(message.channel.id),
        ):
            return await self._orchestrate_observed(
                message,
                guild_id,
                tenant_id,
                content_override=content_override,
                created_thread_ids=created_thread_ids,
                attachments_override=attachments_override,
                unprompted=unprompted,
            )

    async def _orchestrate_observed(
        self,
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        unprompted: bool = False,
    ) -> None:
        """Core orchestration logic extracted for clean error boundary.

        ``created_thread_ids``, when provided, receives the id of a
        bot-created thread as soon as it exists — even if this call later
        raises — so the caller can still drain any mentions queued against
        that thread during this turn.

        ``attachments_override``, when provided, replaces ``message.attachments``
        wholesale for the attachment split below -- the drain path uses this to
        carry the merged attachments from all of a queued author's messages
        (for the merged-attachments path).

        ``unprompted`` marks the trigger as one nobody @mentioned; it reaches
        the agent as an attribute on the ``<user_query>`` element.
        """
        # Serialize every turn against the boot snapshot, including a turn
        # that arrives before on_ready, so a marker written by this process
        # cannot be mistaken for an orphan.
        await self._wait_for_orphan_recovery()
        if self.user is None:
            log.warning("orchestrate_called_before_ready")
            return

        # --- Thread classification (no DB lookup — respond to all mentions) ---
        if isinstance(message.channel, discord.Thread):
            parent_channel_id = str(message.channel.parent_id)
            thread = message.channel
        else:
            parent_channel_id = str(message.channel.id)
            thread = None

        author = message.author
        is_admin = isinstance(author, discord.Member) and is_member_guild_admin(
            author, guild_owner_id=message.guild.owner_id if message.guild else None
        )
        role = Role.ADMIN if is_admin else Role.USER
        # Cache-only: on_message's protection check already fetched an uncached
        # parent when a category is protected, and cached it.
        category_id, category_unresolved = await _resolve_category(message.channel, fetch=False)

        # --- Stage one: admission (identity, config cascade, missing-config
        # bail, MA resolve/retrieve, balance gate, cap gate) -- D-01 admit(). ---
        try:
            admission = await admit(
                self.runtime.turn_deps,
                tenant_id=tenant_id,
                platform="discord",
                external_user_id=str(message.author.id),
                channel_id=parent_channel_id,
                thread_id=str(thread.id) if thread else None,
                role=role,
                platform_role_ids=member_role_ids(author)
                if isinstance(author, discord.Member)
                else None,
                now=datetime.now(UTC),
                category_id=category_id,
                category_unresolved=category_unresolved,
            )
        except MissingTurnConfigError as err:
            log.info(
                "missing_config",
                guild_id=guild_id,
                channel_id=parent_channel_id,
                missing=list(err.missing),
            )
            if unprompted:
                return  # nobody asked; a notice per quiet burst would spam the thread
            target = thread or message.channel
            hints: list[str] = []
            if "agent" in err.missing:
                hints.append(
                    "An admin can tell Daimon: make an agent answer in this channel or the "
                    "whole server. Run `/agent-setup` to see who answers where."
                )
            if "environment" in err.missing:
                hints.append(
                    "An admin of this server or channel can pick an environment in "
                    "`/agent-setup` → Who answers where."
                )
            await target.send(
                f"No {' or '.join(err.missing)} configured for this channel. " + " ".join(hints)
            )
            return
        except MAResolverMissError as err:
            log.warning(
                "resolver.miss",
                kind=err.kind,
                daimon_tag=err.daimon_tag,
                tenant_id=str(err.tenant_id),
            )
            if unprompted:
                return
            target = thread or message.channel
            await target.send(
                "The configured agent or environment no longer exists. An admin of this "
                "server or channel can pick another in `/agent-setup`."
            )
            return
        except AdmissionDenied as err:
            if unprompted:
                # An unprompted turn that cannot be admitted stays silent: the
                # balance or cap notice is owed to someone who asked, and every
                # quiet burst would otherwise repost it.
                log.info(
                    "thread_participation.skipped",
                    reason=err.reason,
                    guild_id=guild_id,
                    tenant_id=str(tenant_id),
                )
                return
            target = thread or message.channel
            if err.reason == "writers_none":
                # Nothing may be posted into a protected channel, a refusal
                # included; the log is the only trace.
                log.info(
                    "turn.skipped.writers_none",
                    guild_id=guild_id,
                    channel_id=parent_channel_id,
                    user_id=str(message.author.id),
                )
            elif err.reason == "invoker_not_allowed":
                log.info(
                    "turn.skipped.invoker_not_allowed",
                    guild_id=guild_id,
                    user_id=str(message.author.id),
                )
            elif err.reason == "runs_elsewhere":
                log.info(
                    "turn.skipped.runs_elsewhere",
                    guild_id=guild_id,
                    channel_id=parent_channel_id,
                    user_id=str(message.author.id),
                )
            elif err.reason == "own_agents_only":
                log.info(
                    "turn.skipped.own_agents_only",
                    guild_id=guild_id,
                    channel_id=parent_channel_id,
                    user_id=str(message.author.id),
                )
            elif err.reason == "balance_depleted":
                log.info("turn.skipped.over_balance", guild_id=guild_id, tenant_id=str(tenant_id))
            elif err.reason == "channel_budget_exceeded":
                log.info(
                    "turn.skipped.over_channel_budget",
                    guild_id=guild_id,
                    channel_id=parent_channel_id,
                )
            else:
                log.info(
                    "turn.skipped.over_cap",
                    guild_id=guild_id,
                    user_id=str(message.author.id),
                )
            if err.reason != "writers_none":
                await target.send(admission_refusal_message(err.reason, self.runtime.settings))
            return

        # D-03 boundary: the per-turn ceiling clock starts here, once admission
        # has passed, and covers session bind/create (bind_session) plus the
        # driver pump (run_prepared_turn) as ONE shared budget -- admit()
        # itself is deliberately outside it.
        turn_deadline_at = turn_deadline(now=datetime.now(UTC))

        agent = admission.agent

        # --- Create thread + status embed BEFORE session create ---
        # MA sessions.create can hold its HTTP response for minutes while it
        # provisions the session (the record exists server-side in ~1s; the
        # response is what stalls). The thread and a thinking embed go up
        # first, behind only the short naming call, so the user gets early
        # feedback; the lifecycle adopts the embed and edits it in place once
        # SSE events flow.
        recorder = TurnPostRecorder(
            sessionmaker=self.runtime.sessionmaker,
            tenant_id=tenant_id,
            ma_agent_id=agent.id,
            requester_id=message.author.id,
        )
        is_thread_mention = thread is not None
        if thread is None:

            async def _open_thread() -> discord.Thread:
                # Name before creation to avoid a Discord rename system message.
                thread_name = f"Chat with {agent.name}"
                naming = self.runtime.settings.thread_naming
                opening_text = strip_mentions(message.content)
                if naming.enabled and opening_text:
                    async with message.channel.typing():
                        thread_name = await generate_thread_name(
                            fallback=thread_name,
                            message_text=opening_text,
                            message_id=message.id,
                            anthropic=self.runtime.anthropic,
                            sessionmaker=self.runtime.sessionmaker,
                            tenant_id=tenant_id,
                            platform_user_id=str(message.author.id),
                            markup=self.runtime.settings.billing.markup,
                            max_input_chars=naming.max_input_chars,
                            timeout_seconds=naming.timeout_seconds,
                            channel_id=admission.budget_channel_id,
                        )
                opened = await message.create_thread(name=thread_name, auto_archive_duration=10080)
                # No await between creation and registration: follow-ups must queue
                # behind this turn. The channel branch owns the eventual cleanup.
                self._processing.add(opened.id)
                if created_thread_ids is not None:
                    created_thread_ids.append(opened.id)
                await recorder.opened_thread(opened)
                return opened

            discord_settings = self.runtime.settings.discord
            assert discord_settings is not None
            thread = await _open_thread_with_notice(
                message,
                _open_thread(),
                guild_id=guild_id,
                after_s=discord_settings.thread_open_notice_after_s,
            )

        # --- Wire lifecycle with send/edit callables ---
        # kwargs are forwarded verbatim to discord.py's overloaded send()/edit().

        try:
            async with self.runtime.sessionmaker.begin() as identity_session:
                identity = await resolve_agent_identity(
                    identity_session,
                    tenant_id=tenant_id,
                    agent_name=agent.name,
                    is_builtin=agent.name.casefold() == "daimon",
                    public_base_url=self.runtime.settings.mcp.app_root_url,
                    enabled=self.runtime.settings.agent_identity.enabled,
                )
        except Exception as exc:
            log.warning("discord.identity_resolution_failed", error_type=type(exc).__name__)
            identity = AgentIdentity(name=agent.name, avatar_url=None, builtin=True)

        transport = DiscordPostTransport(
            self,
            thread,
            name=identity.name,
            avatar_url=identity.avatar_url,
            builtin=identity.builtin,
            identity_enabled=self.runtime.settings.agent_identity.enabled,
        )

        async def _edit_message(
            msg: discord.Message,
            **kwargs: Any,  # noqa: ANN401
        ) -> discord.Message | None:
            return await transport.edit(msg, **kwargs)

        async def _delete_message(msg: discord.Message) -> None:
            await transport.delete(msg)

        cancel = asyncio.Event()

        def _make_lifecycle(
            turn_id: uuid.UUID, on_first_post: Callable[[discord.Message], Awaitable[None]]
        ) -> DiscordTurnLifecycle:
            return DiscordTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                tenant_id=tenant_id,
                budget_channel_id=admission.budget_channel_id,
                requester_id=message.author.id,
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                trigger_message=message,
                render_tables=self.runtime.settings.table_rendering.get(tenant_id, False) is True,
                send=recorder.sender(thread, turn_card_intent_id=turn_id, transport=transport),
                edit=_edit_message,
                delete=_delete_message,
                agent_name=agent.name,
                fallback_active=lambda: transport.fallback_used,
                model_id=agent.model.id,
                cancel_view=CancelView(
                    allowed_user_id=message.author.id, cancel=cancel, turn_id=turn_id
                ),
                unprompted=unprompted,
                on_first_post=on_first_post,
                on_replacement=lambda msg: recorder.message(
                    thread, msg, turn_card_intent_id=turn_id
                ),
            )

        turn_card_intent, lifecycle = await post_initial_turn_card(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            thread_id=str(thread.id),
            make_lifecycle=_make_lifecycle,
        )
        turn_send = recorder.sender(
            thread, turn_card_intent_id=turn_card_intent.id, transport=transport
        )

        discord_settings = self.runtime.settings.discord
        assert discord_settings is not None, (
            "_orchestrate called without discord settings — entrypoint must validate at boot"
        )
        # Each caller in a thread keys their own session on their own account,
        # so nobody reuses another caller's session identity (#162).
        session_account_id = admission.account_id

        # --- Stage two: bind_session (find-or-create, mapping write,
        # recorder binding) -- D-01 bind_session(). ---
        try:
            prepared = await bind_session(
                self.runtime.turn_deps,
                admission,
                tenant_id=tenant_id,
                platform="discord",
                external_user_id=str(message.author.id),
                thread_id=str(thread.id),
                session_account_id=session_account_id,
                reuse_existing=is_thread_mention,
                deadline=turn_deadline_at,
            )
        except SessionAgentMismatch as error:
            # Replace this attempt's status card; the mapped session and its
            # workspace remain intact for the previous responder. No handoff
            # binding authorized this responder change (a handoff would have
            # resolved via config.thread_binding_kind == "handoff" instead),
            # so the copy offers the handoff rather than describing an error.
            owner_name = await _resolve_agent_display_name(
                self.runtime.anthropic, tenant_id=tenant_id, ma_agent_id=error.source_agent_id
            )
            error_text = render_error(
                error,
                request_id=generate_request_id(),
                new_responder=agent.name,
                owner=owner_name,
                channel=f"<#{parent_channel_id}>",
                offer_button=True,
            )
            # Local import: thread_handoff imports this module.
            from daimon.adapters.discord.thread_handoff import hand_over_view

            view = hand_over_view(agent_id=agent.id, agent_name=agent.name)
            summary, separator, remaining = error_text.partition("\nNew thread → ")
            responder, _, request_id_line = remaining.partition("\n`rid: ")
            handoff_embed = discord.Embed(
                title="Switch agent",
                description=summary,
                color=discord.Color.blurple(),
            )
            if separator:
                handoff_embed.add_field(
                    name="New thread", value=f"New thread → {responder}", inline=False
                )
            if request_id_line:
                handoff_embed.set_footer(text=f"rid: {request_id_line.rstrip('`')}")
            if lifecycle.message_ref is not None:
                await _edit_message(
                    lifecycle.message_ref, content=None, embed=handoff_embed, view=view
                )
            else:
                await turn_send(embed=handoff_embed, view=view)
            await retire_terminal_turn_card(
                self.runtime.sessionmaker,
                intent_id=turn_card_intent.id,
                expected_message_id=lifecycle.card_message_id,
                no_post_confirmed=not lifecycle.first_post_attempted,
            )
            return
        except SessionPreparationFailed:
            # Nothing was attempted -- bind_session did not run the turn
            # against a configuration nobody asked for, so no turn to render
            # here either; the copy says what was preserved and asks for a retry.
            failure_text = render_preparation_failed(agent.name)
            if lifecycle.message_ref is not None:
                await _edit_message(
                    lifecycle.message_ref, content=failure_text, embed=None, view=None
                )
            else:
                await turn_send(failure_text)
            await retire_terminal_turn_card(
                self.runtime.sessionmaker,
                intent_id=turn_card_intent.id,
                expected_message_id=lifecycle.card_message_id,
                no_post_confirmed=not lifecycle.first_post_attempted,
            )
            return
        except SessionBusyError as busy:
            # Nothing failed and nothing is misconfigured: the previous turn in
            # this thread is still running, and the session it is running in
            # belongs to the OUTGOING responder. Making the change around it
            # would answer as one agent inside another agent's workspace, so no
            # turn runs here -- the person is told the in-flight message
            # finishes first and the switch applies to their next one. A seal
            # that landed while the turn was prepared defers it the same way;
            # the next message is prepared read-only, so it says to send again.
            busy_text = (
                render_access_changed_try_again()
                if "seal" in busy.pending_reasons
                else render_current_work_must_finish(agent.name, handoff=True)
            )
            if lifecycle.message_ref is not None:
                await _edit_message(lifecycle.message_ref, content=busy_text, embed=None, view=None)
            else:
                await turn_send(busy_text)
            await retire_terminal_turn_card(
                self.runtime.sessionmaker,
                intent_id=turn_card_intent.id,
                expected_message_id=lifecycle.card_message_id,
                no_post_confirmed=not lifecycle.first_post_attempted,
            )
            return

        log.info(
            "session.ready",
            session_id=prepared.ma_session_id,
            thread_id=thread.id,
            reused=prepared.reused,
        )
        # A planned transition the caller didn't necessarily ask to see this
        # turn: say what happened to the workspace BEFORE the answer, so a
        # replacement is never discovered only by its side effects.
        # `prepared.continuity` is the bind's OWN decision, taken before the
        # turn runs -- it can never be "replaced_after_loss" (that state only
        # exists on the post-turn `RunOutcome`, set when `run_prepared_turn`'s
        # recovery cycle recreates the session mid-call). That notice is
        # posted after the turn instead, once the outcome is known.
        replacement_summary: str | None = None
        if (
            prepared.continuity.state == "replaced"
            and prepared.continuity.transfer_kind is not None
        ):
            # Not a separate message: the answer is an in-place edit of the
            # embed posted at mention time, so a summary sent at any point
            # after that lands BELOW the answer it explains ("the file
            # vanished" above "here is why"). It becomes the answer's first
            # paragraph instead. The fallback after the turn covers an answer
            # that never arrives to carry it.
            #
            # A `None` transfer_kind here means a fresh start (no prior
            # session to summarize a transfer from) -- that path already
            # announces itself elsewhere, so no prefix is rendered.
            replacement_summary = render_replacement_summary(prepared.continuity.transfer_kind, [])
            lifecycle.answer_prefix = replacement_summary
        session_state = (
            None
            if prepared.continuity.state == "continued"
            else SessionState(
                state=prepared.continuity.state, applied=prepared.continuity.applied, lost=()
            )
        )

        # Flag the turn as in flight. The render loop lives in THIS process, so
        # if the container is recreated mid-turn the embed freezes forever while
        # MA completes and bills the answer server-side. Recording the embed's
        # id is what lets the next boot find it and say so.
        if prepared.mapping_id is not None and lifecycle.final_message_id is not None:
            async with self.runtime.sessionmaker() as _at_session:
                await mark_turn_active(
                    _at_session,
                    id=prepared.mapping_id,
                    active_turn_message_id=lifecycle.final_message_id,
                    now=datetime.now(UTC),
                )
                await _at_session.commit()

        # Names-only <keys> context: stored key names for THIS agent, named
        # only while the mounted `.env` is still exactly today's agent_files
        # rows (see `list_mounted_key_names`). One extra read per turn.
        async with self.runtime.sessionmaker() as _keys_session:
            _live_row_for_keys = await get_live_thread_session(
                _keys_session,
                tenant_id=tenant_id,
                platform="discord",
                thread_id=str(thread.id),
                account_id=session_account_id,
            )
            _live_config = (
                None if _live_row_for_keys is None else _live_row_for_keys.effective_config
            )
            _env_sha256 = None if _live_config is None else _live_config.env_sha256
            key_names = await list_mounted_key_names(
                _keys_session,
                tenant_id=tenant_id,
                agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
                env_sha256=_env_sha256,
            )

        # Split trigger-message attachments: API-consumable images → vision
        # blocks; everything else (data files, unsupported/oversized images)
        # → signed CDN URL surfaced to the agent (it has bash + network egress
        # and curls the file itself). If it needs the file on a notebook
        # workspace to publish, it uploads on demand via the
        # create_attachment_upload_url MCP tool — the bot no longer uploads
        # eagerly, so there is nothing to silently skip.
        #
        # attachments_override (drain path) replaces message.attachments
        # wholesale with the merged attachments from all of the queued
        # author's messages -- otherwise only the first queued message's
        # attachments would ever reach the turn.
        attachments = (
            attachments_override if attachments_override is not None else message.attachments
        )
        trigger_image_atts = [a for a in attachments if is_vision_image_attachment(a)]
        data_atts = [a for a in attachments if not is_vision_image_attachment(a)]
        target = thread or message.channel

        synthetic_prefix = build_attachment_url_prefix(data_atts)

        # --- Build user message (XML history for thread mentions, raw for channel) ---
        # Continuation turns (reused session with a watermark) use delta context
        # (messages since the watermark). First turns use the full snapshot.
        # History images are intentionally NOT inlined as vision blocks: MA
        # persists and replays every image block across turns, so re-sending the
        # thread's history images each turn compounds the per-request image count
        # past the API's 20-image threshold, which drops its per-image dimension
        # limit from 8000px to 2000px and 400s ordinary photos. History images
        # already carry url= in their <attachment/> XML, so the agent can curl +
        # read them on demand instead. (build_context_xml still reports the
        # history image attachments; we just don't download them here.)
        if isinstance(message.channel, discord.Thread):
            if prepared.reused and prepared.watermark is not None:
                user_message, _ = await build_delta_xml(
                    thread,
                    trigger=message,
                    after_message_id=int(prepared.watermark),
                    bot_user_id=self.user.id if self.user else None,
                    bot_display_name=discord_settings.bot_display_name,
                    is_admin=is_admin,
                    unprompted=unprompted,
                    key_names=key_names,
                )
            else:
                user_message, _ = await build_context_xml(
                    thread,
                    trigger=message,
                    limit=100,
                    bot_user_id=self.user.id if self.user else None,
                    bot_display_name=discord_settings.bot_display_name,
                    is_admin=is_admin,
                    unprompted=unprompted,
                    key_names=key_names,
                )
        else:
            if content_override is not None:
                user_message = content_override
            elif isinstance(message.channel, discord.TextChannel):
                user_message, _ = await build_channel_context_xml(
                    message.channel,
                    trigger=message,
                    thread=thread,
                    bot_user_id=self.user.id if self.user else None,
                    bot_display_name=discord_settings.bot_display_name,
                    is_admin=is_admin,
                    key_names=key_names,
                )
            else:
                # Forum/voice channels: fall back to raw message content
                user_message = message.content

        # Inline only the trigger message's images as base64 vision blocks.
        # Images we can't inline (too large, too many, unsupported, fetch error)
        # are not dropped — their signed CDN URL is surfaced below so the agent
        # can still reach them (curl + read to view, or pass to an external API).
        downloaded_blocks, images_skipped = await download_as_image_blocks(trigger_image_atts)
        skipped_ids = {att.id for att, _ in images_skipped}
        inlined_image_atts = [a for a in trigger_image_atts if a.id not in skipped_ids]
        image_blocks = downloaded_blocks or None

        # Surface a signed-CDN-URL line for every trigger image: inlined images
        # get a handle they can forward to external APIs; skipped images get the
        # only path left for the agent to reach them.
        synthetic_prefix = "\n".join(
            part
            for part in (
                synthetic_prefix,
                build_image_url_prefix(inlined_image_atts),
                build_skipped_image_prefix(images_skipped),
            )
            if part
        )
        if synthetic_prefix:
            user_message = synthetic_prefix + "\n" + user_message

        if images_skipped:
            await turn_send(
                "Some images couldn't be inlined — I've linked them for the agent to "
                "fetch instead: "
                + ", ".join(f"`{att.filename}` ({r})" for att, r in images_skipped)
            )

        # --- Run the turn (D-08/D-09/D-10: run_prepared_turn owns the driver
        # call and the one-shot dead-session recovery cycle). ---
        # lifecycle_holder tracks whichever DiscordTurnLifecycle actually
        # completed the turn -- recovery_lifecycle rebuilds a fresh one against
        # the recreated session, and the watermark write below must read
        # final_message_id off THAT lifecycle, not the pre-recovery one.
        lifecycle_holder: list[DiscordTurnLifecycle] = [lifecycle]

        async def _reseed_user_message() -> str:
            """Full history re-seed for the recreated session (dead-session recovery).

            ``omit_oversized_image_urls=True`` is what stops recovery from
            recreating the failure it is recovering from. An oversized image in
            the history is the most likely reason the previous session died:
            the agent curls the URL, ``read``s it at full size, and MA
            terminates the session. Reseeding that same URL into the fresh
            session just repeats it, so the thread recovers, dies, recovers,
            dies -- each cycle billing a full agentic run. Observed on staging
            thread 1535185295245582356: two consecutive recoveries burned 243k
            then 62k input tokens and both ended terminated. Withholding the
            URL (rather than only warning about it, which the model may ignore)
            is what actually breaks the loop.
            """
            full_message, _ = await build_context_xml(
                thread,
                trigger=message,
                limit=100,
                bot_user_id=self.user.id if self.user else None,
                bot_display_name=discord_settings.bot_display_name,
                omit_oversized_image_urls=True,
                is_admin=is_admin,
                unprompted=unprompted,
                key_names=key_names,
            )
            if synthetic_prefix:
                full_message = synthetic_prefix + "\n" + full_message
            async with self.runtime.sessionmaker() as session:
                recovery_origin = await get_active_origin(
                    session,
                    origin_id=origin.id,
                    tenant_id=tenant_id,
                    account_id=admission.account_id,
                    platform="discord",
                    now=datetime.now(UTC),
                )
            if recovery_origin is None:
                raise DaimonError(
                    "This turn's setup context expired. Mention me again to continue."
                )
            return (
                render_turn_origin(
                    recovery_origin,
                    responder_handle=_responder_handle(self.runtime.settings),
                    session_state=session_state,
                    is_channel_admin=is_channel_admin,
                )
                + "\n"
                + full_message
            )

        def _recovery_lifecycle(cancel_event: asyncio.Event) -> TurnLifecycle:
            new_lifecycle = DiscordTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                tenant_id=tenant_id,
                budget_channel_id=admission.budget_channel_id,
                requester_id=message.author.id,
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                trigger_message=message,
                render_tables=self.runtime.settings.table_rendering.get(tenant_id, False) is True,
                send=turn_send,
                edit=_edit_message,
                delete=_delete_message,
                agent_name=agent.name,
                fallback_active=lambda: transport.fallback_used,
                model_id=agent.model.id,
                cancel_view=CancelView(
                    allowed_user_id=message.author.id,
                    cancel=cancel_event,
                    turn_id=turn_card_intent.id,
                ),
                # Take over the failed attempt's message so its upstream-error
                # embed is edited into this turn's answer rather than left
                # standing next to a second, successful message.
                adopt_message_ref=lifecycle.message_ref,
                unprompted=unprompted,
                on_replacement=lambda msg: recorder.message(
                    thread, msg, turn_card_intent_id=turn_card_intent.id
                ),
            )
            # The replacement summary belongs to the turn, not to the lifecycle
            # object that happens to render it -- a recovery cycle swaps the
            # lifecycle and would otherwise drop it.
            new_lifecycle.answer_prefix = lifecycle.answer_prefix
            lifecycle_holder[0] = new_lifecycle
            return new_lifecycle

        log.info(
            "turn.started",
            guild_id=guild_id,
            channel_id=parent_channel_id,
            thread_id=thread.id,
            session_id=prepared.ma_session_id,
        )
        is_channel_admin = await holds_current_channel_admin_grant(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            platform="discord",
            parent_channel_id=parent_channel_id,
            role=role,
        )
        outcome: RunOutcome | None = None
        archive_after = False
        try:
            async with turn_origin(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                account_id=admission.account_id,
                platform="discord",
                parent_channel_id=parent_channel_id,
                thread_id=str(thread.id),
                responder_ma_agent_id=str(agent.id),
                responder_name=admission.config.agent_name or agent.name,
                configuration_target_ma_agent_id=admission.config.configuration_target_ma_agent_id,
                configuration_target_name=admission.config.configuration_target_name,
                role=role,
                is_setup=admission.config.thread_binding_kind == "setup",
            ) as origin:
                outcome = await run_prepared_turn(
                    self.runtime.turn_deps,
                    prepared,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=str(thread.id),
                    external_user_id=str(message.author.id),
                    user_message=(
                        render_turn_origin(
                            origin,
                            responder_handle=_responder_handle(self.runtime.settings),
                            session_state=session_state,
                            is_channel_admin=is_channel_admin,
                        )
                        + "\n"
                        + user_message
                    ),
                    lifecycle=lifecycle,
                    cancel=cancel,
                    reseed_user_message=_reseed_user_message,
                    recovery_lifecycle=_recovery_lifecycle,
                    image_blocks=image_blocks,
                    render_interval_s=2.0,
                    deadline=turn_deadline_at,
                    confirm_write=discord_confirmation_hook(thread),
                )
                archive_after = await self._archive_requested(origin.id)
        finally:
            # Runs on any exception, not just the happy path: whatever else
            # went wrong, the thread must not be left holding its active_turn
            # marker. Both ids are cleared -- recovery moves the turn to a new
            # mapping row and leaves the marker behind on the old one, which
            # is still pointing at this same message. A ceiling breach is not
            # a special case here: run_prepared_turn already marked the
            # active mapping dead and returns a normal RunOutcome, so it takes
            # this same path.
            _done_ids = {prepared.mapping_id}
            if outcome is not None:
                _done_ids.add(outcome.mapping_id)
            for _done_id in _done_ids - {None}:
                if _done_id is None:  # pragma: no cover - set difference guarantees this
                    continue
                async with self.runtime.sessionmaker() as _ct_session:
                    await clear_active_turn(_ct_session, id=_done_id)
                    await _ct_session.commit()
            if outcome is not None and not lifecycle_holder[0].card_discard_failed:
                await retire_terminal_turn_card(
                    self.runtime.sessionmaker,
                    intent_id=turn_card_intent.id,
                    expected_message_id=lifecycle_holder[0].card_message_id,
                    allow_prepared_without_message=not lifecycle_holder[0].first_post_attempted,
                )

        # Reached only on the non-exceptional path -- any raise inside the try
        # propagates past this point once the finally block above has run.
        assert outcome is not None
        log_anthropic_overload(
            outcome.state.error.cause if outcome.state.error is not None else None,
            tenant_id=tenant_id,
            path="mention",
            alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
        )
        state = outcome.state
        mapping_id = outcome.mapping_id
        final_lifecycle = lifecycle_holder[0]

        # The recovery cycle inside run_prepared_turn only learns a session was
        # lost mid-call, after the turn has already run -- so unlike the
        # planned-replacement summary above, this notice can only be posted
        # here, once `outcome.continuity` is known. Posted regardless of
        # whether the recovered turn itself then answered or errored: the
        # person needs to know the workspace was lost either way.
        if outcome.continuity.state == "replaced_after_loss":
            loss_kind: Literal["transcript", "history"] = (
                "transcript" if outcome.continuity.transfer_kind == "transcript" else "history"
            )
            loss_notice = render_unexpected_loss(loss_kind)
            # Edited in above the answer rather than sent under it, for the
            # same ordering reason as the planned summary above. Falls back to
            # a message when there is no answer to sit above (a tool-only or
            # failed turn) or it will not fit.
            if not await final_lifecycle.prepend_revealed_answer(loss_notice):
                await turn_send(loss_notice)
        if replacement_summary is not None and not final_lifecycle.answer_prefix_applied:
            # The turn produced no answer to carry the summary (tool-only,
            # cancelled, or failed). The person still has to be told what the
            # replacement carried across, so it goes out on its own.
            await turn_send(replacement_summary)

        if state.error is not None:
            log.warning(
                "turn.error",
                thread_id=thread.id,
                session_id=outcome.ma_session_id,
                kind=state.error.kind,
            )
        else:
            log.info("turn.completed", thread_id=thread.id, session_id=outcome.ma_session_id)
            # --- Write watermark (bot's reply message id) ---
            if mapping_id is not None and final_lifecycle.final_message_id is not None:
                async with self.runtime.sessionmaker() as _wm_session:
                    await update_watermark(
                        _wm_session,
                        id=mapping_id,
                        watermark_message_id=final_lifecycle.final_message_id,
                    )
                    await _wm_session.commit()
            # The lifecycle flag below excludes a cancelled turn, which also
            # reaches this branch with a non-None final_message_id -- seeding
            # the vote affordance under a cancellation notice is exactly what
            # this guard prevents. Not gated on mapping_id, which is about
            # session mapping, not whether the turn actually answered.
            if final_lifecycle.was_answered and final_lifecycle.final_message_id is not None:
                await seed_feedback_reactions(thread, message_id=final_lifecycle.final_message_id)
            # A change queued behind this turn (it was already running when the
            # change landed) applies at the caller's NEXT message, not this one
            # -- said only after the answer, so it never reads as a caveat on
            # work that already finished.
            if prepared.continuity.pending:
                await turn_send(render_current_work_must_finish(agent.name, handoff=False))
            # Any task handed to another agent in THIS thread, with work to
            # continue, gets its first turn dispatched now -- still inside this
            # turn's concurrency guard, so nothing else can land in the thread
            # first.
            await self._dispatch_continuations(
                tenant_id=tenant_id, thread=thread, guild_id=guild_id
            )
        self._schedule_output_sweep(outcome, thread=thread, tenant_id=tenant_id)
        if archive_after:
            await self._archive_after_outputs(outcome, thread)

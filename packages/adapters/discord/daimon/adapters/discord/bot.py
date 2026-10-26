"""DaimonBot -- Discord adapter event-driven controller."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, Literal

import aiohttp
import anthropic as _anthropic
import sentry_sdk
import structlog
import structlog.contextvars
from daimon.adapters.discord import theme
from daimon.adapters.discord.agent_setup.stale_picture import StalePictureChangeButton
from daimon.adapters.discord.attachments import build_attachment_url_prefix
from daimon.adapters.discord.budget_notice import with_budget_notifier
from daimon.adapters.discord.button_message import ButtonMessage
from daimon.adapters.discord.checks import admin_caller, is_member_guild_admin, member_role_ids
from daimon.adapters.discord.context import (
    build_channel_context_xml,
    build_context_xml,
    build_delta_xml,
)
from daimon.adapters.discord.continuation_dispatch import dispatch_pending_continuations
from daimon.adapters.discord.errors import (
    NOT_SET_UP_NOTICE,
    SETUP_OUT_OF_DATE_NOTICE,
    bound_request_id,
    generate_request_id,
    render_error,
    render_wake_refused,
)
from daimon.adapters.discord.feedback_seed import seed_feedback_reactions
from daimon.adapters.discord.gating import (
    HintCooldown,
    is_participation_candidate,
    is_plain_text_mention_candidate,
    is_unmentioned_reply_hint_candidate,
    should_process_message,
)
from daimon.adapters.discord.lifecycle import (
    DiscordTurnLifecycle,
    EditFn,
    SendFn,
    drain_retained_card_tasks,
    edit_recovered_card,
)
from daimon.adapters.discord.membership import GatewayMembershipResolver, member_facts
from daimon.adapters.discord.names import remember_guild_user
from daimon.adapters.discord.output_delivery import deliver_session_outputs
from daimon.adapters.discord.permissions import check_missing_permissions
from daimon.adapters.discord.post_transport import (
    DiscordPostTransport,
    clear_webhook_backoff,
    known_webhook_ids,
)
from daimon.adapters.discord.posted_controls import edit_posted_card
from daimon.adapters.discord.provider_actions import (
    discord_provider_action_hook,
    provider_action_options,
)
from daimon.adapters.discord.queue_card import queue_card_ticket, tenant_slot_waiter
from daimon.adapters.discord.routine_delivery import make_discord_routine_poster
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.startup_replay import completed_orphan_text
from daimon.adapters.discord.tables import tables_enabled
from daimon.adapters.discord.thread_gone import (
    ThreadStops,
    TurnThreadWatch,
    stop_reason_of,
)
from daimon.adapters.discord.thread_naming import generate_thread_name
from daimon.adapters.discord.thread_participation import ThreadParticipant
from daimon.adapters.discord.thread_send import safe_thread_send
from daimon.adapters.discord.tool_confirmation import discord_confirmation_hook
from daimon.adapters.discord.turn_card_recovery import (
    UnrecoverableTurnCardError,
    expire_unrecoverable_turn_card,
    is_definite_recovery_failure,
    post_initial_turn_card,
    reconcile_turn_card_intent,
    retire_terminal_turn_card,
)
from daimon.adapters.discord.turn_posts import (
    TurnPostRecorder,
    archive_thread_quietly,
    record_replacement_post,
)
from daimon.adapters.discord.typed_stop import (
    ALREADY_STOPPING,
    STOPPED,
    TypedStops,
    is_stop_request,
)
from daimon.adapters.discord.views import STOP_NOT_ALLOWED, CancelView
from daimon.adapters.discord.vision import (
    build_image_url_prefix,
    build_skipped_image_prefix,
    download_as_image_blocks,
    is_vision_image_attachment,
)
from daimon.adapters.discord.watch_access import require_watch_thread_access
from daimon.core.agent_identity import AgentIdentity, identity_enabled_for, resolve_agent_identity
from daimon.core.agent_pins import agent_pin_names
from daimon.core.anthropic_guard import guard_selfhosted_channel
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.billing_panel import fmt_usd
from daimon.core.channel_budget_notice import drain_budget_notices
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.continuity.continuation import (
    ContinuationDecision,
    check_wake_responder,
    continuation_attended,
    load_asking_agent_id,
)
from daimon.core.continuity.messages import (
    render_access_changed_try_again,
    render_current_work_must_finish,
    render_preparation_failed,
    render_replacement_summary,
    render_unexpected_loss,
)
from daimon.core.continuity.wakes import (
    FrozenWorkerBatch,
    WakeThread,
    WorkerFinishedRootChanged,
    decide_worker_finished,
    require_worker_finished_root,
    run_wake_poller,
    skip_thread_wakes,
)
from daimon.core.continuity.watch_notices import poll_watch_notices
from daimon.core.credential_form_expiry import run_credential_form_expiry_poller
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.defaults.provisioning import provision_tenant, reconcile_tenant_defaults
from daimon.core.defaults.report import compose_failure_reason
from daimon.core.errors import DaimonError, TurnError, UserFacingError
from daimon.core.fleet_run_end import run_run_end_poller
from daimon.core.github_connect_delivery import run_connect_notice_poller
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.github_request_expiry import run_request_expiry_poller
from daimon.core.ma import ORPHAN_INTERRUPT_TIMEOUT_S, interrupt_orphaned_session
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.ops_alerts import alert_ops
from daimon.core.output_delivery import NoticedFiles
from daimon.core.participation_gates import BATCH_MAX_MESSAGES, BATCH_MAX_QUIET_PERIODS
from daimon.core.permissions import channel_permissions
from daimon.core.platform_words import words_for
from daimon.core.posted_controls.lifecycle import drain_card_edits
from daimon.core.routine_delivery import resolve_routine_identity, run_delivery_poller
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.agent_posts import get_post
from daimon.core.stores.discord_message_admissions import (
    claim_message,
    finish_messages,
    has_released_messages,
    prune_old_messages,
    replay_channels,
    unposted_thread_for_message,
)
from daimon.core.stores.domain import (
    CredentialRequestRow,
    Role,
    RoutineRow,
    TaskContinuationRow,
    TenantRow,
    ThreadSessionRow,
    TurnCardIntentRow,
    WatchRow,
)
from daimon.core.stores.github_access_requests import AccessRequest
from daimon.core.stores.github_connect_notices import ConnectNotice
from daimon.core.stores.pending_skill_adds import is_affirmative
from daimon.core.stores.promo_codes import has_redeemable_promo_code
from daimon.core.stores.tenant_ledger import get_prepaid_balance
from daimon.core.stores.tenants import (
    get_plain_text_mentions,
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
    list_live_thread_sessions,
    list_orphaned_turns,
    lock_orphaned_turn,
    mark_dead,
    mark_turn_active,
    update_watermark,
)
from daimon.core.stores.turn_card_intents import (
    get_turn_card_intent,
    list_recoverable_turn_card_intents,
    record_turn_card_recovery_failure,
)
from daimon.core.stores.turn_origins import get_active_origin, thread_archive_requested
from daimon.core.thread_naming import (
    THREAD_NAME_MAX_CHARS,
    fallback_thread_name,
    naming_text,
    strip_mentions,
)
from daimon.core.thread_participation import ParticipationMode
from daimon.core.turn.admission import AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.bookkeeping import release_turn_markers
from daimon.core.turn.ceiling import turn_deadline
from daimon.core.turn.errors import (
    AdmissionDenialReason,
    BackendRefusalCause,
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.outcomes import observe_turn, record_refusal
from daimon.core.turn.output import OutputAuthorization
from daimon.core.turn.prepare import PreparedTurn, admitted_session_scope, bind_session
from daimon.core.turn.protection import (
    ConfirmedProtection,
    ProtectionState,
    confirmed_protection,
    protection_state,
)
from daimon.core.turn.run import RunOutcome, run_prepared_turn
from daimon.core.turn.slots import (
    QUEUE_TIMED_OUT_TEXT,
    holding,
    release_turn_slot,
    wait_for_slot,
)
from daimon.core.turn.state import ToolUseBlock
from daimon.core.turn.thread_queue import (
    QUEUE_NOTICE,
    HeldThreads,
    ThreadQueue,
    claim_dispatch,
    dispatch_and_drain,
    group_by_author,
    release_thread,
)
from daimon.core.turn_hold import RequesterOrg
from daimon.core.turn_keys import list_mounted_key_names
from daimon.core.turn_origin import (
    HandoffNotice,
    SessionState,
    render_turn_origin,
    turn_origin,
)
from daimon.core.turn_queue import TurnQueue
from mux.contracts.ids import Scope
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles import get_profile
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord
from discord.ext import commands
from discord.http import HTTPClient

if TYPE_CHECKING:
    from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCard
    from daimon.core.fleet_run_end import EndOutcome
    from daimon.core.stores.agent_posts import AgentPostRow
    from daimon.core.stores.fleet import EndNotice

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
TENANT_CAP_NOTICE = (
    f"This {words_for('discord').space} has too many chats at once right now"
    " — try again in a moment."
)
THREAD_OPEN_FAILED_NOTICE = "Couldn't open a thread. @mention Daimon again."

# Grace window for active turns. The documented worker stop grace is 90s,
# leaving time for bounded cleanup and card edits after this 60s window.
_DRAIN_GRACE_S: float = 60.0
_DRAIN_CLEANUP_S: float = 5.0
_DRAIN_CARD_EDITS_S: float = 5.0
#: Budget-limit DMs still sending at drain end. Grace 60 s + 10 + 5 + 5 stays
#: under the worker's 90 s stop grace, so close() runs before Docker kills.
_DRAIN_NOTICES_S: float = 10.0

# Bounded concurrency for the on_ready re-seed sweep. Each tenant reconcile
# issues roughly two dozen Skills API calls (a read per seeded skill, plus an
# upload per skill that changed), and that API is rate limited per ORG at 100
# requests/minute -- shared across every deployment on the operator's key. At 4
# a cold sweep of 16 tenants exceeded it and the promote of 2026-08-20 landed
# only 5 of 16 tenants' skills before 429ing; the agent reconcile then failed
# attaching skills the sweep had never created.
_SWEEP_CONCURRENCY = 2
# What a tenant reconcile returns: True on success, False on failure, or the
# task already reconciling that tenant when the in-flight guard skipped this
# call (None only if that task could not be identified).
type _ReconcileOutcome = bool | asyncio.Task[Any] | None
_TURN_CARD_RECOVERY_CONCURRENCY = 4


THREAD_OPENING_REACTION = "⌛"


def _positive_discord_id(value: object) -> bool:
    return isinstance(value, int) and value > 0


class _WritersNoneNoticeLimiter:
    """Bound attempts before I/O, on the single posting owner; not a distributed lease."""

    MAX_ENTRIES = 2048

    def __init__(self) -> None:
        self._until: dict[tuple[str, ...], float] = {}
        self._attempts: deque[float] = deque()

    def reserve(
        self, *, tenant: str, channel: str, requester: str, message: str, now: float
    ) -> bool:
        # No await: concurrent handlers reserve before either can perform I/O.
        self._until = {key: expiry for key, expiry in self._until.items() if expiry > now}
        while self._attempts and self._attempts[0] <= now - 60:
            self._attempts.popleft()
        limits = (
            (("requester", tenant, channel, requester), 300),
            (("channel", tenant, channel), 30),
            (("message", tenant, message), 300),
        )
        if (
            len(self._attempts) >= 10
            or any(key in self._until for key, _ in limits)
            or len(self._until) + len(limits) > self.MAX_ENTRIES
        ):
            return False
        for key, ttl in limits:
            self._until[key] = now + ttl
        self._attempts.append(now)
        return True


async def _open_thread_with_notice(
    message: discord.Message,
    opening: Coroutine[Any, Any, discord.Thread],
    *,
    after_s: float,
    acknowledged: bool = False,
) -> discord.Thread:
    """Keep an opening mention visibly acknowledged while naming or Discord creation waits.

    The acknowledgment is a reaction on the mention, never a channel message: a
    channel-level "your chat is ready" post outlives the wait and reads as a stray
    reply. A failed creation is answered with one plain reply before it propagates
    here, so the mention is never left without an answer."""
    task = asyncio.create_task(opening)
    reacted = False
    try:
        try:
            if after_s > 0:
                return await asyncio.wait_for(asyncio.shield(task), timeout=after_s)
        except TimeoutError:
            pass
        if not acknowledged:
            try:
                await message.add_reaction(THREAD_OPENING_REACTION)
                reacted = True
            except discord.HTTPException as exc:
                log.warning("discord.thread_open_notice_failed", error=str(exc))
        return await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if reacted and message.guild is not None:
            try:
                await message.remove_reaction(THREAD_OPENING_REACTION, message.guild.me)
            except Exception as exc:  # best effort: a stuck ⌛ never fails an opened thread
                log.warning("discord.thread_open_notice_clear_failed", error=str(exc))


def _capture_turn_error(
    exc: BaseException, *, rid: str, tenant_id: uuid.UUID, guild_id: str
) -> None:
    """Report a failed mention turn to Sentry with the tags that find it again."""
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("rid", rid)
        scope.set_tag("tenant_id", str(tenant_id))
        scope.set_tag("guild_id", guild_id)
        sentry_sdk.capture_exception(exc)


class _ThreadOpenFailed(Exception):
    """The opening mention's thread could not be created; the person was told.

    Raised from the creation error, so the turn boundary logs it without
    posting a second error message.
    """


async def _explain_thread_open_failure(message: discord.Message) -> None:
    """The one reply a mention gets when its thread could not be created."""
    try:
        await message.reply(THREAD_OPEN_FAILED_NOTICE, mention_author=False)
    except discord.HTTPException as exc:
        log.warning("discord.thread_open_failed_notice_failed", error=str(exc))


def _resolve_bot_display_name(settings: Settings) -> str:
    """Read the operator-configured bot presented name.

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
    """Distinct from the MAResolverMissError "out of date" message so a
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


DISCORD_REFUSAL_NOUNS = RefusalNouns.for_platform("discord")


def admission_refusal_message(
    reason: AdmissionDenialReason,
    settings: Settings,
    *,
    in_dm: bool = False,
    cause: BackendRefusalCause | None = None,
    provider: str | None = None,
) -> str:
    """The shared admission refusal in Discord's nouns, naming this deployment's bot."""
    return admission_refusal_text(
        reason,
        DISCORD_REFUSAL_NOUNS,
        bot_name=_resolve_bot_display_name(settings),
        in_dm=in_dm,
        cause=cause,
        provider=provider,
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


def _log_bg_task_exception(task: asyncio.Task[Any]) -> None:
    """Done-callback: surface escaped background-task exceptions immediately
    instead of asyncio's GC-time 'Task exception was never retrieved'."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("bg_task_failed", task_name=task.get_name(), exc_info=exc)


def _build_welcome_embed() -> discord.Embed:
    """The first message of an install, edited into the ready state. Pure, no I/O."""
    return discord.Embed(
        title="⏳ Setting up…",
        description="Getting ready. This takes a moment.",
        color=_EMBED_COLOR,
    )


def _build_ready_embed(
    bot_display_name: str = "daimon",
    *,
    promo_codes: bool = False,
    credit: Decimal | None = None,
) -> discord.Embed:
    """What Daimon does, one thing to try and the setup phrase. Pure, no I/O.

    ``promo_codes``: some code is redeemable now, so point admins at /billing.
    ``credit``: the tenant's balance when it is above zero, named so the first
    footer showing what is left is no surprise.
    """
    lines = [
        "I answer questions about data and write code, charts and notebooks.",
        f"Try it: mention `@{bot_display_name}` with a question, "
        "or attach a CSV and ask me to summarise it.",
        'To set things up, mention me and say "help me set up".',
    ]
    if credit is not None:
        lines.append(f"You have {fmt_usd(credit)} of credit. Each answer shows what's left.")
    if promo_codes:
        lines.append("Have a promo code? Redeem it in `/billing`.")
    return discord.Embed(
        title="👋 Daimon is ready", description="\n".join(lines), color=theme.COLOR_GREEN
    )


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


@dataclass
class _TurnFailureSurface:
    """Where a turn that raises shows its error, filled in as the turn opens them.

    ``thread`` is the turn's thread once it exists, so the error lands there and
    not in the parent channel. ``end_card`` turns the turn's status card into
    the error while it is still a card, so no Stop button is left spinning; it
    is cleared once the run returns and the card has become the answer.
    """

    thread: discord.Thread | None = None
    end_card: Callable[[str], Awaitable[None]] | None = None


# Shared with every adapter that follows threads (see `daimon.core.participation_gates`).
_PARTICIPATION_BATCH_MAX_MESSAGES: Final[int] = BATCH_MAX_MESSAGES
_PARTICIPATION_BATCH_MAX_QUIET_PERIODS: Final[int] = BATCH_MAX_QUIET_PERIODS

#: The reaction on an unmentioned reply in a thread daimon opened: only a
#: mention starts a turn. A reaction, not a message, so the hint stays quiet.
UNMENTIONED_REPLY_HINT_EMOJI: Final[str] = "🔔"
PROTECTED_CHANNEL_REACTION: Final[str] = "🔒"


def protected_channel_notice(protection: ConfirmedProtection, *, channel_id: str) -> str:
    """The DM telling a mentioner why a Who-can-post-Nobody place stayed quiet.

    Names the rule that actually matched: a channel rule is changed from the
    Permissions screen by a server admin; a category rule only from the CLI.
    """
    if protection.scope == "category":
        return (
            f"Daimon can't reply in <#{channel_id}>: the **Who can post** rule on its "
            "category is **Nobody**. Whoever runs Daimon for this server can change it."
        )
    return (
        f"Daimon can't reply in <#{protection.rule_id}>: its **Who can post** rule is "
        "**Nobody**. A server admin can change it in `/agent-setup` → "
        "**Who answers where** → **Permissions**."
    )


async def requester_role(guild: discord.Guild, external_user_id: str) -> tuple[Role, list[str]]:
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


# Existing continuation callers retain the same live member lookup.
_requester_role = requester_role


class DaimonBot(commands.Bot):
    """Discord bot process. Slash commands + turn pipeline."""

    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        intents: discord.Intents,
        http_client: HTTPClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(command_prefix=[], intents=intents)  # type: ignore[arg-type]  # discord.py expects Iterable but [] is valid
        # Explicit in-process ingress only; normal gateway/HTTP and time remain
        # untouched when neither dependency is supplied.
        if http_client is not None:
            self.http = http_client
            self._connection.http = http_client
        self._ingress_clock = clock
        self.runtime = with_budget_notifier(
            runtime, self.open_member_dm, self.is_closed, client=self
        )
        self.membership_resolver = GatewayMembershipResolver(self)
        self.runtime = replace(
            self.runtime,
            turn_deps=replace(self.runtime.turn_deps, membership_resolver=self.membership_resolver),
        )
        # The SDK routes slash commands, buttons, and modals before dispatching
        # on_interaction, so filter their gateway payload before those callbacks.
        parse_interaction = self._connection.parsers["INTERACTION_CREATE"]

        def parse_allowed_interaction(data: dict[str, Any]) -> None:
            guild_id = data.get("guild_id")
            if self._allows_guild(int(guild_id) if guild_id is not None else None):
                parse_interaction(data)

        self._connection.parsers["INTERACTION_CREATE"] = parse_allowed_interaction

        # discord.py's on_member_update only fires for SDK-cached members.
        # Invalidate raw updates too: REST-fetched DM members need not be cached.
        parse_member_update = self._connection.parsers["GUILD_MEMBER_UPDATE"]

        def invalidate_member_update(data: dict[str, Any]) -> None:
            guild_id = str(data["guild_id"])
            tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
            self.membership_resolver.invalidate(tenant_id, guild_id, str(data["user"]["id"]))
            parse_member_update(data)

        self._connection.parsers["GUILD_MEMBER_UPDATE"] = invalidate_member_update
        # Per-thread concurrency state. _processing: thread IDs with an active turn.
        # _pending: mentions queued behind an in-flight turn for that thread.
        # Drained after the current turn finishes into a single composite follow-up
        # turn so the user doesn't lose messages they fired while the bot was busy.
        self._processing: HeldThreads[int] = HeldThreads()
        self._processing_tasks: dict[int, asyncio.Task[object]] = {}
        # The running turn of each thread, for a typed "stop" (`_typed_stop`).
        self._typed_stops = TypedStops()
        self._pending: dict[int, list[discord.Message]] = {}
        self._queued_reactions: set[int] = set()
        # Threads whose running turn lost its thread (deleted) or its right to write
        # to it (archived), so the card lifecycle and the bot agree on one answer.
        self._thread_stops = ThreadStops()
        # Continuation dispatches skipped because the thread was processing,
        # keyed by thread id: re-run when the thread is released (see
        # `_release_thread`). Last writer wins; a dispatch reads every pending
        # row for the thread, so one entry is enough.
        self._deferred_dispatch: dict[int, tuple[uuid.UUID, discord.Thread, str]] = {}
        # Turn slots under the per-tenant cap (SCALE-01) and the optional global
        # cap, with the queue a turn waits in when either is full. A ticket is
        # claimed at admission and released in a `holding` block that brackets
        # the whole drain loop, so the slot is always returned.
        global_cap = (
            runtime.settings.discord.max_concurrent_turns
            if runtime.settings.discord is not None
            else None
        )
        self.turn_queue = TurnQueue.from_settings(
            runtime.settings.turn_queue,
            platform="discord",
            global_cap=global_cap if isinstance(global_cap, int) else None,
        )
        # Organic thread participation: per-thread quiet-period batches, keyed
        # by thread id. Populated only for threads that resolved to `on`.
        self._participation_pending: dict[int, _ParticipationBatch] = {}
        # Built on first use, not here: it needs `self.user`, which the gateway
        # only supplies once the bot is ready.
        self._participant: ThreadParticipant | None = None
        # Unmentioned-reply hint: thread id -> when it was last shown there.
        self._unmentioned_hint_cooldown = HintCooldown(
            runtime.settings.discord.unmentioned_reply_hint_cooldown_h * 3600
            if runtime.settings.discord is not None
            else 1.0
        )
        # Mentions replayed at startup: never told about a protected channel.
        self._replayed_message_ids: set[int] = set()
        # Messages already given the 🔒: never a second one (bounded, oldest out).
        self._protected_reacted_ids: OrderedDict[int, None] = OrderedDict()
        # Protected-channel notice: (user id, channel id) -> when last shown.
        self._protected_notice_cooldown = HintCooldown(
            runtime.settings.discord.protected_channel_notice_cooldown_min * 60
            if runtime.settings.discord is not None
            else 600.0
        )
        # In-flight seed guard: tenant_ids with a reconcile in progress, mapped
        # to the task running it so the boot reconcile watcher can wait on it.
        self._seeding: dict[uuid.UUID, asyncio.Task[Any] | None] = {}
        # The "setting up" message of each fresh install, by guild id, until the seed ends.
        self._welcome_messages: dict[int, discord.Message] = {}
        # Separate guard for unjoined-guild refreshes, so a rejoin during one
        # still runs its own seed and flips the tenant back to ready. The
        # reconcile's per-tenant advisory lock serializes the two.
        self._unjoined_reconciling: dict[uuid.UUID, asyncio.Task[Any] | None] = {}
        self._seed_sem = asyncio.Semaphore(_SWEEP_CONCURRENCY)
        # Gateway lifecycle callbacks run as separate tasks. Serialize only the
        # tenant provision/archive transitions so an earlier remove cannot
        # overwrite a later join's archive clear.
        self._guild_lifecycle_locks: dict[int, asyncio.Lock] = {}
        # Track spawned background tasks so they aren't GC'd; discard on done.
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._output_sweeps: dict[str, asyncio.Task[None]] = {}
        self._delivery_notice_thread_ids: set[int] = set()
        # Oversize outputs stay listed; each is announced once, not every sweep.
        self._noticed_output_files = NoticedFiles()
        # Drain flag — set by _drain_and_close on SIGTERM/SIGINT.
        # While True, on_message rejects new mentions; existing turns finish.
        self.draining: bool = False
        # One-shot guard for the orphaned-turn sweep. on_ready re-fires on every
        # full gateway reconnect, and a second run would reap the turns THIS
        # process is currently rendering.
        self._startup_replay_started = False
        # The same re-fire must not redo the all-tenant defaults reconcile and
        # command syncs: a later on_ready covers only what earlier ones did not
        # (a guild joined, rejoined or unavailable at the last ready, a
        # reconcile that has not succeeded yet, a sync that failed). Tenants
        # are added on success only.
        self._ready_reconciled_tenant_ids: set[uuid.UUID] = set()
        self._ready_reconciled_unjoined_tenant_ids: set[uuid.UUID] = set()
        self._ready_synced_guild_ids: set[int] = set()
        self._global_tree_synced = False
        # One-shot guard for the boot reconcile watcher: only the first
        # on_ready's reconcile tasks are watched, so the process logs exactly
        # one `discord.boot_reconcile_complete`/`_failed` event.
        self._boot_reconcile_watched = False
        # Inputs already finished by their card-intent transaction need no
        # cleanup DB await while the completed turn still owns its thread.
        self._committed_message_ids: set[str] = set()
        self._orphans_retired: bool = False
        self._orphan_sweep_lock = asyncio.Lock()
        self._orphan_retry_rows: list[ThreadSessionRow] = []
        self._boot_turn_card_intents: list[TurnCardIntentRow] | None = None
        self._turn_card_recovery_started: bool = False
        self._turn_card_periodic_started: bool = False
        self._turn_card_recovery_lock = asyncio.Lock()
        self._boot_card_recovery_task: asyncio.Task[None] | None = None
        self._live_turn_card_intent_ids: set[uuid.UUID] = set()
        # Set by setup_hook, which runs after login and before the gateway
        # connects, so it is set before any message or interaction can arrive.
        # From then on every turn entry passes the sweep barrier. Left unset
        # only by unit tests that build a bot without logging it in.
        self._orphan_recovery_armed: bool = False
        # Set by the process entrypoint, so the wake poller runs in the
        # deployed bot (started from setup_hook, once login has completed)
        # and never in a test that drives setup_hook directly.
        self.wake_poller_enabled: bool = False

    def _spawn[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Fire-and-forget a background task, tracked so it isn't GC'd."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(_log_bg_task_exception)
        return task

    def _track_processing_task(self, thread_id: int) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._processing_tasks[thread_id] = task

    def _track_live_turn_card(self, intent_id: uuid.UUID) -> None:
        """Exclude this process's turn from periodic recovery until its task ends."""
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - turns always run in tasks
            return
        self._live_turn_card_intent_ids.add(intent_id)
        task.add_done_callback(lambda _done: self._live_turn_card_intent_ids.discard(intent_id))

    def _forget_output_sweep(self, session_id: str, task: asyncio.Task[None]) -> None:
        if self._output_sweeps.get(session_id) is task:
            del self._output_sweeps[session_id]

    async def _routine_agent_names(self, row: RoutineRow) -> tuple[str | None, ...]:
        agent = await find_agent_by_daimon_tag(
            self.runtime.anthropic, tenant_id=row.tenant_id, name=row.agent_name
        )
        if agent is None or agent.id != row.agent_id:
            return ()
        return (row.agent_name, *agent_pin_names(agent.name, agent.metadata))

    async def _sweep_session_outputs(
        self,
        previous: asyncio.Task[None] | None,
        thread: discord.Thread,
        tenant_id: uuid.UUID,
        session_id: str,
        lifecycle: DiscordTurnLifecycle | None = None,
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
                noticed_files=self._noticed_output_files,
                turn_window=lifecycle.turn_window if lifecycle is not None else None,
                answer=lifecycle.answer_message if lifecycle is not None else None,
                requester_id=lifecycle.requester_id if lifecycle is not None else None,
                reply_to=lifecycle.files_reply_to(thread.id) if lifecycle is not None else None,
            )
        except Exception as exc:  # detached sweep must not fail the completed turn
            log.warning(
                "discord.output_delivery.unhandled_error",
                session_id=session_id,
                thread_id=thread.id,
                error=str(exc)[:300],
            )

    def fleet_card_for(
        self,
        *,
        thread: discord.abc.Messageable,
        requester_id: int,
        send: Callable[[], SendFn],
        edit: EditFn,
        run_id: uuid.UUID | None = None,
        turn_card_intent_id: uuid.UUID | None = None,
        recorded_post: AgentPostRow | None = None,
        on_replacement: Callable[[discord.Message], Awaitable[None]] | None = None,
        wake_id: uuid.UUID | None = None,
    ) -> DiscordFleetCard | None:
        factory = getattr(self.runtime, "fleet_card_factory", None)
        settings = getattr(self.runtime.settings, "fleet", None)
        if factory is None or getattr(settings, "enabled", False) is not True:
            return None
        return factory(
            thread,
            requester_id,
            send(),
            edit,
            run_id=run_id,
            turn_card_intent_id=turn_card_intent_id,
            recorded_post=recorded_post,
            on_replacement=on_replacement,
            wake_id=wake_id,
        )

    async def recover_fleet_card(
        self, interaction: discord.Interaction, *, run_id: uuid.UUID
    ) -> DiscordFleetCard | None:
        """Rebuild through the captured post and the same transport owner."""
        thread, message = interaction.channel, interaction.message
        if not isinstance(thread, discord.Thread) or message is None:
            return None
        return await self._fleet_card_from_post(thread, message, run_id=run_id)

    async def _live_fleet_card(
        self, thread: discord.Thread, *, author_id: int, is_admin: bool
    ) -> tuple[DiscordFleetCard, discord.Message] | Literal["not_allowed"] | None:
        """The card of this thread's still-running fleet run, rebuilt from its post.

        "not_allowed" when the author neither started it nor is a server admin: read
        from the stored card post, before any Discord call. The card's own control
        path still proves authority for the rest.
        """
        from daimon.core.stores import fleet as fleet_store

        if self.runtime.fleet_card_factory is None or not self.runtime.settings.fleet.enabled:
            return None
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(thread.guild.id))
        live = await fleet_store.live_run_card(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="discord",
            thread_id=str(thread.id),
        )
        if live is None:
            return None
        run_id, message_id = live
        async with self.runtime.sessionmaker() as session:
            post = await get_post(
                session,
                tenant_id=tenant_id,
                platform="discord",
                channel_id=str(thread.id),
                message_id=message_id,
            )
        if post is None:
            return None
        if post.requester_platform_user_id != str(author_id) and not is_admin:
            return "not_allowed"
        try:
            message = await thread.fetch_message(int(message_id))
        except discord.HTTPException:
            return None
        card = await self._fleet_card_from_post(thread, message, run_id=run_id)
        return None if card is None or card.run_id != run_id else (card, message)

    async def _fleet_card_from_post(
        self, thread: discord.Thread, message: discord.Message, *, run_id: uuid.UUID
    ) -> DiscordFleetCard | None:
        if self.runtime.fleet_card_factory is None or not self.runtime.settings.fleet.enabled:
            return None
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(thread.guild.id))
        async with self.runtime.sessionmaker() as session:
            post = await get_post(
                session,
                tenant_id=tenant_id,
                platform="discord",
                channel_id=str(thread.id),
                message_id=str(message.id),
            )
        if (
            post is None
            or post.source != "turn"
            or post.kind != "message"
            or post.turn_card_intent_id is None
            or post.parent_channel_id != str(thread.parent_id)
            or post.requester_platform_user_id is None
            or not post.requester_platform_user_id.isdecimal()
        ):
            return None
        transport = DiscordPostTransport(
            self,
            thread,
            name=message.author.name,
            avatar_url=None,
            builtin=message.webhook_id is None,
        )

        async def record(replacement: discord.Message) -> None:
            await record_replacement_post(self.runtime.sessionmaker, post, replacement)

        async def send(*args: Any, **kwargs: Any) -> discord.Message:  # noqa: ANN401
            sent = await transport.send(*args, **kwargs)
            await record(sent)
            return sent

        return self.fleet_card_for(
            thread=thread,
            requester_id=int(post.requester_platform_user_id),
            send=lambda: send,
            edit=transport.edit,
            run_id=run_id,
            turn_card_intent_id=post.turn_card_intent_id,
            recorded_post=post,
            on_replacement=record,
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
        self._track_processing_task(thread.id)
        try:
            await archive_thread_quietly(thread)
        finally:
            # Resumes a continuation deferred behind the claim, like any release.
            self._release_thread(thread.id)
            for queued in self._pending.pop(thread.id, []):
                self._spawn(self.on_message(queued, resume_admitted=True))

    def _schedule_output_sweep(
        self,
        outcome: RunOutcome,
        *,
        thread: discord.Thread,
        tenant_id: uuid.UUID,
        lifecycle: DiscordTurnLifecycle | None = None,
        selfhosted: bool = False,
    ) -> None:
        """Sweep the session's files onto the turn's answer, detached from the turn.

        The sweep takes whatever the session's listing holds once it settles.
        Accepted trade-off: a file the next turn writes while this sweep is
        still settling can land on this turn's answer. It is still delivered
        once and never stranded, which a per-turn cutoff could not promise.
        Sweeps stay serialized per session.
        """
        if lifecycle is not None and lifecycle.output_authorization is not None:
            # A watch's execution grant ends with the turn, before detached sweeps.
            return
        if selfhosted or not any(
            isinstance(block, ToolUseBlock) for block in outcome.state.content
        ):
            return
        session_id = outcome.ma_session_id
        previous = self._output_sweeps.get(session_id)
        task = self._spawn(
            self._sweep_session_outputs(previous, thread, tenant_id, session_id, lifecycle)
        )
        self._output_sweeps[session_id] = task
        task.add_done_callback(functools.partial(self._forget_output_sweep, session_id))

    def _running_turn_tasks(self) -> set[asyncio.Task[object]]:
        """Every task running a turn: per-thread claims plus `run_prepared_turn`'s registry.

        The registry covers turns inside `run_prepared_turn` and the whole
        accepted operation of entry points that never claim a thread here (a
        wizard submit or a direct message, from its slot wait to its cleanup);
        the claims cover mention-path turns before `run_prepared_turn`
        (queued, preparing). The calling task is excluded,
        so the drain never counts or cancels itself.
        """
        tasks = set(self.runtime.turn_deps.running_turns.tasks())
        tasks.update(
            task
            for thread_id in self._processing
            if (task := self._processing_tasks.get(thread_id)) is not None and not task.done()
        )
        if (current := asyncio.current_task()) is not None:
            tasks.discard(current)
        return tasks

    async def _drain_and_close(self) -> None:
        """Graceful shutdown drain.

        Flips draining=True so on_message rejects new mentions, then waits
        until no turn is running -- no claimed thread, no held turn slot and no
        turn inside `run_prepared_turn`, whichever entry point started it -- or
        the grace window elapses. Then it stops granting turn slots, cancels
        what is left, waits for their cleanup and retained card writes, and
        closes the client. A cancelled turn's provider session is left for
        recovery, not abandoned: see `docs/architecture.md`.
        """
        self.draining = True
        # Pending auto batches are unasked-for turns that have not started;
        # dropping them is free, whereas letting a timer fire mid-drain would
        # admit a new turn the drain is trying to stop.
        for thread_id in list(self._participation_pending):
            self._cancel_participation_batch(thread_id)
        log.info(
            "discord.draining",
            inflight_threads=len(self._processing),
            running_turns=len(self._running_turn_tasks()),
            turn_slots=self.turn_queue.in_flight(),
        )
        deadline = asyncio.get_running_loop().time() + _DRAIN_GRACE_S
        while (
            self._processing or self.turn_queue.in_flight() or self._running_turn_tasks()
        ) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        log.info(
            "discord.drain_complete",
            remaining=len(self._processing),
            running_turns=len(self._running_turn_tasks()),
            turn_slots=self.turn_queue.in_flight(),
        )
        # Before close(): it closes the HTTP session a notice still DMs through.
        notices = asyncio.ensure_future(drain_budget_notices())
        _, late = await asyncio.wait({notices}, timeout=_DRAIN_NOTICES_S)
        if late:
            log.warning("discord.drain_notices_timed_out", budget_s=_DRAIN_NOTICES_S)
        # Cancellation phase. Grant no further slot first, so releasing a
        # cancelled turn's slot cannot start a queued one this snapshot missed.
        # Cancelled turns leave their provider sessions recoverable: the thread
        # marker stays for the next process's orphan sweep, and a DM interrupts
        # its own session (`RunningTurns.is_drain_cancel`).
        self.turn_queue.stop_dispatch()
        self.runtime.turn_deps.running_turns.begin_shutdown()
        tasks = self._running_turn_tasks()
        if tasks:
            log.info("discord.drain_cancelling", turns=len(tasks))
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_DRAIN_CLEANUP_S)
            if pending:
                log.warning("discord.drain_turn_cleanup_pending", remaining=len(pending))

        # Both retained lifecycle writes and confirmation edits need the HTTP
        # client. Share one deadline so either kind cannot extend shutdown.
        deadline = asyncio.get_running_loop().time() + _DRAIN_CARD_EDITS_S
        remaining = await drain_card_edits(_DRAIN_CARD_EDITS_S)
        if remaining:
            log.warning("discord.drain_card_edits_pending", remaining=remaining)
        remaining_s = max(0.0, deadline - asyncio.get_running_loop().time())
        retained = await drain_retained_card_tasks(remaining_s)
        if retained:
            log.warning("discord.drain_retained_card_tasks_pending", remaining=retained)
        await self.close()

    async def _edit_expired_credential_card(self, row: CredentialRequestRow) -> bool:
        from daimon.adapters.discord.posted_controls import edit_expired_posted_card

        return await edit_expired_posted_card(self, row=row)

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
            self._spawn(
                run_request_expiry_poller(
                    self.runtime.sessionmaker,
                    platform="discord",
                    post=self._post_github_request_expiry,
                    should_stop=lambda: self.draining or self.is_closed(),
                    retire_form=lambda row: edit_posted_card(self, row=row, state="expired"),
                )
            )
            self._spawn(
                run_connect_notice_poller(
                    self.runtime.sessionmaker,
                    platform="discord",
                    deliver=self._send_connect_notice,
                    after_poll=self._poll_watch_import_notices,
                    should_stop=lambda: self.draining or self.is_closed(),
                )
            )
            if self.runtime.turn_deps.fleet_enabled:
                # An ended fleet no turn rendered the end of: settle its card, say so.
                self._spawn(
                    run_run_end_poller(
                        self.runtime.sessionmaker,
                        platform="discord",
                        post=self._post_fleet_run_end,
                        should_stop=lambda: self.draining or self.is_closed(),
                    )
                )
            # Post routine results to their destinations. Same
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
                        client=self,
                        output_client=self.runtime.anthropic,
                        membership_resolver=self.membership_resolver,
                        resolve_agent_names=self._routine_agent_names,
                        resolve_identity=lambda row, guild_id: resolve_routine_identity(
                            self.runtime.sessionmaker,
                            self.runtime.anthropic,
                            self.runtime.settings,
                            row=row,
                            platform="discord",
                            workspace_id=guild_id,
                            default_agent_name=self.runtime.deployment_default.agent_name,
                        ),
                    ),
                    should_stop=lambda: self.draining or self.is_closed(),
                )
            )
            if (
                self.runtime.settings.discord is not None
                and self.runtime.settings.discord.credential_form_expiry is True
            ):
                self._spawn(
                    run_credential_form_expiry_poller(
                        self.runtime.sessionmaker,
                        edit=self._edit_expired_credential_card,
                        should_stop=lambda: self.draining or self.is_closed(),
                    )
                )
        from daimon.adapters.discord.commands.agent_setup import AgentSetupCog
        from daimon.adapters.discord.commands.billing import BillingCog
        from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
        from daimon.adapters.discord.commands.github import GitHubCog
        from daimon.adapters.discord.commands.help import HelpCog
        from daimon.adapters.discord.commands.here import HereCog
        from daimon.adapters.discord.commands.leads import LeadsCog
        from daimon.adapters.discord.commands.memory import MemoryCog
        from daimon.adapters.discord.commands.model import ModelCog
        from daimon.adapters.discord.commands.privacy import PrivacyCog
        from daimon.adapters.discord.commands.routines import RoutinesCog
        from daimon.adapters.discord.feedback_reactions import FeedbackReactionCog

        await self.add_cog(HelpCog(self))
        await self.add_cog(HereCog(self))
        await self.add_cog(DirectMessageCog(self))
        await self.add_cog(AgentSetupCog(self))
        await self.add_cog(GitHubCog(self))
        await self.add_cog(RoutinesCog(self))
        await self.add_cog(LeadsCog(self))
        await self.add_cog(BillingCog(self))
        await self.add_cog(PrivacyCog(self))
        await self.add_cog(MemoryCog(self))
        await self.add_cog(ModelCog(self))
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
        if getattr(getattr(self.runtime.settings, "fleet", None), "enabled", False) is True:
            from daimon.adapters.discord.posted_controls.fleet import FleetControl

            self.add_dynamic_items(FleetControl)

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

        from daimon.adapters.discord.github_connect_button import GitHubConnectButton

        self.add_dynamic_items(GitHubConnectButton)

        # A setup panel from before picture uploads were turned off can still
        # show Change; the click gets the refusal instead of "interaction failed".
        self.add_dynamic_items(StalePictureChangeButton)

    async def _post_to_guild(
        self, guild: discord.Guild, embed: discord.Embed
    ) -> discord.Message | None:
        """Post an embed via the fallback chain: text channel → DM owner → skip.

        Returns the posted message, or None when nowhere took it."""
        channel = _pick_post_channel(guild)
        if channel is not None:
            try:
                return await channel.send(embed=embed)
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
                return await owner.send(embed=embed)
            except discord.HTTPException as exc:
                log.warning("guild_post_dm_failed", guild_id=str(guild.id), error=str(exc))
        log.warning("guild_post_skipped", guild_id=str(guild.id))
        return None

    async def _post_welcome(self, guild: discord.Guild) -> None:
        """Post the "setting up" message and keep it so the seed can finish it in place."""
        welcome = await self._post_to_guild(guild, _build_welcome_embed())
        if welcome is not None:
            self._welcome_messages[guild.id] = welcome

    async def _finish_welcome(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """Turn the "setting up" message into `embed`; post it fresh when there is
        no such message (a returning install) or it can no longer be edited."""
        welcome = self._welcome_messages.pop(guild.id, None)
        if welcome is not None:
            try:
                await welcome.edit(embed=embed)
                return
            except discord.HTTPException as exc:
                log.warning("guild_welcome_edit_failed", guild_id=str(guild.id), error=str(exc))
        await self._post_to_guild(guild, embed)

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

    async def _ready_embed(self, tenant_id: uuid.UUID) -> discord.Embed:
        """The ready message with the promo and credit lines that apply right now."""
        promo_codes = False
        credit: Decimal | None = None
        try:
            async with self.runtime.sessionmaker() as session:
                promo_codes = await has_redeemable_promo_code(session, now=datetime.now(UTC))
                # Prepaid only, as the turn footer: an operator-funded tenant has no balance.
                balance = await get_prepaid_balance(session, tenant_id=tenant_id)
            credit = balance if balance is not None and balance > 0 else None
        except SQLAlchemyError as exc:
            # The guild is ready; a failed lookup only drops those lines.
            log.warning("guild_seed_ready_lookup_failed", tenant_id=str(tenant_id), error=str(exc))
        return _build_ready_embed(
            _resolve_bot_display_name(self.runtime.settings),
            promo_codes=promo_codes,
            credit=credit,
        )

    async def _seed_tenant_defaults(
        self, *, tenant_id: uuid.UUID, guild: discord.Guild, was_ready: bool
    ) -> _ReconcileOutcome:
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

        Returns True when the reconcile succeeded, False when it failed (a
        failing report, a missing default agent or a caught exception) and the
        task already seeding the tenant when the in-flight guard skipped this
        call. Only the boot reconcile watcher reads it.
        """
        if tenant_id in self._seeding:
            return self._seeding[tenant_id]
        self._seeding[tenant_id] = asyncio.current_task()
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
                self._ready_reconciled_tenant_ids.add(tenant_id)
                if not was_ready:
                    await self._finish_welcome(guild, await self._ready_embed(tenant_id))
                return True
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
                    await self._finish_welcome(guild, _build_snag_embed())
                return False
        except (DaimonError, _anthropic.APIError, discord.HTTPException) as exc:
            log.warning("guild_seed_failed", tenant_id=str(tenant_id), error=str(exc))
            # Best-effort flip before posting so the tenant is never left wedged in
            # 'pending' — a raise inside this handler would NOT be caught by the
            # sibling except clause below and would skip the snag embed entirely.
            await self._flip_failed_best_effort(
                tenant_id, reason=f"{type(exc).__name__}: {exc}", was_ready=was_ready
            )
            if not was_ready:
                await self._finish_welcome(guild, _build_snag_embed())
            return False
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
                await self._finish_welcome(guild, _build_snag_embed())
            return False
        finally:
            self._seeding.pop(tenant_id, None)

    async def _reconcile_unjoined_tenant_defaults(self, tenant_id: uuid.UUID) -> _ReconcileOutcome:
        """Refresh defaults for a registered tenant without changing its install state.

        Returns True on success, False on failure and the task already
        refreshing the tenant when the in-flight guard skipped this call, as
        `_seed_tenant_defaults` does."""
        if tenant_id in self._unjoined_reconciling:
            return self._unjoined_reconciling[tenant_id]
        self._unjoined_reconciling[tenant_id] = asyncio.current_task()
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
            if report.is_failure():
                log.warning(
                    "unjoined_guild_reconcile_failed",
                    tenant_id=str(tenant_id),
                    reason=compose_failure_reason(report),
                )
                return False
            self._ready_reconciled_unjoined_tenant_ids.add(tenant_id)
            return True
        except Exception:
            log.exception("unjoined_guild_reconcile_failed", tenant_id=str(tenant_id))
            return False
        finally:
            self._unjoined_reconciling.pop(tenant_id, None)

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

    async def _retire_orphaned_turns(self, *, retry: bool = False) -> None:
        async with self._orphan_sweep_lock:
            await self._retire_orphaned_turns_once(retry=retry)

    async def _retire_orphaned_turns_once(self, *, retry: bool = False) -> None:
        """Recover dead workers' cards before admitting new turns.

        The boot snapshot runs once behind the admission barrier. Completed
        cards keep their formatting and controls; still-working cards receive
        recovered provider text or a visible restart notice. Permanent missing
        identity/permissions clear the marker, while transient failures retain
        it and enter the periodic retry list. Retries only revisit that original
        list, check owner liveness and compare the original marker, so neither
        gateway reconnects nor periodic recovery can interrupt a new live turn.
        """
        if self._orphans_retired and not retry:
            return
        if retry:
            orphans, self._orphan_retry_rows = self._orphan_retry_rows, []
        else:
            async with self.runtime.sessionmaker() as session:
                if self._boot_turn_card_intents is None:
                    self._boot_turn_card_intents = await list_recoverable_turn_card_intents(
                        session, platform="discord", exclude_live_owners=True
                    )
                orphans = await list_orphaned_turns(session, platform="discord")
        if not orphans:
            self._orphans_retired = True
            self._start_turn_card_recovery()
            return
        log.info("turn.orphans_found", count=len(orphans))

        async def retire(row: ThreadSessionRow) -> None:
            async with self.runtime.sessionmaker() as session:
                locked = await lock_orphaned_turn(session, row=row)
                if locked is None:
                    # A disconnected owner may still be draining. Revisit the
                    # snapshot after its heartbeat grace instead of losing it.
                    self._orphan_retry_rows.append(row)
                    return
                if locked.active_turn_message_id is None:
                    return
                delivered = False
                orphan_turn_id = next(
                    (
                        intent.id
                        for intent in self._boot_turn_card_intents or ()
                        if intent.message_id == locked.active_turn_message_id
                    ),
                    None,
                )
                try:
                    async with asyncio.timeout(30):
                        channel = self.get_channel(
                            int(locked.thread_id)
                        ) or await self.fetch_channel(int(locked.thread_id))
                        if isinstance(channel, discord.abc.Messageable):
                            message = await channel.fetch_message(
                                int(locked.active_turn_message_id)
                            )
                            if not any(
                                embed.color == discord.Color(theme.COLOR_IN_PROGRESS)
                                and embed.title is not None
                                and ("Working on it" in embed.title or "Thinking" in embed.title)
                                for embed in message.embeds
                            ):
                                # Delivery can precede marker cleanup. Preserve
                                # the final answer, attachments and feedback UI.
                                await clear_active_turn_if_message_id(
                                    session,
                                    id=locked.id,
                                    expected_message_id=locked.active_turn_message_id,
                                )
                                await session.commit()
                                return
                            result = await completed_orphan_text(self.runtime.anthropic, locked)
                            transport = DiscordPostTransport(
                                self, channel, name="Daimon", avatar_url=None, builtin=False
                            )
                            embed = discord.Embed(
                                color=theme.COLOR_RED,
                                title="Daimon restarted before this request finished.",
                                description=(
                                    "This request was interrupted by a restart; reply to retry "
                                    "or @mention Daimon to try again."
                                ),
                            )
                            # A single idempotent edit also covers a crash after
                            # Discord accepted it but before the DB commit.
                            kwargs: dict[str, Any] = {"embed": embed, "view": None}
                            if result:
                                kwargs = {
                                    "content": result
                                    if len(result) <= 2000
                                    else "Recovered reply attached.",
                                    "embed": None,
                                    "view": None,
                                }
                                if len(result) > 2000:
                                    kwargs["attachments"] = [
                                        attachment
                                        for attachment in message.attachments
                                        if attachment.filename != "recovered-reply.txt"
                                    ] + [
                                        discord.File(
                                            io.BytesIO(result.encode()),
                                            filename="recovered-reply.txt",
                                        )
                                    ]
                            if transport._destination() is not None:  # pyright: ignore[reportPrivateUsage]

                                async def edit_card(
                                    card: discord.Message,
                                    **edit_kwargs: Any,  # noqa: ANN401
                                ) -> discord.Message | None:
                                    return await transport.edit(
                                        card, **edit_kwargs, _allow_replacement=False
                                    )
                            elif not isinstance(message.webhook_id, int):

                                async def edit_card(
                                    card: discord.Message,
                                    **edit_kwargs: Any,  # noqa: ANN401
                                ) -> discord.Message | None:
                                    return await card.edit(**edit_kwargs)
                            else:
                                raise discord.ClientException("webhook channel unavailable")
                            log.info(
                                "turn.card_orphan_retirement_issued",
                                turn_id=str(orphan_turn_id) if orphan_turn_id else None,
                                session_id=str(locked.id),
                                message_id=locked.active_turn_message_id,
                            )
                            edited = await edit_recovered_card(
                                message,
                                turn_id=orphan_turn_id or locked.id,
                                edit=edit_card,
                                **kwargs,
                            )
                            if not edited:
                                raise discord.ClientException("recovery card edit was not applied")
                            log.info(
                                "turn.card_orphan_retirement_completed",
                                turn_id=str(orphan_turn_id) if orphan_turn_id else None,
                                session_id=str(locked.id),
                                message_id=locked.active_turn_message_id,
                                recovered_answer=bool(result),
                            )
                            delivered = bool(result)
                            log.info(
                                "turn.orphan_resumed" if delivered else "turn.orphan_retired",
                                thread_id=locked.thread_id,
                                message_id=locked.active_turn_message_id,
                                frozen_for_s=(
                                    datetime.now(UTC) - locked.active_turn_started_at
                                ).total_seconds()
                                if locked.active_turn_started_at
                                else None,
                            )
                except (
                    discord.HTTPException,
                    discord.ClientException,
                    TimeoutError,
                    ValueError,
                ) as err:
                    log.warning(
                        "turn.orphan_retire_failed", thread_id=locked.thread_id, error=str(err)
                    )
                    reason = (
                        "message_missing"
                        if isinstance(err, discord.HTTPException) and err.code == 10008
                        else "webhook_path_unavailable"
                        if isinstance(err, discord.ClientException)
                        and str(err) == "webhook channel unavailable"
                        else "edit_failed"
                    )
                    log.warning(
                        "turn.card_orphan_retirement_dropped",
                        turn_id=str(orphan_turn_id) if orphan_turn_id else None,
                        session_id=str(locked.id),
                        message_id=locked.active_turn_message_id,
                        reason=reason,
                    )
                    if not (
                        isinstance(err, (discord.NotFound, discord.Forbidden, ValueError))
                        or is_definite_recovery_failure(err)
                    ):
                        self._orphan_retry_rows.append(row)
                        return
                cleared = await clear_active_turn_if_message_id(
                    session, id=locked.id, expected_message_id=locked.active_turn_message_id
                )
                if cleared and not delivered:
                    interrupted = await interrupt_orphaned_session(
                        self.runtime.anthropic,
                        session_id=locked.ma_session_id,
                        scope=Scope(
                            tenant_id=str(locked.tenant_id),
                            account_id=str(locked.account_id),
                            principal_id="daimon",
                            authorization_id="discord-orphan-sweep",
                        )
                        if locked.account_id is not None
                        else None,
                    )
                    if not interrupted:
                        await mark_dead(session, id=locked.id)
                await session.commit()

        # Four rows progress independently; a slow provider or unreachable
        # thread cannot hold every restart notice behind it.
        queue: asyncio.Queue[ThreadSessionRow] = asyncio.Queue()
        for row in orphans:
            queue.put_nowait(row)

        async def worker() -> None:
            while not queue.empty():
                await retire(queue.get_nowait())

        await asyncio.gather(*(worker() for _ in range(min(4, len(orphans)))))
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
        # A card whose marker the sweep deferred (a live or not-yet-expired
        # owner, or a retryable failure) belongs to marker recovery, which
        # reads the provider result and interrupts the session. Retiring it
        # here first would make that retry see a terminal card and skip both.
        # Periodic recovery picks the intent up once its marker is gone.
        deferred = {row.active_turn_message_id for row in self._orphan_retry_rows}
        self._boot_card_recovery_task = self._spawn(
            self._reconcile_boot_turn_cards(
                [
                    intent
                    for intent in self._boot_turn_card_intents
                    if intent.message_id is None or intent.message_id not in deferred
                ]
            )
        )
        if not self._turn_card_periodic_started:
            self._turn_card_periodic_started = True
            self._spawn(self._periodic_turn_card_recovery())

    async def _periodic_turn_card_recovery(self) -> None:
        """Revisit aged active intents without requiring another process restart."""
        next_aged_sweep = asyncio.get_running_loop().time()
        while not self.is_closed():
            await asyncio.sleep(30)
            try:
                await self._retire_orphaned_turns(retry=True)
                await self._recover_stopped_worker_cards()
                if asyncio.get_running_loop().time() >= next_aged_sweep:
                    await self._sweep_aged_turn_cards()
                    await self._prune_message_admissions()
                    next_aged_sweep = asyncio.get_running_loop().time() + 3600
            except Exception:
                log.warning("turn.card_intent_periodic_sweep_failed", exc_info=True)

    async def _recover_stopped_worker_cards(self) -> None:
        """Pick up intents omitted from the boot snapshot during owner grace."""
        async with self.runtime.sessionmaker() as session:
            intents = await list_recoverable_turn_card_intents(
                session, platform="discord", exclude_live_owners=True
            )
            active_card_ids = {
                row.active_turn_message_id
                for row in await list_orphaned_turns(session, platform="discord")
            }
        await self._reconcile_boot_turn_cards(
            [
                intent
                for intent in intents
                if intent.message_id not in active_card_ids
                and intent.id not in self._live_turn_card_intent_ids
            ]
        )

    async def _prune_message_admissions(self) -> None:
        tenants = await list_tenants_by_platform(self.runtime.sessionmaker, platform="discord")
        async with self.runtime.sessionmaker() as session:
            for tenant in tenants:
                await prune_old_messages(
                    session, tenant_id=tenant.id, cutoff=datetime.now(UTC) - timedelta(minutes=60)
                )
            await session.commit()

    async def _sweep_aged_turn_cards(self) -> None:
        if self._turn_card_recovery_lock.locked() or (
            self._boot_card_recovery_task is not None and not self._boot_card_recovery_task.done()
        ):
            log.info("turn.card_intent_periodic_sweep_skipped", reason="recovery_in_progress")
            return
        discord_settings = self.runtime.settings.discord
        assert discord_settings is not None
        cutoff = datetime.now(UTC).timestamp() - discord_settings.turn_card_unrecoverable_after_s
        async with self.runtime.sessionmaker() as session:
            intents = await list_recoverable_turn_card_intents(
                session, platform="discord", exclude_live_owners=True
            )
            live_card_ids = {
                row.active_turn_message_id
                for row in await list_orphaned_turns(session, platform="discord")
            }
        aged = [
            intent
            for intent in intents
            if intent.created_at.timestamp() <= cutoff
            and intent.message_id not in live_card_ids
            and intent.id not in self._live_turn_card_intent_ids
        ]
        await self._reconcile_boot_turn_cards(aged, restarted=False)

    async def _reconcile_boot_turn_cards(
        self, intents: list[TurnCardIntentRow], *, restarted: bool = True
    ) -> None:
        """Run bounded recovery workers for boot or periodic intents."""
        if not intents:
            return
        async with self._turn_card_recovery_lock:
            await self.wait_until_ready()
            intent_iter = iter(intents)

            async def worker() -> None:
                for intent in intent_iter:
                    try:
                        await self._reconcile_turn_card_intent(intent, restarted=restarted)
                    except Exception:
                        log.warning(
                            "turn.card_intent_recovery_failed",
                            intent_id=str(intent.id),
                            exc_info=True,
                        )

            await asyncio.gather(
                *(worker() for _ in range(min(_TURN_CARD_RECOVERY_CONCURRENCY, len(intents))))
            )
            async with self.runtime.sessionmaker() as session:
                released = await has_released_messages(
                    session, intent_ids=(intent.id for intent in intents)
                )
            if released:
                self._spawn(self._replay_missed_messages())

    async def _reconcile_turn_card_intent(
        self,
        intent: TurnCardIntentRow,
        *,
        restarted: bool = True,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Recover one intent independently so a delayed search cannot block others."""
        await self.wait_until_ready()
        thread: discord.Thread | None = None
        candidate_message_ids: set[int] = set()
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
                    await self._record_failed_card_recovery(
                        intent, thread=None, candidate_message_ids=candidate_message_ids
                    )
                    return
                thread = channel
                await reconcile_turn_card_intent(
                    self.runtime.sessionmaker,
                    intent=intent,
                    thread=channel,
                    client=self,
                    restarted=restarted,
                    candidate_message_ids=candidate_message_ids,
                )
                await self._record_failed_card_recovery(
                    intent, thread=thread, candidate_message_ids=candidate_message_ids
                )
                return
            except UnrecoverableTurnCardError as err:
                discord_settings = self.runtime.settings.discord
                assert discord_settings is not None
                await expire_unrecoverable_turn_card(
                    self.runtime.sessionmaker,
                    intent=intent,
                    thread=thread,
                    max_age_s=discord_settings.turn_card_unrecoverable_after_s,
                    reason=str(err),
                    candidate_message_ids=candidate_message_ids | err.message_ids,
                )
                return
            except (
                discord.HTTPException,
                discord.ClientException,
                SQLAlchemyError,
                TimeoutError,
                ValueError,
            ) as err:
                if is_definite_recovery_failure(err):
                    discord_settings = self.runtime.settings.discord
                    assert discord_settings is not None
                    await expire_unrecoverable_turn_card(
                        self.runtime.sessionmaker,
                        intent=intent,
                        thread=thread,
                        max_age_s=discord_settings.turn_card_unrecoverable_after_s,
                        reason=str(err),
                        candidate_message_ids=candidate_message_ids,
                    )
                    return
                if attempt < 2:
                    await sleep(5.0)
                    continue
                log.warning(
                    "turn.card_intent_thread_fetch_failed",
                    intent_id=str(intent.id),
                    thread_id=intent.thread_id,
                    error=str(err),
                )
                await self._record_failed_card_recovery(
                    intent, thread=thread, candidate_message_ids=candidate_message_ids
                )

    async def _record_failed_card_recovery(
        self,
        intent: TurnCardIntentRow,
        *,
        thread: discord.Thread | None,
        candidate_message_ids: set[int] | None = None,
    ) -> None:
        """Count unresolved passes, then retire an aged intent at the configured limit."""
        async with self.runtime.sessionmaker() as session:
            failures = await record_turn_card_recovery_failure(session, id=intent.id)
            refreshed = (
                await get_turn_card_intent(session, id=intent.id) if failures is not None else None
            )
            await session.commit()
        discord_settings = self.runtime.settings.discord
        assert discord_settings is not None
        if (
            failures is None
            or refreshed is None
            or failures < discord_settings.turn_card_unrecoverable_after_attempts
        ):
            return
        await expire_unrecoverable_turn_card(
            self.runtime.sessionmaker,
            intent=refreshed,
            thread=thread,
            max_age_s=discord_settings.turn_card_unrecoverable_after_s,
            reason="recovery attempts exhausted",
            candidate_message_ids=candidate_message_ids,
            allow_missing_member=True,
        )

    @property
    def _non_production(self) -> bool:
        name = self.runtime.settings.environment.name
        return isinstance(name, str) and bool(name)  # pyright: ignore[reportUnnecessaryIsInstance]

    def _allows_guild(self, guild_id: int | None) -> bool:
        """Non-production accepts only configured guilds; DMs have no guild to allow."""
        if not self._non_production:
            return True
        settings = self.runtime.settings.discord
        return (
            guild_id is not None and settings is not None and guild_id in settings.command_guild_ids
        )

    def dispatch(self, event: str, /, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Stop guild events before any registered cog listener sees them."""
        if args and event not in {"ready", "connect", "disconnect"}:
            subject = args[0]
            guild = getattr(subject, "guild", None)
            guild_id = getattr(subject, "guild_id", None)
            if guild_id is None and isinstance(subject, discord.Guild):
                guild_id = subject.id
            if guild_id is None and guild is not None:
                guild_id = getattr(guild, "id", None)
            guild_event = event in {"message", "interaction"} or guild_id is not None
            if guild_event and not self._allows_guild(guild_id):
                return
        super().dispatch(event, *args, **kwargs)

    async def on_ready(self) -> None:
        """Forward-only reconcile sweep: provision-if-missing, re-seed pending/failed,
        sync the command tree. NO archive-on-absence."""
        log.info("bot_ready", user=str(self.user))
        environment_name = self.runtime.settings.environment.name
        command_guild_ids = (
            set(self.runtime.settings.discord.command_guild_ids)
            if self.runtime.settings.discord is not None
            else set[int]()
        )
        non_production = self._non_production
        log.info(
            "discord_command_environment",
            environment=environment_name if non_production else "production",
            allowlist_size=len(command_guild_ids),
        )
        if non_production and not command_guild_ids:
            log.warning("discord_command_allowlist_empty", environment=environment_name)
        await self._retire_orphaned_turns()
        if not self._startup_replay_started:
            self._startup_replay_started = True
            self._spawn(self._replay_missed_messages())
        # The first on_ready's reconcile tasks are watched so the process logs one
        # aggregate event when they have all finished (the staging deploy waits on
        # it before `daimon defaults verify`). The watcher only awaits; it never
        # holds on_ready or changes what is reconciled.
        watch_boot_reconcile = not self._boot_reconcile_watched
        self._boot_reconcile_watched = True
        boot_tasks: list[asyncio.Task[_ReconcileOutcome]] = []
        try:
            tenants = await list_tenants_by_platform(self.runtime.sessionmaker, platform="discord")
            known_tenants = {tr.external_id: tr for tr in tenants}
            recovered_tenant_ids: set[uuid.UUID] = set()
            # Provision guilds joined while the bot was down. A known archived tenant
            # means the bot left and rejoined while this process was stopped: revive
            # it and reseed without sending a second welcome or signup credit.
            for guild in self.guilds:
                if not self._allows_guild(guild.id):
                    continue
                ws_id = str(guild.id)
                known_tenant = known_tenants.get(ws_id)
                if known_tenant is not None and known_tenant.archived_at is None:
                    continue
                tenant_id = await self._provision_joined_guild(guild)
                if tenant_id is None:
                    continue
                if known_tenant is not None:
                    recovered_tenant_ids.add(tenant_id)
                    boot_tasks.append(
                        self._spawn(
                            self._seed_tenant_defaults(
                                tenant_id=tenant_id, guild=guild, was_ready=False
                            )
                        )
                    )
                    continue
                await self._post_welcome(guild)
                boot_tasks.append(
                    self._spawn(
                        self._seed_tenant_defaults(
                            tenant_id=tenant_id, guild=guild, was_ready=False
                        )
                    )
                )

            # Reconcile every registered, unarchived tenant against the shipped defaults on
            # every boot, not just the ones stuck in pending/failed. Because every
            # deploy restarts this process, this loop is how a defaults edit (a prompt
            # rewrite, a new skill) reaches an already-provisioned install without any
            # hand-run command. An in-sync tenant costs roughly 13-15 provider read
            # calls here and zero writes -- the reconcile's own per-resource fingerprint
            # gate turns a hash match into a skip -- bounded by the sweep's concurrency
            # cap above. Per-guild permission check + tree sync for joined guilds.
            for tr in tenants:
                if non_production and int(tr.external_id) not in command_guild_ids:
                    continue
                guild = self.get_guild(int(tr.external_id))
                # Tracked per path: the joined path adds the roster check and the
                # ready flip, so a guild back since the last ready still gets them.
                reconciled_ids = (
                    self._ready_reconciled_unjoined_tenant_ids
                    if guild is None
                    else self._ready_reconciled_tenant_ids
                )
                reconciled = tr.provision_status == "ready" and tr.id in reconciled_ids
                if guild is None:
                    log.warning("registered_guild_not_joined", external_id=tr.external_id)
                    if tr.archived_at is None and not reconciled:
                        boot_tasks.append(
                            self._spawn(self._reconcile_unjoined_tenant_defaults(tr.id))
                        )
                    continue
                if tr.id not in recovered_tenant_ids and not reconciled:
                    boot_tasks.append(
                        self._spawn(
                            self._seed_tenant_defaults(
                                tenant_id=tr.id,
                                guild=guild,
                                was_ready=tr.provision_status == "ready",
                            )
                        )
                    )
                if guild.unavailable:
                    # Listed unavailable in READY (a Discord outage): it has no `me` to
                    # check and is not synced, so a later ready syncs it.
                    log.warning("registered_guild_unavailable", external_id=tr.external_id)
                    continue
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
                if non_production or guild.id in self._ready_synced_guild_ids:
                    continue
                try:
                    guild_obj = discord.Object(id=int(tr.external_id))
                    # Clear any guild-scoped command copies so commands live ONLY at
                    # global scope (synced below). A command registered both globally
                    # and per-guild renders twice in the guild; this also self-heals
                    # guilds that accumulated copies from the old copy_global_to path.
                    self.tree.clear_commands(guild=guild_obj)
                    await self.tree.sync(guild=guild_obj)
                    self._ready_synced_guild_ids.add(guild.id)
                    log.info("tree_synced", guild_id=tr.external_id)
                except discord.HTTPException as exc:
                    log.warning("tree_sync_failed", guild_id=tr.external_id, error=str(exc))
        except Exception:
            if watch_boot_reconcile:
                self._spawn(
                    self._watch_boot_reconcile(
                        boot_tasks, self._running_reconcile_tasks(), aborted=True
                    )
                )
            raise
        if watch_boot_reconcile:
            self._spawn(
                self._watch_boot_reconcile(
                    boot_tasks, self._running_reconcile_tasks(), aborted=False
                )
            )
        if non_production:
            # Remove commands left by a production deployment without registering
            # any global commands in this environment. Guild copies stay local.
            if not self._global_tree_synced:
                if self.application_id is None:
                    log.warning("tree_sync_global_clear_failed", reason="missing_application_id")
                    return
                try:
                    await self.http.bulk_upsert_global_commands(self.application_id, payload=[])
                    self._global_tree_synced = True
                    log.info("tree_cleared_global")
                except discord.HTTPException as exc:
                    log.warning("tree_sync_global_clear_failed", error=str(exc))
                    return
            for guild in self.guilds:
                if guild.id in self._ready_synced_guild_ids:
                    continue
                guild_obj = discord.Object(id=guild.id)
                try:
                    self.tree.clear_commands(guild=guild_obj)
                    if guild.id in command_guild_ids:
                        self.tree.copy_global_to(guild=guild_obj)
                    await self.tree.sync(guild=guild_obj)
                    self._ready_synced_guild_ids.add(guild.id)
                    log.info("tree_synced", guild_id=str(guild.id))
                except discord.HTTPException as exc:
                    log.warning("tree_sync_failed", guild_id=str(guild.id), error=str(exc))
            return
        # Global sync so dm_permission=True commands (e.g. /privacy) appear in DMs.
        # DM-capable commands must be registered at the global scope. Note: global
        # sync has propagation latency (up to ~1h); operators triggering UAT in
        # the test guild should wait or use a guild copy if iterating rapidly.
        if self._global_tree_synced:
            return
        try:
            await self.tree.sync()
            self._global_tree_synced = True
            log.info("tree_synced_global")
        except discord.HTTPException as exc:
            log.warning("tree_sync_global_failed", error=str(exc))

    def _running_reconcile_tasks(self) -> set[asyncio.Task[Any]]:
        """Every task holding either in-flight reconcile guard right now.

        The joined and unjoined guards are separate, so a boot reconcile on one
        path does not see a reconcile for the same tenant running on the other,
        and a tenant skipped as already reconciled spawns no boot task at all.
        The boot watcher waits on this snapshot as well as its own tasks."""
        current = asyncio.current_task()
        return {
            task
            for task in (*self._seeding.values(), *self._unjoined_reconciling.values())
            if task is not None and task is not current
        }

    async def _watch_boot_reconcile(
        self,
        tasks: list[asyncio.Task[_ReconcileOutcome]],
        running: set[asyncio.Task[Any]],
        *,
        aborted: bool,
    ) -> None:
        """Log one aggregate event once every boot reconcile task has finished.

        `running` is the snapshot of reconciles already holding an in-flight
        guard when on_ready's sweep ended (see `_running_reconcile_tasks`). A
        boot reconcile the guard skipped returns the task already reconciling
        that tenant. The watcher waits for all of them, deduplicated by task,
        and counts each result, so `complete` never fires while a reconcile it
        could see can still write. `asyncio.wait` does not cancel anything if
        this watcher is cancelled at shutdown, so every task keeps its own
        lifetime. A task that raised, was cancelled or returned False counts as
        failed, as does a skip whose running task is unknown. `aborted` is set
        when on_ready raised before it had started every tenant's reconcile.
        """
        started = monotonic()
        if tasks:
            await asyncio.wait(tasks)
        succeeded = failed = 0
        followed: list[asyncio.Task[Any]] = [*running]
        for task in tasks:
            result = None if task.cancelled() or task.exception() is not None else task.result()
            if isinstance(result, asyncio.Task):
                followed.append(result)
            elif result is False or result is None:
                failed += 1
            else:
                succeeded += 1
        # Dedupe by task: a skip's answer can also be in the snapshot. The boot
        # tasks are counted above and the watcher never waits on itself.
        own: set[asyncio.Task[Any] | None] = {*tasks, asyncio.current_task()}
        in_flight = [task for task in dict.fromkeys(followed) if task not in own]
        if in_flight:
            await asyncio.wait(in_flight)
        for task in in_flight:
            if task.cancelled() or task.exception() is not None or task.result() is False:
                failed += 1
            else:
                succeeded += 1
        fields: dict[str, object] = {
            "tenants": len(tasks),
            "succeeded": succeeded,
            "failed": failed,
            "in_flight": len(in_flight),
            "duration_s": round(monotonic() - started, 3),
        }
        if failed or aborted:
            log.warning("discord.boot_reconcile_failed", aborted=aborted, **fields)
        else:
            log.info("discord.boot_reconcile_complete", **fields)

    async def _replay_missed_messages(self) -> None:
        """Bound history reads, then send missed inputs through normal message gates."""
        settings = self.runtime.settings.discord
        if settings is None or settings.startup_replay_minutes == 0:
            return
        cutoff = datetime.now(UTC) - timedelta(minutes=settings.startup_replay_minutes)
        for guild in self.guilds:
            if not self._allows_guild(guild.id):
                continue
            tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(guild.id))
            async with self.runtime.sessionmaker() as session:
                candidates = await replay_channels(
                    session,
                    tenant_id=tenant_id,
                    cutoff=cutoff,
                    limit=settings.startup_replay_channels_per_guild,
                )
            for channel_id, watermark in candidates:
                try:
                    async with asyncio.timeout(30):
                        channel = self.get_channel(int(channel_id)) or await self.fetch_channel(
                            int(channel_id)
                        )
                        if (
                            not isinstance(channel, (discord.TextChannel, discord.Thread))
                            or channel.guild.id != guild.id
                        ):
                            continue
                        after = max(
                            discord.utils.time_snowflake(cutoff), int(watermark) if watermark else 0
                        )
                        messages = [
                            message
                            async for message in channel.history(
                                after=discord.Object(id=after),
                                oldest_first=True,
                                limit=settings.startup_replay_messages_per_channel,
                            )
                        ]

                    async def dispatch(messages: list[discord.Message]) -> None:
                        for message in messages:
                            # Preserve each channel's order; other channels do
                            # not wait for a long turn to finish here.
                            await self.on_message(message, startup_replay=True)

                    self._spawn(dispatch(messages))
                    log.info(
                        "startup_replay.channel_scanned",
                        guild_id=str(guild.id),
                        channel_id=channel_id,
                        messages=len(messages),
                    )
                except (
                    discord.HTTPException,
                    discord.ClientException,
                    TimeoutError,
                    ValueError,
                    SQLAlchemyError,
                ) as err:
                    log.warning(
                        "startup_replay.channel_failed",
                        channel_id=channel_id,
                        error_type=type(err).__name__,
                    )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Async two-phase provisioning: provision (pending) → immediate welcome →
        per-guild tree sync → background seed that flips ready/failed + posts the follow-up."""
        if not self._allows_guild(guild.id):
            return
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
            await self._post_welcome(guild)
            try:
                guild_obj = discord.Object(id=guild.id)
                self.tree.clear_commands(guild=guild_obj)
                if self._non_production:
                    self.tree.copy_global_to(guild=guild_obj)
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
        self.membership_resolver.invalidate_guild(str(guild.id))
        if not self._allows_guild(guild.id):
            return
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
        # A typed stop from someone allowed stops the running reply or fleet.
        if await self._typed_stop(message, thread, mentioned=False):
            return
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
        # Nobody asked for this turn, so nobody waits on it: it takes a free
        # slot or is silently refused, and never queues.
        count = self.turn_queue.in_flight(tenant_id)
        ticket = self.turn_queue.try_claim(tenant_id, cap=cap)
        if ticket is None:
            if count >= cap or self.turn_queue.depth(tenant_id):
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
                    reason="unprompted_at_cap",
                )
            else:
                log.info(
                    "turn.skipped.global_concurrency_shed",
                    tenant_id=str(tenant_id),
                    guild_id=guild_id,
                    channel_id=str(thread_id),
                    reason="unprompted_at_cap",
                )
            return
        self._processing.add(thread_id)
        self._thread_stops.claim(thread_id)
        self._track_processing_task(thread_id)
        with holding(ticket):
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
                await self._release_and_clear(thread_id, tenant_id, clear_reactions=False)
                self._pending.pop(thread_id, None)

    def _plain_text_names(self, guild: discord.Guild | None) -> tuple[str, ...]:
        """The names a typed "@<name>" may use: configured, account and server nickname."""
        me = guild.me if guild is not None else None
        candidates: tuple[object, ...] = (
            _resolve_bot_display_name(self.runtime.settings),
            self.user.name if self.user is not None else None,
            me.display_name if me is not None else None,
        )
        # Every message with an "@" reaches this; anything that is not a real
        # name string (a member missing from the cache, a test double) is
        # skipped rather than failing on_message.
        return tuple(name for name in candidates if isinstance(name, str))

    async def _plain_text_mentions_on(self, message: discord.Message) -> bool:
        """Read the tenant's switch for a message that already names the bot as text."""
        assert message.guild is not None
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(message.guild.id))
        try:
            enabled = await get_plain_text_mentions(self.runtime.sessionmaker, tenant_id=tenant_id)
        except SQLAlchemyError as exc:
            # A failed read leaves the message unaddressed, as with the switch off.
            log.warning("plain_text_mention.lookup_failed", error_type=type(exc).__name__)
            return False
        if enabled:
            log.info(
                "plain_text_mention.accepted",
                guild_id=str(message.guild.id),
                channel_id=str(message.channel.id),
            )
        return enabled

    async def _maybe_hint_unmentioned_reply(
        self, message: discord.Message, *, bot_mentioned: bool
    ) -> None:
        """React once to a person's unmentioned reply in a thread daimon opened.

        Such a reply starts no turn, by design. Without a sign, people read the
        silence as daimon ignoring them and re-mention it in the channel, which
        opens a sibling thread with none of this one's context. The reaction
        says "mention me to continue" without a message: once per thread per
        cooldown, not while the thread is followed, where the reply may still
        be answered unprompted, and not where the agent may not post. Best
        effort: a failure is logged only, and never shows the hint.
        """
        discord_settings = self.runtime.settings.discord
        if discord_settings is None or self.user is None or message.guild is None:
            return
        thread = message.channel
        if not isinstance(thread, discord.Thread):
            return
        if not is_unmentioned_reply_hint_candidate(
            enabled=discord_settings.unmentioned_reply_hint,
            author_is_bot=message.author.bot,
            author_is_webhook=isinstance(message.webhook_id, int),
            bot_mentioned=bot_mentioned,
            in_bot_owned_thread=thread.owner_id == self.user.id,
            mentions_someone_else=(
                message.mention_everyone
                or bool(message.role_mentions)
                or any(user.id != self.user.id for user in message.mentions)
            ),
            is_plain_message=message.type
            in (discord.MessageType.default, discord.MessageType.reply),
        ):
            return
        thread_id = thread.id
        if thread_id in self._processing or thread_id in self._participation_pending:
            return  # a turn is running or deciding here; this message may yet be read
        now = asyncio.get_running_loop().time()
        if not self._unmentioned_hint_cooldown.claim(thread_id, now=now):
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(message.guild.id))
        try:
            responder = self._thread_participant(
                bot_user_id=self.user.id, bot_display_name=discord_settings.bot_display_name
            )
            resolved = await responder.resolve(tenant_id=tenant_id, thread=thread)
            if resolved.mode is ParticipationMode.ON:
                self._unmentioned_hint_cooldown.release(thread_id)
                return
            # A reaction is a post: where the agent may not write (or that
            # can't be established), it stays silent like every other notice.
            post_state = await _channel_protection_state(
                self.runtime.sessionmaker, tenant_id=tenant_id, channel=thread
            )
            if not post_state.may_post:
                self._unmentioned_hint_cooldown.release(thread_id)
                return
            await message.add_reaction(UNMENTIONED_REPLY_HINT_EMOJI)
        except Exception as exc:  # cosmetic hint: never surfaces, never retries
            log.warning(
                "unmentioned_reply_hint.failed",
                thread_id=str(thread_id),
                err_type=type(exc).__name__,
            )
            return
        log.info("unmentioned_reply_hint.shown", thread_id=str(thread_id))

    async def _signal_protected_channel(self, message: discord.Message) -> None:
        """Tell a mentioner, quietly, why a Who-can-post-Nobody place stays silent.

        Called where the turn was already refused as protected. The protection
        still holds: no turn runs and no message goes into the channel. The
        one exception is a 🔒 reaction on the mention itself, so "not allowed
        here" doesn't look like "Daimon is down"; the reason and who can
        change it go to the author by direct message.

        Only a rule that positively matches is told. A refusal that came from
        failing closed (an unresolved category while some category is
        protected) or an unreadable policy stays silent, as does a mention
        replayed at startup. At most once per person per channel per
        ``protected_channel_notice_cooldown_min``. Best effort: a closed DM or
        a missing reaction permission is logged.
        """
        # Policy (root, 10-12): a direct @mention only -- never a plain message
        # or a reply without one; one reaction per message at most.
        if self.user is None or not any(u.id == self.user.id for u in message.mentions):
            return
        if message.id in self._replayed_message_ids or message.id in self._protected_reacted_ids:
            return
        channel = message.channel
        if isinstance(channel, discord.Thread):
            channel_id, thread_id = str(channel.parent_id), str(channel.id)
        else:
            channel_id, thread_id = str(channel.id), None
        key = (message.author.id, channel_id)
        # Claimed before any await, so racing mentions cannot both notify.
        if not self._protected_notice_cooldown.claim(key, now=asyncio.get_running_loop().time()):
            return
        # Cached only: a category that needs a fetch is unresolved, and an
        # unresolved category confirms nothing.
        category_id, _ = await _resolve_category(channel, fetch=False)
        guild_id = message.guild.id if message.guild is not None else None
        protection = (
            await confirmed_protection(
                self.runtime.sessionmaker,
                tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)),
                channel_id=channel_id,
                thread_id=thread_id,
                category_id=category_id,
            )
            if guild_id is not None
            else None
        )
        if protection is None:
            self._protected_notice_cooldown.release(key)
            log.info("protected_channel_notice.unconfirmed", channel_id=channel_id)
            return
        self._protected_reacted_ids[message.id] = None
        if len(self._protected_reacted_ids) > 4096:
            self._protected_reacted_ids.popitem(last=False)
        reacted = False
        try:
            await message.add_reaction(PROTECTED_CHANNEL_REACTION)
            reacted = True
        except Exception as exc:  # cosmetic: never fails the (already dropped) turn
            log.warning(
                "protected_channel_notice.reaction_failed",
                channel_id=channel_id,
                err_type=type(exc).__name__,
            )
        dm_sent = False
        try:
            await message.author.send(protected_channel_notice(protection, channel_id=channel_id))
            dm_sent = True
        except Exception as exc:  # DMs closed, or the author is a bot
            log.info(
                "protected_channel_notice.dm_failed",
                channel_id=channel_id,
                user_id=str(message.author.id),
                err_type=type(exc).__name__,
            )
        log.info(
            "protected_channel_notice.shown",
            channel_id=channel_id,
            user_id=str(message.author.id),
            scope=protection.scope,
            reacted=reacted,
            dm_sent=dm_sent,
        )

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Remember the clicker's name for /billing; the command tree handles the rest."""
        if not self._allows_guild(interaction.guild_id):
            return
        remember_guild_user(
            self.runtime.sessionmaker, guild_id=interaction.guild_id, user=interaction.user
        )

    async def _notice_writers_none(self, message: discord.Message, tenant_id: uuid.UUID) -> None:
        """Opted-in, non-content host signal; never authorizes a post or a turn."""
        settings = self.runtime.settings.discord
        if (
            settings is None
            or settings.writers_none_notice is not True
            or self.wake_poller_enabled is not True
            or self.user is None
            or message.guild is None
            or derive_tenant_uuid(platform="discord", workspace_id=str(message.guild.id))
            != tenant_id
            or not any(user.id == self.user.id for user in message.mentions)
            or not should_process_message(
                author_is_bot=message.author.bot,
                author_id=str(message.author.id),
                bot_mentioned=True,
                author_is_webhook=isinstance(message.webhook_id, int),
                guild_id=str(message.guild.id),
                self_user_id=str(self.user.id),
                qa_bot_user_ids=settings.qa_bot_user_ids,
            )
        ):
            return
        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        if channel.guild.id != message.guild.id:
            return
        parent_id = channel.parent_id if isinstance(channel, discord.Thread) else channel.id
        if not all(
            _positive_discord_id(value)
            for value in (channel.id, parent_id, message.id, message.author.id)
        ):
            return
        limiter = getattr(self, "_writers_none_notice_limiter", None)
        if not isinstance(limiter, _WritersNoneNoticeLimiter):
            limiter = _WritersNoneNoticeLimiter()
            self._writers_none_notice_limiter = limiter
        if not limiter.reserve(
            tenant=str(tenant_id),
            channel=str(parent_id),
            requester=str(message.author.id),
            message=str(message.id),
            now=monotonic(),
        ):
            return
        try:
            async with asyncio.timeout(3):
                async with self.runtime.sessionmaker() as session:
                    policy = await load_access_policy(session, tenant_id=tenant_id)
                category_id, _ = await _resolve_category(channel, fetch=False)
                # Unresolved-category protection is conservative, not evidence
                # of a real writers=none rule at this origin. Never fetch here.
                confirmed = (
                    channel_permissions(
                        policy,
                        channel_id=str(channel.id),
                        parent_channel_id=(
                            str(parent_id) if isinstance(channel, discord.Thread) else None
                        ),
                        category_id=category_id,
                        category_unresolved=False,
                    ).writers
                    == "none"
                )
                if confirmed:
                    await message.add_reaction("⛔")
        except Exception as exc:
            # Keep the reservation on failure: no retry or fallback post/DM.
            log.warning("discord.writers_none_notice_failed", error_type=type(exc).__name__)

    async def on_message(
        self,
        message: discord.Message,
        *,
        startup_replay: bool = False,
        resume_admitted: bool = False,
    ) -> None:
        """Gate on mention, resolve TenantContext once + run the non-ready self-heal gate,
        then orchestrate a turn in a thread."""
        if not self._allows_guild(message.guild.id if message.guild else None):
            return
        if startup_replay:
            # Its admission may run later (queued); it must stay as quiet as here.
            self._replayed_message_ids.add(message.id)
        discord_settings = self.runtime.settings.discord
        # Explicit @-mention only. `mentioned_in` returns True for @everyone/@here
        # (it short-circuits on message.mention_everyone), which would make the bot
        # reply to every mass ping. message.mentions excludes @everyone/@here and
        # role mentions, so this triggers only on a direct user mention of the bot.
        bot_mentioned = isinstance(message, ButtonMessage) or (
            self.user is not None and any(user.id == self.user.id for user in message.mentions)
        )
        if self.draining:
            return
        if (
            not startup_replay
            and not resume_admitted
            and not isinstance(message, ButtonMessage)
            and message.guild is not None
            and isinstance(message.author, discord.Member)
            and not message.author.bot
        ):
            # Timestamp actual ingress, before routing/queue waits. A queued
            # message's old Member snapshot must never restore a revoked role.
            # A button's snapshot predates its seed/switch reads; admission uses
            # current membership evidence instead of re-observing that snapshot.
            guild_id = str(message.guild.id)
            self.membership_resolver.observe(
                derive_tenant_uuid(platform="discord", workspace_id=guild_id),
                guild_id,
                str(message.author.id),
                member_facts(message.author),
            )
        is_webhook_post = isinstance(message.webhook_id, int)
        if not bot_mentioned and is_plain_text_mention_candidate(
            author_is_bot=message.author.bot,
            author_is_webhook=is_webhook_post,
            bot_mentioned=bot_mentioned,
            guild_id=str(message.guild.id) if message.guild else None,
            content=message.content,
            bot_names=self._plain_text_names(message.guild),
        ):
            # From here on it is a mention: the same gates, the same turn, and
            # no participation screening or unmentioned-reply hint for it.
            bot_mentioned = await self._plain_text_mentions_on(message)
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
        identity_on = identity_enabled_for(
            self.runtime.settings, "discord", str(message.guild.id) if message.guild else None
        )
        # Without identity every agent post is the bot's own, so a reply to any of
        # them (a later answer chunk, unpinged) counts, not only a pinged reply.
        resolved_is_bot = (
            isinstance(resolved, discord.Message)
            and self.user is not None
            and resolved.author.id == self.user.id
        )
        if (
            not bot_mentioned
            and (identity_on or resolved_is_bot)
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
                # Without identity only the bot's answers and routine results
                # count; a tool post still needs the ping.
                reply_to_recorded_post = post is not None and (
                    post.source != "auto_thread"
                    if identity_on
                    else post.source in ("turn", "routine")
                )
            except Exception as exc:
                log.warning("reply_gate.lookup_failed", error_type=type(exc).__name__)
        if not should_process_message(
            author_is_bot=message.author.bot,
            author_id=str(message.author.id),
            bot_mentioned=bot_mentioned,
            reply_to_recorded_post=reply_to_recorded_post,
            author_is_webhook=is_webhook_post,
            guild_id=str(message.guild.id) if message.guild else None,
            self_user_id=str(self.user.id) if self.user is not None else None,
            qa_bot_user_ids=discord_settings.qa_bot_user_ids if discord_settings else (),
        ):
            # Not a mention. The only other way a message starts a turn is
            # organic thread participation, which has its own gates.
            if not startup_replay and is_participation_candidate(
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
            if not startup_replay:
                await self._maybe_hint_unmentioned_reply(message, bot_mentioned=bot_mentioned)
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
            # Only a rule we could read says "not allowed here"; an unknown
            # state stays fully silent, and a replayed backlog is not re-told.
            # The explicit writers_none_notice opt-in keeps its own static
            # reaction instead, so one mention never gets both signals.
            if (
                post_state is ProtectionState.PROTECTED
                and discord_settings is not None
                and discord_settings.writers_none_notice is True
                and self.wake_poller_enabled is True
            ):
                await self._notice_writers_none(message, tenant_id)
            elif post_state is ProtectionState.PROTECTED and not startup_replay:
                await self._signal_protected_channel(message)
            return
        claimed_message = False
        queued_message = False
        try:
            tr: TenantRow | None = await get_tenant_liveness(self.runtime.sessionmaker, tenant_id)
            needs_provisioning = tr is None or tr.archived_at is not None
            if needs_provisioning:
                # Unprovisioned OR archived → provision + un-archive + seed in background.
                await self._ensure_provisioning(guild)
                tr = await get_tenant_liveness(self.runtime.sessionmaker, tenant_id)
            if tr is None:
                return

            async with self.runtime.sessionmaker() as session:
                claimed = await claim_message(
                    session,
                    tenant_id=tenant_id,
                    channel_id=str(message.channel.id),
                    message_id=str(message.id),
                    owner_key=self.runtime.owner_key,
                    resume_owned=resume_admitted,
                )
                await session.commit()
            if not claimed:
                return
            claimed_message = True

            if needs_provisioning:
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
            # "@Daimon stop" stops the running reply or fleet instead of waiting behind it.
            if isinstance(message.channel, discord.Thread) and await self._typed_stop(
                message, message.channel, mentioned=True
            ):
                return
            if thread_id in self._processing:
                queued_message = True
                await self._queue_behind_inflight_turn(thread_id, message)
                return

            # --- Turn slots (SCALE-01 per tenant, optional process-wide cap) ---
            # Admission is synchronous (no await between the per-thread check
            # above and the claim). Over either cap the turn queues: it posts
            # the ordinary card and waits for a slot after it (`wait_for_slot`
            # in _orchestrate). Only a full queue refuses, with the plain notice.
            # `holding` returns the slot, or leaves the queue, on every exit.
            cap = (
                tr.turn_cap
                if tr.turn_cap is not None
                else self.runtime.settings.discord.max_concurrent_turns_per_tenant
            )
            ticket = self.turn_queue.admit(
                tenant_id, cap=cap, guild_id=guild_id, channel_id=str(thread_id)
            )
            if ticket is None:
                count = self.turn_queue.in_flight(tenant_id)
                if count >= cap:
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
                        reason="queue_full",
                    )
                    await message.channel.send(TENANT_CAP_NOTICE)
                    return
                log.info(
                    "turn.skipped.global_concurrency_shed",
                    tenant_id=str(tenant_id),
                    guild_id=guild_id,
                    channel_id=str(thread_id),
                    reason="queue_full",
                )
                await message.channel.send(GLOBAL_CAP_NOTICE)
                return

            with (
                holding(ticket),
                queue_card_ticket(ticket, cap=cap)
                if self.runtime.settings.discord.tenant_cap_waiting_card
                else contextlib.nullcontext(),
            ):
                # Channel-level mentions each open their own thread + MA session, so
                # they run in parallel — no serialization (bounded by the
                # per-tenant and optional process-wide slots claimed above).
                # Serializing them by channel id wedged the whole channel whenever
                # a single turn stalled (e.g. an
                # upstream overload backoff with no SSE events for minutes).
                #
                # Channel mentions still parallelize per-mention (each opens its own
                # thread + MA session up front). But the bot-created thread is
                # registered in self._processing at creation time (inside
                # _orchestrate, immediately after create_thread), so an in-thread
                # follow-up mention that arrives during the *same* originating turn
                # queues instead of racing a second turn onto that thread's session.
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
                            try:
                                await self._drain_pending_mentions(
                                    created_thread_ids[0], guild_id, tenant_id
                                )
                            except Exception:
                                # Once the thread exists nothing for it is posted in the
                                # parent channel, so a drain failing on a lost thread ends
                                # here instead of reaching the prologue error post.
                                if self._thread_stops.get(created_thread_ids[0]) is None:
                                    raise
                                log.warning("thread_gone.drain_failed", exc_info=True)
                    finally:
                        # No-op in the normal case (the drain above already emptied
                        # the queue) — this only catches messages that arrive after
                        # the final drain iteration, the same residual window the
                        # thread branch below has.
                        for created_id in created_thread_ids:
                            await self._release_and_clear(
                                created_id, tenant_id, clear_reactions=True
                            )
                    return

                thread_id = message.channel.id
                self._processing.add(thread_id)
                self._thread_stops.claim(thread_id)
                self._track_processing_task(thread_id)
                try:
                    await self._handle_mention(message, guild_id, tenant_id)
                    await self._drain_pending_mentions(thread_id, guild_id, tenant_id)
                finally:
                    await self._release_and_clear(thread_id, tenant_id, clear_reactions=True)
        except (DaimonError, _anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await self._handle_prologue_failure(message, exc, guild_id, post_state=post_state)
        except Exception as exc:  # on_message event-handler boundary
            await self._handle_prologue_failure(message, exc, guild_id, post_state=post_state)
        finally:
            if claimed_message and not queued_message:
                await self._finish_admitted_messages(tenant_id, (str(message.id),))

    async def _finish_admitted_messages(
        self, tenant_id: uuid.UUID, message_ids: tuple[str, ...], *, discard_committed: bool = True
    ) -> None:
        committed = self._committed_message_ids.intersection(message_ids)
        if discard_committed:
            self._committed_message_ids.difference_update(message_ids)
        pending = tuple(message_id for message_id in message_ids if message_id not in committed)
        if not pending:
            return
        # Cancellation before the durable card intent must leave the input
        # pending for the next process. Posted intents already mark it handled.
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            return
        async with self.runtime.sessionmaker() as session:
            await finish_messages(session, tenant_id=tenant_id, message_ids=pending)
            await session.commit()

    async def _typed_stop(
        self, message: discord.Message, thread: discord.Thread, *, mentioned: bool
    ) -> bool:
        """A typed stop phrase in a thread: the Stop buttons' paths. True when answered.

        - Addressed to the bot (an @mention, a typed-name mention, or a reply to one
          of its posts: see `typed_stop`), with a still-running fleet run in the
          thread: the fleet card's Stop-all (its starter or a server admin, as the
          button), and the author's own running reply too. Refused there, the
          author can still stop a reply they may stop.
        - Otherwise the running reply's Stop: its asker or a server admin. An
          unmentioned stop phrase never touches a fleet, and one from someone who
          may not stop the reply is left alone; an @-mentioned one is told who can.
        """
        bot_id = self.user.id if self.user is not None else None
        if not is_stop_request(message.content, bot_id=bot_id):
            return False
        from daimon.adapters.discord.posted_controls.fleet import (
            FLEET_STOP_NOT_ALLOWED,
            FLEET_STOPPING,
            TypedPresser,
        )

        thread_id, author_id = thread.id, message.author.id
        is_admin = isinstance(message.author, discord.Member) and (
            admin_caller(message.author).is_server_admin
        )
        fleet = (
            await self._live_fleet_card(thread, author_id=author_id, is_admin=is_admin)
            if mentioned
            else None
        )
        if fleet is not None:
            if fleet == "not_allowed":
                text = FLEET_STOP_NOT_ALLOWED
            else:
                card, card_message = fleet
                text = await card.stop_typed(
                    TypedPresser(
                        user=message.author, guild_id=thread.guild.id, channel_id=thread_id
                    ),
                    message=card_message,
                )
            reply_outcome = self._typed_stops.stop(
                thread_id, author_id=author_id, is_admin=is_admin
            )
            fleet_stopped = text == FLEET_STOPPING
            log.info(
                "turn.stop_typed",
                platform="discord",
                thread_id=str(thread_id),
                pressed_by=str(author_id),
                outcome="fleet_stopped" if fleet_stopped else "fleet_refused",
                reply_outcome=reply_outcome,
            )
            if not fleet_stopped and reply_outcome in ("stopped", "already_stopping"):
                # Not their fleet to stop, but their own reply: that much stops.
                text = STOPPED if reply_outcome == "stopped" else ALREADY_STOPPING
            await self._reply_quietly(message, text)
            return True
        if thread_id not in self._processing:
            return False
        outcome = self._typed_stops.stop(thread_id, author_id=author_id, is_admin=is_admin)
        log.info(
            "turn.stop_typed",
            platform="discord",
            thread_id=str(thread_id),
            pressed_by=str(author_id),
            outcome=outcome,
        )
        if outcome == "no_running_turn" or (outcome == "not_allowed" and not mentioned):
            return False
        reply = {
            "stopped": STOPPED,
            "already_stopping": ALREADY_STOPPING,
            "not_allowed": STOP_NOT_ALLOWED,
        }[outcome]
        await self._reply_quietly(message, reply)
        return True

    async def _reply_quietly(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text, mention_author=False)
        except discord.HTTPException as exc:
            log.warning("turn.stop_typed_reply_failed", message_id=str(message.id), error=str(exc))

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
        self._queued_reactions.add(id(message))
        self._thread_queue.enqueue(thread_id, message)
        generation = self._processing.generation(thread_id)
        notify = self._processing.should_notify(thread_id)
        try:
            if self._thread_lost(message):
                return  # no reaction in a gone or closed thread; the release finishes the row
            await message.add_reaction("⌛")
        except Exception as exc:
            # best-effort queue marker after the message is already queued; see docstring
            log.warning(
                "mention_queue.reaction_failed",
                thread_id=str(thread_id),
                err_type=type(exc).__name__,
            )
        finally:
            # The drain can finish while add_reaction is still in flight.
            if id(message) not in self._queued_reactions and not self._thread_lost(message):
                await self._remove_mention_reaction(message, "⌛")
        if (
            notify
            and not self._thread_lost(message)
            and self._processing.is_current(thread_id, generation)
        ):
            try:
                await message.channel.send(QUEUE_NOTICE)
            except Exception as exc:
                log.warning(
                    "mention_queue.notice_failed",
                    thread_id=str(thread_id),
                    err_type=type(exc).__name__,
                )

    def _thread_lost(self, message: discord.Message) -> bool:
        """Whether `message` sits in a thread that is gone or closed.

        A reaction there fails the same way every other write does, so none is
        issued. A message in the parent channel (the 👀 on a channel mention) is
        not in the lost thread and is still cleared.
        """
        channel = message.channel
        return (
            isinstance(channel, discord.Thread) and self._thread_stops.get(channel.id) is not None
        )

    async def _add_mention_reaction(self, message: discord.Message, emoji: str) -> None:
        """A cosmetic marker on the trigger: its failure is logged, never raised."""
        try:
            if not self._thread_lost(message):
                await message.add_reaction(emoji)
        except Exception as exc:
            log.warning("mention.reaction_failed", emoji=emoji, err_type=type(exc).__name__)

    async def _remove_mention_reaction(self, message: discord.Message, emoji: str) -> None:
        if message.guild is None or self.user is None or self._thread_lost(message):
            return
        try:
            await message.remove_reaction(emoji, self.user)
        except Exception as exc:
            log.warning("mention.reaction_clear_failed", emoji=emoji, err_type=type(exc).__name__)

    async def _clear_queued_reactions(self, messages: list[discord.Message]) -> None:
        for message in messages:
            self._queued_reactions.discard(id(message))
            await self._remove_mention_reaction(message, "⌛")

    async def _drop_queued(self, messages: list[discord.Message], tenant_id: uuid.UUID) -> None:
        """Give up on messages queued behind a turn whose thread is gone or closed.

        No turn will run them, so their admission rows are finished (startup
        replay must not start them again). Their hourglass stays: it sits in the
        thread, and no reaction call is issued there once the thread is lost.
        """
        if not messages:
            return
        for message in messages:
            self._queued_reactions.discard(id(message))  # bookkeeping only: no reaction call
        try:
            await self._finish_admitted_messages(
                tenant_id, tuple(str(m.id) for m in messages), discard_committed=True
            )
        except Exception as exc:  # the rows stay pending; replay finds the thread gone
            log.warning("thread_gone.finish_queued_failed", err_type=type(exc).__name__)
        log.info("thread_gone.queued_dropped", dropped=len(messages))

    async def _release_and_clear(
        self, thread_id: int, tenant_id: uuid.UUID, *, clear_reactions: bool
    ) -> None:
        """Release a thread's turn slot; messages that arrived after the last drain go with it.

        A stopped thread's leftover queue is dropped (admissions finished);
        otherwise the callers' own cleanup stands. The stop is read before the
        release, which forgets it.
        """
        stopped = self._thread_stops.get(thread_id) is not None
        self._release_thread(thread_id)
        if stopped:
            await self._drop_queued(self._pending.pop(thread_id, []), tenant_id)
        elif clear_reactions:
            await self._clear_queued_reactions(self._pending.pop(thread_id, []))

    async def _handle_prologue_failure(
        self,
        message: discord.Message,
        exc: Exception,
        guild_id: str,
        *,
        post_state: ProtectionState,
    ) -> None:
        """Best-effort error render for on_message prologue failures (a backstop).

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
        return ThreadQueue(self._processing, self._pending, start_turn=self._processing.start_turn)

    async def _drain_pending_mentions(
        self, thread_id: int, guild_id: str, tenant_id: uuid.UUID
    ) -> None:
        draining: dict[int, discord.Message] = {}

        def compose(queued: list[discord.Message]) -> list[list[discord.Message]]:
            draining.update((id(message), message) for message in queued)
            groups: list[list[discord.Message]] = []
            for author_messages in group_by_author(
                queued, lambda m: m.author.id, separate=lambda m: isinstance(m, ButtonMessage)
            ):
                pending: list[discord.Message] = []
                for queued_message in author_messages:
                    if is_affirmative(queued_message.content):
                        if pending:
                            groups.append(pending)
                            pending = []
                        groups.append([queued_message])
                    else:
                        pending.append(queued_message)
                if pending:
                    groups.append(pending)
            return groups

        async def run(messages: list[discord.Message]) -> None:
            # The turn before this one is over; this group's turn registers its own Stop.
            self._typed_stops.release(thread_id)
            if self._thread_stops.get(thread_id) is not None:
                # Checked before every group, so a thread lost mid-drain starts nothing more.
                for message in messages:
                    draining.pop(id(message), None)
                await self._drop_queued(messages, tenant_id)
                return
            try:
                await self._handle_mention(
                    messages[0],
                    guild_id,
                    tenant_id,
                    content_override=_compose_queued_content(messages),
                    attachments_override=[a for m in messages for a in m.attachments],
                    admitted_message_ids=tuple(str(m.id) for m in messages),
                    query_messages=tuple(messages),
                )
            finally:
                await self._clear_queued_reactions(messages)
                for message in messages:
                    draining.pop(id(message), None)

        try:
            await self._thread_queue.drain(thread_id, compose=compose, run=run)
        finally:
            # A cancelled/escaping author turn can leave the rest of its popped
            # batch unrun. Those messages must not retain a queue marker either.
            await self._clear_queued_reactions(list(draining.values()))

    async def _handle_mention(
        self,
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        admitted_message_ids: tuple[str, ...] = (),
        query_messages: tuple[discord.Message, ...] = (),
        unprompted: bool = False,
    ) -> None:
        """Orchestrate thread creation/lookup, session lifecycle, and turn execution.

        When ``content_override`` is provided (drain path for queued mentions),
        it replaces ``message.content`` as the active user query. Everything
        else (author, channel, attachments, thread history) still comes from
        ``message``.

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
        surface = _TurnFailureSurface()
        try:
            # Before admit(): resolving seeded agents/environments can take tens
            # of seconds. Queued mentions already have their own hourglass.
            if not unprompted and content_override is None:
                await self._add_mention_reaction(message, "👀")
            await self._orchestrate(
                message,
                guild_id,
                tenant_id,
                content_override=content_override,
                created_thread_ids=created_thread_ids,
                attachments_override=attachments_override,
                admitted_message_ids=admitted_message_ids,
                query_messages=query_messages,
                unprompted=unprompted,
                failure_surface=surface,
            )
        except _ThreadOpenFailed as exc:
            # Already answered in the channel; only record it.
            log.warning(
                "turn.thread_open_failed", error=str(exc), channel_id=str(message.channel.id)
            )
            _capture_turn_error(
                exc.__cause__ or exc, rid=rid, tenant_id=tenant_id, guild_id=guild_id
            )
        except (
            DaimonError,
            _anthropic.APIError,
            ProviderError,
            discord.HTTPException,
            SQLAlchemyError,
        ) as exc:
            log_anthropic_overload(
                exc,
                tenant_id=tenant_id,
                path="mention",
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
            )
            log.warning("turn.failed", error=str(exc), channel_id=str(message.channel.id))
            await self._render_turn_error(message, tenant_id, guild_id, rid, exc, surface=surface)
        except Exception as exc:  # mention-turn adapter boundary
            log.exception(
                "turn.failed.unexpected", error=str(exc), channel_id=str(message.channel.id)
            )
            await self._render_turn_error(message, tenant_id, guild_id, rid, exc, surface=surface)
        finally:
            await self._retire_gone_thread(
                tenant_id,
                created_thread_ids[0]
                if created_thread_ids
                else message.channel.id
                if isinstance(message.channel, discord.Thread)
                else None,
            )
            await self._finish_admitted_messages(
                tenant_id,
                admitted_message_ids or (str(message.id),),
                discard_committed=bool(admitted_message_ids),
            )
            # One slot per turn: a follow-up drained after this re-enters
            # admission instead of keeping the slot (wait_for_slot).
            release_turn_slot()
            structlog.contextvars.unbind_contextvars("rid")
            if not unprompted and content_override is None:
                await self._remove_mention_reaction(message, "👀")

    async def _render_turn_error(
        self,
        message: discord.Message,
        tenant_id: uuid.UUID,
        guild_id: str,
        rid: str,
        exc: Exception,
        *,
        surface: _TurnFailureSurface | None = None,
    ) -> None:
        """Sentry-tag + post a rendered error for a turn failure caught in _handle_mention.

        The error goes where the turn was: onto its status card while that is
        still up, else into its thread, and into the mention's channel only when
        no thread was opened. Never raises: a failed error post is logged, so
        nothing escapes `_handle_mention` into the prologue handler, which would
        post into the parent channel for a thread that no longer exists.

        A thread that is gone or closed gets no error anywhere: there is nobody
        to read it, and the parent channel must not be told about it.
        """
        thread = surface.thread if surface is not None else None
        if thread is not None:
            # A failure that is itself the thread's loss (a write outside the
            # turn's transport) counts the same as one the transport saw.
            if (stop := stop_reason_of(exc)) is not None:
                self._thread_stops.signal(thread.id, reason=stop[0], code=stop[1], phase="failure")
            if self._thread_stops.get(thread.id) is not None:
                log.info("turn.error_not_posted", reason="thread_stopped", thread_id=thread.id)
                return
        try:
            _capture_turn_error(exc, rid=rid, tenant_id=tenant_id, guild_id=guild_id)
            error_text = render_error(exc, request_id=rid)
            if surface is not None and surface.end_card is not None:
                try:
                    await surface.end_card(error_text)
                    return
                except Exception as card_exc:  # the card is best effort; the post below is not
                    log.warning("turn.error_card_failed", error_type=type(card_exc).__name__)
                    if (stop := stop_reason_of(card_exc)) is not None and thread is not None:
                        self._thread_stops.signal(
                            thread.id, reason=stop[0], code=stop[1], phase="failure"
                        )
                    if thread is not None and self._thread_stops.get(thread.id) is not None:
                        return  # the card write found the thread lost: say nothing anywhere
            target: discord.abc.Messageable = thread if thread is not None else message.channel
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
        except Exception as post_exc:  # the error post is the last resort; never raise from it
            if thread is not None and (stop := stop_reason_of(post_exc, write="send")) is not None:
                self._thread_stops.signal(thread.id, reason=stop[0], code=stop[1], phase="failure")
            log.warning(
                "turn.error_post_failed",
                error_type=type(post_exc).__name__,
                thread_id=thread.id if thread is not None else None,
            )

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
            identity_enabled=identity_enabled_for(self.runtime.settings, "discord", guild_id),
            worker_finished_decision=(
                functools.partial(decide_worker_finished, self.runtime.sessionmaker)
                if self.runtime.turn_deps.fleet_enabled
                else None
            ),
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
        self._thread_stops.claim(thread.id)
        self._track_processing_task(thread.id)
        try:
            await dispatch_and_drain(
                lambda: self._dispatch_continuations(
                    tenant_id=tenant_id, thread=thread, guild_id=guild_id
                ),
                lambda: self._drain_pending_mentions(thread.id, guild_id, tenant_id),
                drain_on_error=True,
            )
        finally:
            await self._release_and_clear(thread.id, tenant_id, clear_reactions=False)

    async def _channel_by_id(self, channel_id: int) -> object:
        """Cached channel, else a REST fetch (raises NotFound/Forbidden)."""
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def _post_fleet_run_end(self, notice: EndNotice) -> EndOutcome:
        """Settle an ended run's card and post its line in the run's own thread."""
        from daimon.adapters.discord.posted_controls.fleet import record_card, show_fleet_ended
        from daimon.core.fleet_orchestration import FleetRefused

        run = notice.run
        try:
            channel = await self._channel_by_id(int(run.thread_id))
            if (
                not isinstance(channel, discord.Thread)
                or derive_tenant_uuid(platform="discord", workspace_id=str(channel.guild.id))
                != run.tenant_id
                or not await self._may_post_in(tenant_id=run.tenant_id, channel=channel)
            ):
                return "give_up"
            await show_fleet_ended(channel, notice, record_card(self.runtime.sessionmaker))
            return "shown"
        except (ValueError, FleetRefused):
            return "give_up"
        except discord.HTTPException as exc:
            # A gone, locked or forbidden thread never takes the post; 429/5xx may.
            return "retry" if exc.status == 429 or exc.status >= 500 else "give_up"

    async def _post_github_request_expiry(self, request: AccessRequest) -> bool:
        try:
            channel = await self._channel_by_id(int(request.thread_id))
            if not isinstance(channel, discord.abc.Messageable):
                return True
            await channel.send(
                "Stopped waiting for GitHub access. Ask again any time.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except (discord.NotFound, discord.Forbidden, ValueError):
            return True
        except discord.HTTPException:
            return False

    async def _send_connect_notice(self, notice: ConnectNotice) -> bool:
        from daimon.core.stores.tenants import get_tenant

        async with self.runtime.sessionmaker() as session:
            tenant = await get_tenant(session, notice.tenant_id)
        if tenant is None:
            return True
        try:
            if (
                notice.encrypted_origin_followup is not None
                and notice.origin_followup_expires_at is not None
                and datetime.now(UTC) < notice.origin_followup_expires_at
            ):
                credentials = build_multifernet(
                    tuple(key.get_secret_value() for key in self.runtime.settings.crypto.keys)
                )
                application_id, interaction_token = decrypt_token(
                    credentials, notice.encrypted_origin_followup
                ).split(":", 1)
                async with (
                    aiohttp.ClientSession() as client,
                    client.post(
                        f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}",
                        json={
                            "content": notice.text,
                            "flags": 64,
                            "allowed_mentions": {"parse": []},
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response,
                ):
                    if response.status < 300:
                        return True
                    if response.status == 429:
                        return False
                    if response.status >= 500:
                        return False
            # Only the person who connected hears about it, privately. Nobody else in
            # the channel needs a follow-up message.
            return True
        except aiohttp.ClientError:
            return False
        except (discord.NotFound, discord.Forbidden, ValueError, LookupError):
            return True
        except discord.HTTPException:
            return False

    async def _poll_watch_import_notices(self) -> None:
        from daimon.adapters.discord.budget_notice import send_member_notice

        async def deliver(watch: WatchRow, workspace_id: str, text: str) -> bool:
            return await send_member_notice(
                self.runtime,
                self.open_member_dm,
                self.is_closed,
                tenant_id=watch.tenant_id,
                workspace_id=workspace_id,
                recipient_id=watch.owner_external_id,
                text=text,
            )

        await poll_watch_notices(self.runtime.sessionmaker, deliver=deliver)

    async def open_member_dm(self, guild_id: int, user_id: int) -> discord.abc.Messageable:
        """A DM with a human member of `guild_id` (routine delivery fallback).

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
        self._processing_tasks.pop(thread_id, None)
        self._thread_stops.release(thread_id)
        self._typed_stops.release(thread_id)

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
        surface = _TurnFailureSurface(thread=thread)
        try:
            with observe_turn(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="discord",
                channel_id=str(thread.parent_id),
                thread_id=str(thread.id),
                origin="handoff",
            ):
                async with contextlib.AsyncExitStack() as origins:
                    return await self._run_continuation_turn_observed(
                        row,
                        decision,
                        thread=thread,
                        tenant_id=tenant_id,
                        guild_id=guild_id,
                        surface=surface,
                        origins=origins,
                    )
        except Exception as exc:
            if isinstance(exc, _anthropic.APIError):
                log_anthropic_overload(
                    exc,
                    tenant_id=tenant_id,
                    path="continuation",
                    alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                )
            # The dispatcher settles the row; the card this turn posted is ours to end.
            await self._end_continuation_card(row, exc, surface)
            raise

    async def _end_continuation_card(
        self, row: TaskContinuationRow, exc: Exception, surface: _TurnFailureSurface
    ) -> None:
        """Turn a failed continuation's still-pending card into plain copy; never raises."""
        from daimon.core.neutral_resources import WorkerSpecRefused

        if surface.end_card is None or (
            surface.thread is not None and self._thread_stops.get(surface.thread.id) is not None
        ):
            return
        rid = bound_request_id()
        if row.reason == "worker_finished" and isinstance(
            exc, (WorkerSpecRefused, WorkerFinishedRootChanged)
        ):
            log.warning("fleet.wake_card_refused", error_type=type(exc).__name__, rid=rid)
            text = render_wake_refused(request_id=rid)
        else:
            text = render_error(exc, request_id=rid)
        try:
            await surface.end_card(text)
        except Exception as card_exc:  # best effort: the row is settled either way
            log.warning("continuation.error_card_failed", error_type=type(card_exc).__name__)

    async def _run_continuation_turn_observed(
        self,
        row: TaskContinuationRow,
        decision: ContinuationDecision,
        *,
        thread: discord.Thread,
        tenant_id: uuid.UUID,
        guild_id: str,
        surface: _TurnFailureSurface,
        origins: contextlib.AsyncExitStack,
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

        # Watches and long-job completions require fresh REST-only owner access.
        # Other continuations retain their existing requester-role resolution.
        if row.reason in ("watch_fired", "job_finished"):
            access = await require_watch_thread_access(self.runtime.sessionmaker, row, self)
            member = access.member
            role = (
                Role.ADMIN
                if is_member_guild_admin(member, guild_owner_id=member.guild.owner_id)
                else Role.USER
            )
            _role_ids = member_role_ids(member)
            category_id, category_unresolved = access.category_id, False
        else:
            role, _role_ids = await _requester_role(thread.guild, row.requester_external_user_id)
            post_state = await _channel_protection_state(
                self.runtime.sessionmaker, tenant_id=tenant_id, channel=thread
            )
            if not post_state.may_post:
                raise AdmissionDenied(reason="writers_none")
            category_id, category_unresolved = await _resolve_category(thread, fetch=False)
        admission = await admit(
            self.runtime.turn_deps,
            tenant_id=tenant_id,
            requester_org=RequesterOrg.HOST,
            platform="discord",
            external_user_id=row.requester_external_user_id,
            channel_id=row.parent_channel_id,
            thread_id=row.thread_id,
            role=role,
            role_ids=tuple(_role_ids),
            verified_membership=True if row.reason in ("watch_fired", "job_finished") else None,
            origin=row.reason,
            attended=continuation_attended(row),
            now=datetime.now(UTC),
            category_id=category_id,
            category_unresolved=category_unresolved,
        )
        if (
            row.reason in ("watch_fired", "job_finished")
            and admission.account_id != row.requester_account_id
        ):
            raise AdmissionDenied(reason="invoker_not_allowed")
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
        completion_batch = None
        if row.reason == "worker_finished":
            if not self.runtime.turn_deps.fleet_enabled:
                raise WorkerFinishedRootChanged("Fleet completion is disabled")
            completion_batch = FrozenWorkerBatch.model_validate_json(row.requested_work or "")
            await require_worker_finished_root(
                self.runtime.sessionmaker,
                row,
                session_id=completion_batch.session_id,
                account_id=admission.account_id,
                agent_id=admission.agent.id,
                now=datetime.now(UTC),
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
                    enabled=identity_enabled_for(self.runtime.settings, "discord", guild_id),
                    background_sessionmaker=self.runtime.sessionmaker,
                    wait_for_face=True,
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
        watch = TurnThreadWatch(self._thread_stops, thread.id)
        transport = DiscordPostTransport(
            self,
            thread,
            name=identity.name,
            avatar_url=identity.avatar_url,
            builtin=identity.builtin,
            identity_enabled=identity_enabled_for(self.runtime.settings, "discord", guild_id),
            watch=watch,
        )

        async def _edit_message(
            msg: discord.Message,
            **kwargs: Any,  # noqa: ANN401
        ) -> discord.Message | None:
            return await transport.edit(msg, **kwargs)

        async def _urgent_edit_message(
            msg: discord.Message,
            **kwargs: Any,  # noqa: ANN401
        ) -> discord.Message | None:
            return await transport.edit(msg, _urgent=True, **kwargs)

        async def _delete_message(msg: discord.Message) -> None:
            await transport.delete(msg)

        cancel = asyncio.Event()
        # A thread lost mid-turn sets the same event Stop does: the normal interrupt path.
        self._thread_stops.watch(thread.id, cancel)
        self._typed_stops.watch(
            thread.id, requester_id=int(row.requester_external_user_id), cancel=cancel
        )

        async def watch_binding() -> None:
            from daimon.core.stores.watches import watch_seed

            if row.reason == "watch_fired":
                async with self.runtime.sessionmaker() as db:
                    seed = await watch_seed(db, row, now=datetime.now(UTC))
                if seed is None:
                    raise AdmissionDenied(reason="runs_elsewhere")
            access = await require_watch_thread_access(self.runtime.sessionmaker, row, self)
            member = access.member
            live_role = (
                Role.ADMIN
                if is_member_guild_admin(member, guild_owner_id=member.guild.owner_id)
                else Role.USER
            )
            if live_role is not role or set(member_role_ids(member)) != set(_role_ids):
                raise AdmissionDenied(reason="invoker_not_allowed")

        def _make_lifecycle(
            turn_id: uuid.UUID, on_first_post: Callable[[discord.Message], Awaitable[None]]
        ) -> DiscordTurnLifecycle:
            lifecycle = DiscordTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                tenant_id=tenant_id,
                budget_channel_id=admission.budget_channel_id,
                requester_id=int(row.requester_external_user_id),
                requester_is_admin=admission.is_admin,
                in_dm=isinstance(thread, discord.DMChannel),
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                render_tables=tables_enabled(self.runtime.settings.table_rendering, tenant_id),
                fleet_card=self.fleet_card_for(
                    thread=thread,
                    requester_id=int(row.requester_external_user_id),
                    send=lambda: recorder.sender(
                        thread, turn_card_intent_id=turn_id, transport=transport
                    ),
                    edit=_edit_message,
                    turn_card_intent_id=turn_id,
                    on_replacement=lambda msg: recorder.message(
                        thread, msg, turn_card_intent_id=turn_id
                    ),
                    wake_id=row.id if row.reason == "worker_finished" else None,
                ),
                send=recorder.sender(thread, turn_card_intent_id=turn_id, transport=transport),
                edit=_edit_message,
                urgent_edit=_urgent_edit_message,
                delete=_delete_message,
                agent_name=agent.name,
                fallback_active=lambda: transport.fallback_used,
                thread_stopped=watch.is_stopped,
                model_id=agent.model.id,
                turn_id=turn_id,
                markup=self.runtime.turn_deps.markup,
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

            if row.reason in ("watch_fired", "job_finished"):
                lifecycle.set_output_authorization(OutputAuthorization(watch_binding))
            return lifecycle

        turn_card_intent, lifecycle = await post_initial_turn_card(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            thread_id=row.thread_id,
            make_lifecycle=_make_lifecycle,
            channel_id=row.parent_channel_id,
            owner_key=self.runtime.owner_key,
        )
        self._track_live_turn_card(turn_card_intent.id)
        lifecycle_holder: list[DiscordTurnLifecycle] = [lifecycle]

        async def _end_card_with_error(text: str) -> None:
            current = lifecycle_holder[0]
            if current.ended:
                return  # the card already became the answer or its own error
            if current.message_ref is not None:
                await current.end_card(text)
            else:
                await transport.send(text)
            shown_on = current.message_ref
            if shown_on is not None and str(shown_on.id) != current.card_message_id:
                return  # an earlier card still shows its Stop button; recovery retires it
            await retire_terminal_turn_card(
                self.runtime.sessionmaker,
                intent_id=turn_card_intent.id,
                expected_message_id=current.card_message_id,
                no_post_confirmed=not current.first_post_attempted,
            )

        surface.end_card = _end_card_with_error

        origin_context = turn_origin(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            platform="discord",
            attended=continuation_attended(row),
            parent_channel_id=row.parent_channel_id,
            thread_id=row.thread_id,
            responder_ma_agent_id=str(agent.id),
            responder_name=admission.config.agent_name or agent.name,
            configuration_target_ma_agent_id=admission.config.configuration_target_ma_agent_id,
            configuration_target_name=admission.config.configuration_target_name,
            role=Role.ADMIN if admission.is_platform_admin else Role.USER,
            sender_foreign=admission.hold is not None and admission.hold.client,
            shared_channel=admission.shared_channel,
            is_setup=admission.config.thread_binding_kind == "setup",
            keep_fleet_root=self.runtime.turn_deps.fleet_enabled,
        )
        prepared_origin = None
        if self.runtime.turn_deps.fleet_enabled:
            from daimon.core.turn.runtime_manifest import read_agent_fleet_grant_or_off

            # As on a mention: the origin opens before the session binds, so a
            # fleet root (a wake included) captures its source while preparing.
            grant = await read_agent_fleet_grant_or_off(
                self.runtime.turn_deps, admission, now=datetime.now(UTC)
            )
            if grant is not None:
                prepared_origin = await origins.enter_async_context(origin_context)

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

        async def completion_root() -> None:
            if prepared.session_account_id != prepared.admission.account_id:
                raise WorkerFinishedRootChanged("Prepared fleet account differs from admission")
            await require_worker_finished_root(
                self.runtime.sessionmaker,
                row,
                session_id=prepared.ma_session_id,
                account_id=prepared.admission.account_id,
                agent_id=prepared.admission.agent.id,
                now=datetime.now(UTC),
            )

        if completion_batch is not None:
            await completion_root()
        watch.session_id = prepared.ma_session_id

        marked_message_id: str | None = None
        if prepared.mapping_id is not None and lifecycle.final_message_id is not None:
            marked_message_id = lifecycle.final_message_id
            async with self.runtime.sessionmaker() as session:
                await mark_turn_active(
                    session,
                    id=prepared.mapping_id,
                    active_turn_message_id=lifecycle.final_message_id,
                    now=datetime.now(UTC),
                    owner_key=self.runtime.owner_key,
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
                raise UserFacingError("This setup expired. Mention me again to continue.")
            return (
                render_turn_origin(
                    recovery_origin,
                    responder_handle=_responder_handle(self.runtime.settings),
                    session_state=session_state,
                    handoff=handoff_notice,
                    effective_is_admin=admission.is_admin,
                )
                + "\n"
                + seed_message
            )

        def _recovery_lifecycle(cancel_event: asyncio.Event) -> TurnLifecycle:
            self._typed_stops.watch(
                thread.id, requester_id=int(row.requester_external_user_id), cancel=cancel_event
            )
            new_lifecycle = DiscordTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                tenant_id=tenant_id,
                budget_channel_id=admission.budget_channel_id,
                requester_id=int(row.requester_external_user_id),
                requester_is_admin=admission.is_admin,
                in_dm=isinstance(thread, discord.DMChannel),
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                render_tables=tables_enabled(self.runtime.settings.table_rendering, tenant_id),
                send=recorder.sender(
                    thread, turn_card_intent_id=turn_card_intent.id, transport=transport
                ),
                edit=_edit_message,
                urgent_edit=_urgent_edit_message,
                delete=_delete_message,
                agent_name=agent.name,
                fallback_active=lambda: transport.fallback_used,
                thread_stopped=watch.is_stopped,
                model_id=agent.model.id,
                turn_id=turn_card_intent.id,
                markup=self.runtime.turn_deps.markup,
                cancel_view=CancelView(
                    allowed_user_id=int(row.requester_external_user_id),
                    cancel=cancel_event,
                    turn_id=turn_card_intent.id,
                ),
                adopt_message_ref=lifecycle.release_message_ref(),
                adopt_pending_progress=lifecycle,
                unprompted=False,
                on_replacement=lambda msg: recorder.message(
                    thread, msg, turn_card_intent_id=turn_card_intent.id
                ),
            )
            if row.reason in ("watch_fired", "job_finished"):
                new_lifecycle.set_output_authorization(OutputAuthorization(watch_binding))
            lifecycle_holder[0] = new_lifecycle
            return new_lifecycle

        outcome: RunOutcome | None = None
        archive_after = False
        watch.phase = "running"
        try:
            async with contextlib.AsyncExitStack() as running_origin:
                if prepared_origin is None:
                    origin = await running_origin.enter_async_context(origin_context)
                else:
                    await running_origin.enter_async_context(origins.pop_all())
                    origin = prepared_origin
                outcome = await run_prepared_turn(
                    self.runtime.turn_deps,
                    prepared,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=row.thread_id,
                    external_user_id=row.requester_external_user_id,
                    watch_row=row if row.reason == "watch_fired" else None,
                    job_row=row if row.reason == "job_finished" else None,
                    origin="handoff" if handoff_notice is not None else "chat",
                    attended=False
                    if row.reason in ("watch_fired", "job_finished")
                    else continuation_attended(row),
                    user_message=(
                        render_turn_origin(
                            origin,
                            responder_handle=_responder_handle(self.runtime.settings),
                            session_state=session_state,
                            handoff=handoff_notice,
                            effective_is_admin=admission.is_admin,
                        )
                        + "\n"
                        + seed_message
                    ),
                    lifecycle=lifecycle,
                    turn_card_intent_id=turn_card_intent.id,
                    cancel=cancel,
                    reseed_user_message=_reseed_continuation_message,
                    recovery_lifecycle=_recovery_lifecycle,
                    render_interval_s=2.0,
                    deadline=turn_deadline_at,
                    confirm_write=discord_confirmation_hook(
                        thread,
                        runtime=self.runtime,
                        tenant_id=tenant_id,
                        account_id=admission.account_id,
                        admission=admission,
                    ),
                    active_turn_message_id=marked_message_id,
                    before_send=(
                        watch_binding
                        if row.reason in ("watch_fired", "job_finished")
                        else completion_root
                        if completion_batch is not None
                        else None
                    ),
                    worker_finished=row if completion_batch is not None else None,
                    **provider_action_options(
                        prepared,
                        lambda: discord_provider_action_hook(
                            thread,
                            thread_id=row.thread_id,
                            channel_id=thread.id,
                        ),
                    ),
                )
                # The run ended the card itself (answer, or red error card).
                surface.end_card = None
                archive_after = await self._archive_requested(origin.id)
        finally:
            await self._interrupt_stopped_turn(
                thread.id,
                tenant_id,
                prepared,
                prepared.ma_session_id,
                outcome.ma_session_id if outcome is not None else None,
            )
            await release_turn_markers(
                self.runtime.sessionmaker,
                self.runtime.turn_deps.running_turns,
                {i: None for i in (prepared.mapping_id, outcome and outcome.mapping_id) if i},
            )
            if outcome is not None and not lifecycle_holder[0].card_discard_failed:
                await retire_terminal_turn_card(
                    self.runtime.sessionmaker,
                    intent_id=turn_card_intent.id,
                    expected_message_id=lifecycle_holder[0].card_message_id,
                    allow_prepared_without_message=not lifecycle_holder[0].first_post_attempted,
                    keep_fleet_root=self.runtime.turn_deps.fleet_enabled,
                )

        assert outcome is not None
        log_anthropic_overload(
            outcome.state.error.cause if outcome.state.error is not None else None,
            tenant_id=tenant_id,
            path="continuation",
            alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
        )
        if self._thread_stops.get(thread.id) is not None:
            # The thread is gone or closed: no answer, notice, file or archive to deliver.
            await self._retire_gone_thread(tenant_id, thread.id)
            return
        final_lifecycle = lifecycle_holder[0]
        if outcome.state.error is None and prepared.mapping_id is not None:
            if final_lifecycle.watermark_message_id is not None:
                async with self.runtime.sessionmaker() as session:
                    await update_watermark(
                        session,
                        id=prepared.mapping_id,
                        watermark_message_id=final_lifecycle.watermark_message_id,
                        delivered_answer_chunk_ids=final_lifecycle.delivered_answer_chunk_ids,
                    )
                    await session.commit()
            if final_lifecycle.was_answered and final_lifecycle.feedback_message_id is not None:
                await seed_feedback_reactions(
                    thread, message_id=final_lifecycle.feedback_message_id
                )
        self._schedule_output_sweep(
            outcome,
            thread=thread,
            tenant_id=tenant_id,
            lifecycle=final_lifecycle,
            selfhosted=(
                prepared.admission.backend_revision is not None
                and prepared.admission.backend_revision.backend == "selfhosted"
            ),
        )
        if archive_after:
            await self._archive_after_outputs(outcome, thread)

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        self.membership_resolver.invalidate_guild(str(after.guild.id))
        if not self._allows_guild(after.guild.id):
            return
        if before.permissions != after.permissions:
            clear_webhook_backoff(after.guild)

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        guild_id = str(payload.guild_id)
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        self.membership_resolver.invalidate(tenant_id, guild_id, str(payload.user.id))

    async def on_member_remove(self, member: discord.Member) -> None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(member.guild.id))
        self.membership_resolver.invalidate(tenant_id, str(member.guild.id), str(member.id))

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        self.membership_resolver.invalidate_guild(str(guild.id))

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(after.guild.id))
        self.membership_resolver.observe(
            tenant_id, str(after.guild.id), str(after.id), member_facts(after)
        )
        if not self._allows_guild(after.guild.id):
            return
        if self.user is not None and after.id == self.user.id and before.roles != after.roles:
            clear_webhook_backoff(after.guild)

    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        if not self._allows_guild(after.guild.id):
            return
        if before.overwrites != after.overwrites:
            clear_webhook_backoff(after.guild)

    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent) -> None:
        if not self._allows_guild(payload.guild_id):
            return
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
        if not self._allows_guild(payload.guild_id):
            return
        had_running_turn = payload.thread_id in self._processing
        # Every time, so a deployment can show whether this event beats the first 10003.
        log.info(
            "thread.deleted",
            thread_id=str(payload.thread_id),
            parent_id=str(payload.parent_id),
            guild_id=str(payload.guild_id),
            had_running_turn=had_running_turn,
        )
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(payload.guild_id))
        if had_running_turn:
            # Before any await: the turn stops at its next write or wait.
            self._thread_stops.signal(payload.thread_id, reason="gone", code=None, phase="running")
        await self._retire_thread_sessions(tenant_id, payload.thread_id)
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

    async def _interrupt_stopped_turn(
        self, thread_id: int, tenant_id: uuid.UUID, prepared: PreparedTurn, *session_ids: str | None
    ) -> None:
        """After a turn whose thread was lost, request interruption of its bound session.

        A Stop that lands before the driver has read any events (while the question
        is still being sent) is finalised without a `user.interrupt`, so MA could
        go on running a turn nobody can read. An interrupt on an idle session is
        a no-op, so this is sent for every stopped turn without inspecting how far
        the driver got. Best effort; runs before the sessions are retired.
        """
        if self._thread_stops.get(thread_id) is None:
            return
        scope = Scope(
            tenant_id=str(tenant_id),
            account_id=str(prepared.session_account_id),
            principal_id="daimon",
            authorization_id="discord-thread-gone",
        )
        revision = prepared.admission.backend_revision
        profile = revision.profile if revision is not None else "anthropic.managed_agents"
        for session_id in dict.fromkeys(i for i in session_ids if i):
            try:
                if profile != "anthropic.managed_agents":
                    scope = admitted_session_scope(
                        prepared.admission, tenant_id=tenant_id, session_id=session_id
                    )
                if prepared.session_ref is not None and (
                    prepared.session_ref.kind != "session"
                    or prepared.session_ref.provider != get_profile(profile).provider
                    or prepared.session_ref.tenant_id != scope.tenant_id
                    or prepared.session_ref.account_id != scope.account_id
                ):
                    log.info(
                        "thread_gone.interrupt_skipped",
                        session_id=session_id,
                        reason="mismatched_provider_binding",
                    )
                    continue
                if (
                    prepared.backend is not None
                    and prepared.backend.capabilities().profile_id != profile
                ):
                    log.info(
                        "thread_gone.interrupt_skipped",
                        session_id=session_id,
                        reason="mismatched_provider_profile",
                    )
                    continue
                if profile == "anthropic.managed_agents":
                    await interrupt_orphaned_session(
                        self.runtime.anthropic, session_id=session_id, scope=scope
                    )
                    continue
                backend, ref = prepared.backend, prepared.session_ref
                if backend is None or ref is None or ref.id != session_id:
                    log.info(
                        "thread_gone.interrupt_skipped",
                        session_id=session_id,
                        reason="missing_authorized_provider_binding",
                    )
                    continue
                async with asyncio.timeout(ORPHAN_INTERRUPT_TIMEOUT_S):
                    snapshot = await backend.events.reconcile(scope, ref)
                    if snapshot.session != ref or snapshot.active_root_turn is None:
                        log.info(
                            "thread_gone.interrupt_skipped",
                            session_id=session_id,
                            reason="missing_authorized_active_root",
                        )
                        continue
                    await backend.events.cancel(
                        scope,
                        ref,
                        turn_id=snapshot.active_root_turn,
                        key=str(uuid.uuid4()),
                    )
            except ScopeViolation:
                log.info(
                    "thread_gone.interrupt_skipped",
                    session_id=session_id,
                    reason="mismatched_admission_scope",
                )
            except UnsupportedCapability:
                log.info(
                    "thread_gone.interrupt_skipped",
                    session_id=session_id,
                    reason="provider_cancel_unsupported",
                )
            except Exception as exc:  # best effort; the sweep and retired rows cover the rest
                log.warning(
                    "thread_gone.interrupt_failed",
                    session_id=session_id,
                    err_type=type(exc).__name__,
                )

    async def _retire_gone_thread(self, tenant_id: uuid.UUID, thread_id: int | None) -> None:
        """After a turn: if its thread was deleted, nothing in it may be revived.

        A closed (archived) thread keeps its sessions; it can be mentioned again.
        """
        if thread_id is None:
            return
        stop = self._thread_stops.get(thread_id)
        if stop is not None and stop.reason == "gone":
            await self._retire_thread_sessions(tenant_id, thread_id)

    async def _retire_thread_sessions(self, tenant_id: uuid.UUID, thread_id: int) -> None:
        """Mark every account's live session in a deleted thread dead and clear its turn marker.

        Neither startup replay nor orphan recovery then revives a conversation
        nobody can reach. Best effort: a failure is logged and the sessions stay
        live, where the next boot's recovery finds the thread gone.
        """
        try:
            async with self.runtime.sessionmaker() as session:
                rows = await list_live_thread_sessions(
                    session, tenant_id=tenant_id, platform="discord", thread_id=str(thread_id)
                )
                for row in rows:
                    await clear_active_turn(session, id=row.id)
                    await mark_dead(session, id=row.id)
                await session.commit()
        except Exception as exc:  # cleanup of a conversation nobody can reach
            log.warning(
                "thread_gone.retire_failed", thread_id=str(thread_id), err_type=type(exc).__name__
            )

    async def _orchestrate(
        self,
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        admitted_message_ids: tuple[str, ...] = (),
        query_messages: tuple[discord.Message, ...] = (),
        unprompted: bool = False,
        failure_surface: _TurnFailureSurface | None = None,
    ) -> None:
        with observe_turn(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="discord",
            channel_id=str(message.channel.id),
        ):
            async with contextlib.AsyncExitStack() as origins:
                return await self._orchestrate_observed(
                    message,
                    guild_id,
                    tenant_id,
                    origins=origins,
                    content_override=content_override,
                    created_thread_ids=created_thread_ids,
                    attachments_override=attachments_override,
                    admitted_message_ids=admitted_message_ids,
                    query_messages=query_messages,
                    unprompted=unprompted,
                    failure_surface=failure_surface,
                )

    async def _orchestrate_observed(
        self,
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        origins: contextlib.AsyncExitStack | None = None,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        admitted_message_ids: tuple[str, ...] = (),
        query_messages: tuple[discord.Message, ...] = (),
        unprompted: bool = False,
        failure_surface: _TurnFailureSurface | None = None,
    ) -> None:
        async with guard_selfhosted_channel(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="discord",
            channel_id=str(
                message.channel.parent_id
                if isinstance(message.channel, discord.Thread)
                else message.channel.id
            ),
            thread_id=str(message.channel.id)
            if isinstance(message.channel, discord.Thread)
            else None,
            caller_id=str(message.author.id),
            force=False,
        ):
            return await self._orchestrate_observed_guarded(
                message,
                guild_id,
                tenant_id,
                origins=origins,
                content_override=content_override,
                created_thread_ids=created_thread_ids,
                attachments_override=attachments_override,
                admitted_message_ids=admitted_message_ids,
                query_messages=query_messages,
                unprompted=unprompted,
                failure_surface=failure_surface,
            )

    async def _orchestrate_observed_guarded(
        self,
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        origins: contextlib.AsyncExitStack | None = None,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
        admitted_message_ids: tuple[str, ...] = (),
        query_messages: tuple[discord.Message, ...] = (),
        unprompted: bool = False,
        failure_surface: _TurnFailureSurface | None = None,
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

        ``failure_surface``, when provided, receives the turn's thread and a way
        to end its status card, so an error raised past this point is shown
        there (see `_render_turn_error`).
        """
        if origins is None:
            async with contextlib.AsyncExitStack() as owned_origins:
                return await DaimonBot._orchestrate_observed(
                    self,
                    message,
                    guild_id,
                    tenant_id,
                    origins=owned_origins,
                    content_override=content_override,
                    created_thread_ids=created_thread_ids,
                    attachments_override=attachments_override,
                    admitted_message_ids=admitted_message_ids,
                    query_messages=query_messages,
                    unprompted=unprompted,
                    failure_surface=failure_surface,
                )
        surface = failure_surface if failure_surface is not None else _TurnFailureSurface()
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
        # bail, MA resolve/retrieve, balance gate, cap gate) -- admit(). ---
        try:
            admission = await admit(
                self.runtime.turn_deps,
                origin="chat",
                tenant_id=tenant_id,
                requester_org=RequesterOrg.HOST,
                platform="discord",
                external_user_id=str(message.author.id),
                channel_id=parent_channel_id,
                thread_id=str(thread.id) if thread else None,
                role=role,
                now=self._ingress_clock() if self._ingress_clock is not None else datetime.now(UTC),
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
            await target.send(NOT_SET_UP_NOTICE)
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
            await target.send(SETUP_OUT_OF_DATE_NOTICE)
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
                # Nothing is posted into a protected channel, a refusal
                # included; the author gets the quiet signal instead (or, under
                # the explicit opt-in, only its static host reaction).
                log.info(
                    "turn.skipped.writers_none",
                    guild_id=guild_id,
                    channel_id=parent_channel_id,
                    user_id=str(message.author.id),
                )
                notice_settings = self.runtime.settings.discord
                if (
                    notice_settings is not None
                    and notice_settings.writers_none_notice is True
                    and self.wake_poller_enabled is True
                ):
                    await self._notice_writers_none(message, tenant_id)
                else:
                    await self._signal_protected_channel(message)
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
            elif err.reason == "client_requester":
                log.info(
                    "turn.skipped.client_requester",
                    guild_id=guild_id,
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
                await target.send(
                    admission_refusal_message(
                        err.reason, self.runtime.settings, cause=err.cause, provider=err.provider
                    )
                )
            return
        is_admin = admission.is_admin

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
                async with self.runtime.sessionmaker() as session:
                    unposted_thread_id = await unposted_thread_for_message(
                        session, tenant_id=tenant_id, message_id=str(message.id)
                    )
                if unposted_thread_id is not None:
                    existing = self.get_channel(
                        int(unposted_thread_id)
                    ) or await self.fetch_channel(int(unposted_thread_id))
                    if (
                        isinstance(existing, discord.Thread)
                        and existing.parent_id == message.channel.id
                    ):
                        self._processing.add(existing.id)
                        self._thread_stops.claim(existing.id)
                        if created_thread_ids is not None:
                            created_thread_ids.append(existing.id)
                        return existing
                # Name before creation to avoid a Discord rename system message.
                # The fallback title quotes the message, never the agent's
                # internal name (a slug means nothing to the reader).
                naming = self.runtime.settings.thread_naming
                bot_names = self._plain_text_names(message.guild)
                message_text = strip_mentions(message.content, bot_names)
                attachment_names = [a.filename for a in message.attachments]
                opening_text = naming_text(message_text, attachment_names)
                # Line breaks kept: the fallback takes only the first line,
                # while the naming model reads the whole collapsed message.
                thread_name = fallback_thread_name(
                    "\n".join(
                        strip_mentions(line, bot_names) for line in message.content.splitlines()
                    ),
                    default=attachment_names[0][:THREAD_NAME_MAX_CHARS]
                    if attachment_names
                    else f"Chat with {_resolve_bot_display_name(self.runtime.settings)}",
                )
                try:
                    if (
                        naming.enabled
                        and opening_text
                        and not (
                            admission.backend_revision is not None
                            and admission.backend_revision.backend == "selfhosted"
                        )
                    ):
                        async with contextlib.AsyncExitStack() as typing:
                            try:
                                await typing.enter_async_context(message.channel.typing())
                            except Exception as exc:  # best effort: typing never stops the open
                                log.warning("discord.thread_open_typing_failed", error=str(exc))
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
                    opened = await message.create_thread(
                        name=thread_name, auto_archive_duration=10080
                    )
                except Exception as exc:
                    # Only a failure before the thread exists is "couldn't open".
                    await _explain_thread_open_failure(message)
                    raise _ThreadOpenFailed(str(exc)) from exc
                # No await between creation and registration: follow-ups must queue
                # behind this turn. The channel branch owns the eventual cleanup.
                self._processing.add(opened.id)
                self._thread_stops.claim(opened.id)
                self._track_processing_task(opened.id)
                if created_thread_ids is not None:
                    created_thread_ids.append(opened.id)
                await recorder.opened_thread(opened)
                return opened

            discord_settings = self.runtime.settings.discord
            assert discord_settings is not None
            thread = await _open_thread_with_notice(
                message,
                _open_thread(),
                after_s=discord_settings.thread_open_notice_after_s,
                acknowledged=not unprompted,
            )

        surface.thread = thread

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
                    enabled=identity_enabled_for(self.runtime.settings, "discord", guild_id),
                    background_sessionmaker=self.runtime.sessionmaker,
                    wait_for_face=True,
                )
        except Exception as exc:
            log.warning("discord.identity_resolution_failed", error_type=type(exc).__name__)
            identity = AgentIdentity(name=agent.name, avatar_url=None, builtin=True)

        watch = TurnThreadWatch(self._thread_stops, thread.id)
        transport = DiscordPostTransport(
            self,
            thread,
            name=identity.name,
            avatar_url=identity.avatar_url,
            builtin=identity.builtin,
            identity_enabled=identity_enabled_for(self.runtime.settings, "discord", guild_id),
            watch=watch,
        )

        async def _edit_message(
            msg: discord.Message,
            **kwargs: Any,  # noqa: ANN401
        ) -> discord.Message | None:
            return await transport.edit(msg, **kwargs)

        async def _urgent_edit_message(
            msg: discord.Message,
            **kwargs: Any,  # noqa: ANN401
        ) -> discord.Message | None:
            return await transport.edit(msg, _urgent=True, **kwargs)

        async def _delete_message(msg: discord.Message) -> None:
            await transport.delete(msg)

        cancel = asyncio.Event()
        # A thread lost mid-turn sets the same event Stop does: the normal interrupt path.
        self._thread_stops.watch(thread.id, cancel)
        self._typed_stops.watch(thread.id, requester_id=message.author.id, cancel=cancel)

        def _make_lifecycle(
            turn_id: uuid.UUID, on_first_post: Callable[[discord.Message], Awaitable[None]]
        ) -> DiscordTurnLifecycle:
            return DiscordTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                tenant_id=tenant_id,
                budget_channel_id=admission.budget_channel_id,
                requester_id=message.author.id,
                requester_is_admin=is_admin,
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                trigger_message=message,
                acknowledgment_managed=True,
                render_tables=tables_enabled(self.runtime.settings.table_rendering, tenant_id),
                fleet_card=self.fleet_card_for(
                    thread=thread,
                    requester_id=message.author.id,
                    send=lambda: recorder.sender(
                        thread, turn_card_intent_id=turn_id, transport=transport
                    ),
                    edit=_edit_message,
                    turn_card_intent_id=turn_id,
                    on_replacement=lambda msg: recorder.message(
                        thread, msg, turn_card_intent_id=turn_id
                    ),
                ),
                send=recorder.sender(thread, turn_card_intent_id=turn_id, transport=transport),
                edit=_edit_message,
                urgent_edit=_urgent_edit_message,
                delete=_delete_message,
                agent_name=agent.name,
                fallback_active=lambda: transport.fallback_used,
                thread_stopped=watch.is_stopped,
                model_id=agent.model.id,
                turn_id=turn_id,
                markup=self.runtime.turn_deps.markup,
                cancel_view=CancelView(
                    allowed_user_id=message.author.id, cancel=cancel, turn_id=turn_id
                ),
                unprompted=unprompted,
                on_first_post=on_first_post,
                on_replacement=lambda msg: recorder.message(
                    thread, msg, turn_card_intent_id=turn_id
                ),
                waiting_for_tenant_slot=(
                    tenant_slot_waiter(self.turn_queue, tenant_id)
                    if self.runtime.settings.discord is not None
                    and self.runtime.settings.discord.tenant_cap_waiting_card
                    else None
                ),
            )

        turn_card_intent, lifecycle = await post_initial_turn_card(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            thread_id=str(thread.id),
            message_ids=admitted_message_ids or (str(message.id),),
            channel_id=parent_channel_id,
            make_lifecycle=_make_lifecycle,
            owner_key=self.runtime.owner_key,
        )
        self._committed_message_ids.update(admitted_message_ids or (str(message.id),))
        self._track_live_turn_card(turn_card_intent.id)
        turn_send = recorder.sender(
            thread, turn_card_intent_id=turn_card_intent.id, transport=transport
        )
        # Whichever lifecycle renders the turn: dead-session recovery swaps in a
        # fresh one, and the watermark write and error card must read THAT one.
        lifecycle_holder: list[DiscordTurnLifecycle] = [lifecycle]

        async def _end_card_with_error(text: str) -> None:
            """Turn the still-pending card into the error; raises only if nothing showed."""
            current = lifecycle_holder[0]
            if current.ended:
                # The card already became the answer or its own error card:
                # never overwrite it. The caller posts the error under it.
                raise RuntimeError("turn card already ended")
            if current.message_ref is not None:
                # No replacement post: retiring the intent below is only right
                # when the card itself now shows the error.
                await current.end_card(text)
            else:
                await turn_send(text)
            shown_on = current.message_ref
            if shown_on is not None and str(shown_on.id) != current.card_message_id:
                # An earlier edit replaced the card; the original may still show
                # its Stop button, so its intent stays for recovery.
                return
            try:
                await retire_terminal_turn_card(
                    self.runtime.sessionmaker,
                    intent_id=turn_card_intent.id,
                    expected_message_id=current.card_message_id,
                    no_post_confirmed=not current.first_post_attempted,
                )
            except Exception as exc:  # the error is on screen; recovery retires it later
                log.warning("turn.error_card_retire_failed", error_type=type(exc).__name__)

        surface.end_card = _end_card_with_error

        # Over a cap the turn waits here, behind its ordinary card, with the
        # same ⌛ a mention queued behind its thread's turn gets (a drained
        # follow-up already carries one). Stop ends the wait like any turn; the
        # max wait is a safeguard that ends the card with the ordinary error.
        # The ⌛ add and remove are background tasks, never awaited here, so a
        # slow reaction call cannot hold up Stop, the max wait or a granted slot.
        # Queue wording on the card itself stays opt-in (tenant_cap_waiting_card).
        assert self.runtime.settings.discord is not None
        marked: list[asyncio.Task[None]] = []

        def _mark_queued() -> None:
            marked.append(self._spawn(self._add_mention_reaction(message, "⌛")))

        async def _unmark_queued(added: asyncio.Task[None]) -> None:
            await asyncio.wait({added})  # removing before the add lands would leave ⌛ behind
            await self._remove_mention_reaction(message, "⌛")

        try:
            slot = await wait_for_slot(
                cancel,
                sessionmaker=self.runtime.sessionmaker,
                tenant_id=tenant_id,
                default_cap=self.runtime.settings.discord.max_concurrent_turns_per_tenant,
                on_queued=_mark_queued if not unprompted and content_override is None else None,
            )
        finally:
            for added in marked:
                self._spawn(_unmark_queued(added))
        if slot != "started":
            ended = {
                "cancelled": "Stopped.\nSend a message to start again.",
                "timed_out": QUEUE_TIMED_OUT_TEXT,
                "balance_depleted": admission_refusal_message(
                    "balance_depleted", self.runtime.settings
                ),
                "queue_full": TENANT_CAP_NOTICE,
            }[slot]
            if lifecycle.message_ref is not None:
                await lifecycle.edit_card(content=ended, embed=None, view=None)
            else:
                await turn_send(ended)
            await retire_terminal_turn_card(
                self.runtime.sessionmaker,
                intent_id=turn_card_intent.id,
                expected_message_id=lifecycle.card_message_id,
                no_post_confirmed=not lifecycle.first_post_attempted,
            )
            return

        if self.runtime.settings.discord.tenant_cap_waiting_card:
            await lifecycle.on_slot_started()

        # The per-turn ceiling clock starts here, once admission
        # has passed and the turn holds its slot, and covers session
        # bind/create (bind_session) plus the driver pump (run_prepared_turn)
        # as ONE shared budget -- admit() and the queue wait are outside it.
        turn_deadline_at = turn_deadline(
            now=self._ingress_clock() if self._ingress_clock is not None else datetime.now(UTC)
        )

        discord_settings = self.runtime.settings.discord
        assert discord_settings is not None, (
            "_orchestrate called without discord settings — entrypoint must validate at boot"
        )
        # Each caller in a thread keys their own session on their own account,
        # so nobody reuses another caller's session identity.
        session_account_id = admission.account_id

        origin_context = turn_origin(
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
            role=Role.ADMIN if admission.is_platform_admin else Role.USER,
            sender_foreign=admission.hold is not None and admission.hold.client,
            shared_channel=admission.shared_channel,
            is_setup=admission.config.thread_binding_kind == "setup",
            message_text=message.content,
            queued_message=content_override is not None,
            keep_fleet_root=self.runtime.turn_deps.fleet_enabled,
        )
        prepared_origin = None
        if self.runtime.turn_deps.fleet_enabled:
            from daimon.core.turn.runtime_manifest import read_agent_fleet_grant_or_off

            grant = await read_agent_fleet_grant_or_off(
                self.runtime.turn_deps, admission, now=datetime.now(UTC)
            )
            if grant is not None:
                prepared_origin = await origins.enter_async_context(origin_context)

        bind = bind_session
        if prepared_origin is not None:
            bind = functools.partial(bind_session, turn_card_intent_id=turn_card_intent.id)

        # --- Stage two: bind_session (find-or-create, mapping write,
        # recorder binding) -- bind_session(). ---
        try:
            prepared = await bind(
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
            owner_name = "the previous agent"
            if (
                admission.backend_revision is None
                or admission.backend_revision.backend != "selfhosted"
            ):
                owner_name = await _resolve_agent_display_name(
                    self.runtime.anthropic, tenant_id=tenant_id, ma_agent_id=error.source_agent_id
                )
            error_text = render_error(
                error,
                request_id=generate_request_id(),
                new_responder=agent.name,
                owner=owner_name,
                channel=f"<#{parent_channel_id}>",
                offer_button=not agent.id.startswith("sh_agent_"),
            )
            # Local import: thread_handoff imports this module.
            from daimon.adapters.discord.thread_handoff import hand_over_view

            view = (
                None
                if agent.id.startswith("sh_agent_")
                else hand_over_view(agent_id=agent.id, agent_name=agent.name)
            )
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
                await lifecycle.edit_card(content=None, embed=handoff_embed, view=view)
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
                await lifecycle.edit_card(content=failure_text, embed=None, view=None)
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
                await lifecycle.edit_card(content=busy_text, embed=None, view=None)
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
        watch.session_id = prepared.ma_session_id
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
            prepared.continuity.announces_replacement()
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
        if prepared.continuity.state == "replaced" and prepared.mapping_id is not None:
            from daimon.core.stores.thread_sessions import github_key_restart_line

            async with self.runtime.sessionmaker() as notice_session:
                key_restart = await github_key_restart_line(
                    notice_session, mapping_id=prepared.mapping_id
                )
            if key_restart is not None:
                replacement_summary = key_restart
                lifecycle.answer_prefix = key_restart
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
        marked_message_id: str | None = None
        if prepared.mapping_id is not None and lifecycle.final_message_id is not None:
            marked_message_id = lifecycle.final_message_id
            async with self.runtime.sessionmaker() as _at_session:
                await mark_turn_active(
                    _at_session,
                    id=prepared.mapping_id,
                    active_turn_message_id=lifecycle.final_message_id,
                    now=datetime.now(UTC),
                    owner_key=self.runtime.owner_key,
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
        # The turn's own status card is already posted and would read as an empty
        # message from the agent right before the query. Earlier turns' cards stay.
        own_card_ids = tuple(i for i in (lifecycle.card_message_id,) if i is not None)
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
                    excluded_message_ids=(
                        *(
                            _live_row_for_keys.delivered_answer_chunk_ids or ()
                            if _live_row_for_keys is not None
                            and _live_row_for_keys.id == prepared.mapping_id
                            else ()
                        ),
                        *own_card_ids,
                    ),
                    query_text=content_override,
                    query_message_ids=tuple(int(i) for i in admitted_message_ids),
                    query_messages=query_messages,
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
                    query_text=content_override,
                    query_message_ids=tuple(int(i) for i in admitted_message_ids),
                    query_messages=query_messages,
                    excluded_message_ids=own_card_ids,
                )
        else:
            if isinstance(message.channel, discord.TextChannel):
                user_message, _ = await build_channel_context_xml(
                    message.channel,
                    trigger=message,
                    thread=thread,
                    bot_user_id=self.user.id if self.user else None,
                    bot_display_name=discord_settings.bot_display_name,
                    is_admin=is_admin,
                    key_names=key_names,
                    query_text=content_override,
                    query_message_ids=tuple(int(i) for i in admitted_message_ids),
                    query_messages=query_messages,
                )
            else:
                # Forum/voice channels: fall back to raw message content
                user_message = content_override if content_override is not None else message.content

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

        # --- Run the turn (run_prepared_turn owns the driver
        # call and the one-shot dead-session recovery cycle). ---

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
                query_text=content_override,
                query_message_ids=tuple(int(i) for i in admitted_message_ids),
                query_messages=query_messages,
                excluded_message_ids=(
                    *own_card_ids,
                    *(i for i in (lifecycle_holder[0].card_message_id,) if i is not None),
                ),
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
                raise UserFacingError("This setup expired. Mention me again to continue.")
            return (
                render_turn_origin(
                    recovery_origin,
                    responder_handle=_responder_handle(self.runtime.settings),
                    session_state=session_state,
                    effective_is_admin=admission.is_admin,
                )
                + "\n"
                + full_message
            )

        def _recovery_lifecycle(cancel_event: asyncio.Event) -> TurnLifecycle:
            self._typed_stops.watch(thread.id, requester_id=message.author.id, cancel=cancel_event)
            new_lifecycle = DiscordTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                tenant_id=tenant_id,
                budget_channel_id=admission.budget_channel_id,
                requester_id=message.author.id,
                requester_is_admin=is_admin,
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                trigger_message=message,
                acknowledgment_managed=True,
                render_tables=tables_enabled(self.runtime.settings.table_rendering, tenant_id),
                send=turn_send,
                edit=_edit_message,
                urgent_edit=_urgent_edit_message,
                delete=_delete_message,
                agent_name=agent.name,
                fallback_active=lambda: transport.fallback_used,
                thread_stopped=watch.is_stopped,
                model_id=agent.model.id,
                turn_id=turn_card_intent.id,
                markup=self.runtime.turn_deps.markup,
                cancel_view=CancelView(
                    allowed_user_id=message.author.id,
                    cancel=cancel_event,
                    turn_id=turn_card_intent.id,
                ),
                # Take over the failed attempt's message so its upstream-error
                # embed is edited into this turn's answer rather than left
                # standing next to a second, successful message.
                adopt_message_ref=lifecycle.release_message_ref(),
                adopt_pending_progress=lifecycle,
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

        outcome: RunOutcome | None = None
        archive_after = False
        watch.phase = "running"
        try:
            async with contextlib.AsyncExitStack() as running_origin:
                if prepared_origin is None:
                    origin = await running_origin.enter_async_context(origin_context)
                else:
                    await running_origin.enter_async_context(origins.pop_all())
                    origin = prepared_origin
                outcome = await run_prepared_turn(
                    self.runtime.turn_deps,
                    prepared,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=str(thread.id),
                    external_user_id=str(message.author.id),
                    context_mention_ids=tuple(
                        str(u.id) for u in message.mentions if not getattr(u, "bot", True)
                    ),
                    context_message_text=message.content,
                    user_message=(
                        render_turn_origin(
                            origin,
                            responder_handle=_responder_handle(self.runtime.settings),
                            session_state=session_state,
                            effective_is_admin=admission.is_admin,
                        )
                        + "\n"
                        + user_message
                    ),
                    lifecycle=lifecycle,
                    turn_card_intent_id=turn_card_intent.id,
                    cancel=cancel,
                    reseed_user_message=_reseed_user_message,
                    recovery_lifecycle=_recovery_lifecycle,
                    image_blocks=image_blocks,
                    render_interval_s=2.0,
                    deadline=turn_deadline_at,
                    confirm_write=discord_confirmation_hook(
                        thread,
                        runtime=self.runtime,
                        tenant_id=tenant_id,
                        account_id=admission.account_id,
                        admission=admission,
                    ),
                    active_turn_message_id=marked_message_id,
                    **provider_action_options(
                        prepared,
                        lambda: discord_provider_action_hook(
                            thread,
                            thread_id=str(thread.id),
                            channel_id=thread.id,
                        ),
                    ),
                )
                # The run ended the card itself (answer, or red error card).
                surface.end_card = None
                archive_after = await self._archive_requested(origin.id)
        finally:
            # Runs on any exception, not just the happy path: whatever else
            # went wrong, the thread must not be left holding its active_turn
            # marker -- except when the shutdown drain cancelled the turn, whose
            # still-running session the next process's orphan sweep recovers
            # through that marker. Both ids are cleared -- a dead-session
            # recovery moves the marker onto its replacement mapping, which
            # `outcome` names. A ceiling breach is not
            # a special case here: run_prepared_turn already marked the
            # active mapping dead and returns a normal RunOutcome, so it takes
            # this same path.
            await self._interrupt_stopped_turn(
                thread.id,
                tenant_id,
                prepared,
                prepared.ma_session_id,
                outcome.ma_session_id if outcome is not None else None,
            )
            await release_turn_markers(
                self.runtime.sessionmaker,
                self.runtime.turn_deps.running_turns,
                {i: None for i in (prepared.mapping_id, outcome and outcome.mapping_id) if i},
            )
            if outcome is not None and not lifecycle_holder[0].card_discard_failed:
                await retire_terminal_turn_card(
                    self.runtime.sessionmaker,
                    intent_id=turn_card_intent.id,
                    expected_message_id=lifecycle_holder[0].card_message_id,
                    allow_prepared_without_message=not lifecycle_holder[0].first_post_attempted,
                    keep_fleet_root=self.runtime.turn_deps.fleet_enabled,
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
        if self._thread_stops.get(thread.id) is not None:
            # The thread is gone or closed: the turn was interrupted and there is no
            # answer, notice, reaction, file or archive left to deliver.
            return
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
            # --- Persist consumed-input boundary and delivered answer chunks ---
            if mapping_id is not None and final_lifecycle.watermark_message_id is not None:
                async with self.runtime.sessionmaker() as _wm_session:
                    await update_watermark(
                        _wm_session,
                        id=mapping_id,
                        watermark_message_id=final_lifecycle.watermark_message_id,
                        delivered_answer_chunk_ids=final_lifecycle.delivered_answer_chunk_ids,
                    )
                    await _wm_session.commit()
            # The lifecycle flag below excludes a cancelled turn, which also
            # reaches this branch with a non-None final_message_id -- seeding
            # the vote affordance under a cancellation notice is exactly what
            # this guard prevents. Not gated on mapping_id, which is about
            # session mapping, not whether the turn actually answered.
            if final_lifecycle.was_answered and final_lifecycle.feedback_message_id is not None:
                await seed_feedback_reactions(
                    thread, message_id=final_lifecycle.feedback_message_id
                )
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
        self._schedule_output_sweep(
            outcome,
            thread=thread,
            tenant_id=tenant_id,
            lifecycle=final_lifecycle,
            selfhosted=(
                prepared.admission.backend_revision is not None
                and prepared.admission.backend_revision.backend == "selfhosted"
            ),
        )
        if archive_after:
            await self._archive_after_outputs(outcome, thread)

"""DaimonBot -- Discord adapter event-driven controller."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

import anthropic as _anthropic
import sentry_sdk
import structlog
import structlog.contextvars
from daimon.adapters.discord import theme
from daimon.adapters.discord.attachments import build_attachment_url_prefix
from daimon.adapters.discord.checks import is_member_guild_admin
from daimon.adapters.discord.context import (
    build_channel_context_xml,
    build_context_xml,
    build_delta_xml,
)
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.feedback_seed import seed_feedback_reactions
from daimon.adapters.discord.gating import should_process_message
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.permissions import check_missing_permissions
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.thread_send import safe_thread_send
from daimon.adapters.discord.views import CancelView
from daimon.adapters.discord.vision import (
    build_image_url_prefix,
    build_skipped_image_prefix,
    download_as_image_blocks,
    is_vision_image_attachment,
)
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.provisioning import provision_tenant, reconcile_tenant_defaults
from daimon.core.defaults.report import compose_failure_reason
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.tenants import (
    get_tenant_liveness,
    list_tenants_by_platform,
    set_provision_status,
)
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    list_orphaned_turns,
    mark_dead,
    mark_turn_active,
    update_watermark,
)
from daimon.core.turn.admission import AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.gating import should_admit_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import RunOutcome, run_prepared_turn
from sqlalchemy.exc import SQLAlchemyError  # noqa: TCH002

import discord
from discord.ext import commands

log = structlog.get_logger()

_EMBED_COLOR = theme.COLOR_BLURPLE  # Blurple — repo standard (help.py D-FORMAT-01).

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

# Wall-clock ceiling on a single turn. Not a latency target -- legitimate
# agentic turns run many minutes (notebooks, model fits), so this is set well
# above the longest real turn and exists only to bound the pathological case.
#
# Without it a turn can hang forever: an MA session that never leaves `running`
# (e.g. a tool call whose result never comes back) leaves the await unresolved,
# so the clear_active_turn below it is never reached and the thread's
# active_turn marker is held for good. _retire_orphaned_turns cannot help --
# it runs once per process at on_ready and deliberately has no age filter,
# because it assumes "a turn cannot outlive the process rendering it". A hung
# await outlives the process just fine, which is how a thread ends up
# permanently unable to accept a mention until the next deploy.
#
# ponytail: a flat wall-clock deadline, not a liveness deadline. A turn
# streaming events is alive no matter how long it runs, and one silent for ten
# minutes is wedged regardless of total duration -- keying on time-since-last-
# event would cut the dead case loose sooner and never touch a live one. That
# needs the render loop to expose a last-event timestamp; upgrade to it if this
# ceiling ever fires on a real turn.
_TURN_DEADLINE_S: float = 45.0 * 60.0


def _resolve_bot_display_name(settings: Settings) -> str:
    """Read the operator-configured bot presented name (SPEC req 9).

    Defaults to "daimon" when Discord settings are unset — the same default
    ``DiscordSettings.bot_display_name`` carries, kept here too so callers
    that only have a bare ``Settings`` (discord block optional) never crash.
    """
    return settings.discord.bot_display_name if settings.discord is not None else "daimon"


def _setting_up_message(bot_display_name: str) -> str:
    """Distinct from the MAResolverMissError "no longer exists" message so a
    still-provisioning state is never confused with a genuine misconfiguration."""
    return f"{bot_display_name.capitalize()} is setting up this server — try again in a moment."


def _credit_depleted_message(bot_display_name: str) -> str:
    return (
        f"This server's {bot_display_name} credit is depleted. An admin can top up with `/billing`."
    )


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
            "to manage your agents."
        ),
        inline=False,
    )
    return embed


def _build_ready_embed() -> discord.Embed:
    """Terminal success follow-up. Pure — no I/O."""
    return discord.Embed(
        title="✅ Ready",
        description="Mention me anywhere, or run `/agent-setup`.",
        color=theme.COLOR_GREEN,
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


class DaimonBot(commands.Bot):
    """Discord bot process. Slash commands + turn pipeline."""

    def __init__(self, *, runtime: DiscordRuntime, intents: discord.Intents) -> None:
        super().__init__(command_prefix=[], intents=intents)  # type: ignore[arg-type]  # discord.py expects Iterable but [] is valid
        self.runtime = runtime
        # Per-thread concurrency state. _processing: thread IDs with an active turn.
        # _pending: mentions queued behind an in-flight turn for that thread.
        # Drained after the current turn finishes into a single composite follow-up
        # turn so the user doesn't lose messages they fired while the bot was busy.
        self._processing: set[int] = set()
        self._pending: dict[int, list[discord.Message]] = {}
        # Per-tenant concurrency cap (SCALE-01): active turn count keyed by tenant_id.
        # Incremented before the turn starts; decremented in a finally that brackets
        # the whole drain loop so the slot is always released.
        self._inflight: dict[uuid.UUID, int] = {}
        # In-flight seed guard: tenant_ids with a reconcile in progress.
        self._seeding: set[uuid.UUID] = set()
        # Track spawned background tasks so they aren't GC'd; discard on done.
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Drain flag — set by _drain_and_close on SIGTERM/SIGINT.
        # While True, on_message rejects new mentions; existing turns finish.
        self.draining: bool = False
        # One-shot guard for the orphaned-turn sweep. on_ready re-fires on every
        # full gateway reconnect, and a second run would reap the turns THIS
        # process is currently rendering.
        self._orphans_retired: bool = False

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Fire-and-forget a background task, tracked so it isn't GC'd."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(_log_bg_task_exception)
        return task

    async def _drain_and_close(self) -> None:
        """Graceful shutdown drain.

        Flips draining=True so on_message rejects new mentions, then polls the
        existing _processing set until it empties or the grace window elapses.
        Any cut turn surfaces as a retryable error (acceptable). Calls
        bot.close() unconditionally so the gateway disconnects cleanly.
        """
        self.draining = True
        log.info("discord.draining", inflight_threads=len(self._processing))
        deadline = asyncio.get_running_loop().time() + _DRAIN_GRACE_S
        while self._processing and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        log.info("discord.drain_complete", remaining=len(self._processing))
        await self.close()

    async def setup_hook(self) -> None:
        """Load command Cogs before on_ready syncs the tree."""
        from daimon.adapters.discord.commands.agent_setup import AgentSetupCog
        from daimon.adapters.discord.commands.billing import BillingCog
        from daimon.adapters.discord.commands.help import HelpCog
        from daimon.adapters.discord.commands.memory import MemoryCog
        from daimon.adapters.discord.commands.privacy import PrivacyCog
        from daimon.adapters.discord.commands.routines import RoutinesCog
        from daimon.adapters.discord.feedback_reactions import FeedbackReactionCog

        await self.add_cog(HelpCog(self))
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

        self.add_dynamic_items(FeedbackButton)

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
                    await self._post_to_guild(guild, _build_ready_embed())
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
        except Exception as exc:  # noqa: BLE001 — background-task supervisor boundary
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

    async def _ensure_provisioning(self, guild: discord.Guild) -> None:
        """Self-heal an unprovisioned/archived guild: provision + un-archive + bg seed."""
        guild_id = str(guild.id)
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
        self._spawn(
            self._seed_tenant_defaults(tenant_id=result.tenant_id, guild=guild, was_ready=False)
        )

    async def _retire_orphaned_turns(self) -> None:
        """Lay to rest every embed whose turn died with the previous process.

        A turn's render loop lives in the process that started it, so a deploy
        mid-turn freezes the embed on 'thinking' forever while MA completes and
        bills the answer server-side. The user sees a spinner that never stops
        and has no way to tell it is dead.

        Marking it failed is honest and cheap, and the alternative -- draining
        in-flight turns before the container exits -- needs a lameduck story the
        compose refresh does not have. This runs first in on_ready so a user
        reading the thread sees the truth before anything else happens.

        Failures to edit are swallowed per row: the message may be deleted, the
        thread archived, or permissions changed since. One unreachable embed
        must not stop the sweep clearing the rest, and the row is cleared either
        way so a permanently unreachable message is not retried on every boot.

        Runs at most once per process: on_ready re-fires on every full gateway
        reconnect, and a marker set by this process is a LIVE turn, not an
        orphan.
        """
        if self._orphans_retired:
            return
        self._orphans_retired = True
        async with self.runtime.sessionmaker() as session:
            orphans = await list_orphaned_turns(session, platform="discord")
        if not orphans:
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
                    await message.edit(
                        embed=discord.Embed(
                            color=theme.COLOR_RED,
                            description=(
                                "❌ This turn was interrupted by a restart and cannot be "
                                "resumed. Nothing was lost on your side — mention me again "
                                "to retry."
                            ),
                        ),
                        view=None,
                    )
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
            async with self.runtime.sessionmaker() as session:
                await clear_active_turn(session, id=row.id)
                await session.commit()

    async def _retire_deadlocked_turn(
        self,
        *,
        mapping_id: uuid.UUID | None,
        thread_id: str,
        session_id: str | None,
        message_ref: Any | None,
    ) -> None:
        """Lay to rest a turn that blew `_TURN_DEADLINE_S`.

        The session is wedged, not merely slow: it will never reach a terminal
        state, so the mapping is marked dead. Without that the next mention
        binds the same dead MA session and hangs again — the thread would come
        unstuck for exactly one message.

        Clearing the active_turn marker is deliberately NOT done here; the
        caller's `finally` owns it, so it also runs for failures that are not
        deadline-related.
        """
        log.error(
            "turn.deadline_exceeded",
            thread_id=thread_id,
            session_id=session_id,
            deadline_s=_TURN_DEADLINE_S,
        )
        if mapping_id is not None:
            async with self.runtime.sessionmaker() as session:
                await mark_dead(session, id=mapping_id)
                await session.commit()
        if message_ref is None:
            return
        try:
            await message_ref.edit(
                embed=discord.Embed(
                    color=theme.COLOR_RED,
                    description=(
                        "❌ This turn stopped responding and was abandoned. Nothing was "
                        "lost on your side — mention me again to start fresh."
                    ),
                ),
                view=None,
            )
        except (discord.HTTPException, discord.ClientException) as err:
            # The spinner may be deleted or the thread archived. The marker is
            # cleared by the caller either way, which is what actually unsticks
            # the thread; a stale embed is cosmetic by comparison.
            log.warning("turn.deadline_embed_failed", thread_id=thread_id, error=str(err))

    async def on_ready(self) -> None:
        """Forward-only reconcile sweep: provision-if-missing, re-seed pending/failed,
        sync the command tree. NO archive-on-absence."""
        log.info("bot_ready", user=str(self.user))
        await self._retire_orphaned_turns()
        tenants = await list_tenants_by_platform(self.runtime.sessionmaker, platform="discord")
        known_guild_ids = {tr.external_id for tr in tenants}
        sem = asyncio.Semaphore(_SWEEP_CONCURRENCY)

        async def _bounded_seed(
            *, tenant_id: uuid.UUID, guild: discord.Guild, was_ready: bool
        ) -> None:
            async with sem:
                await self._seed_tenant_defaults(
                    tenant_id=tenant_id, guild=guild, was_ready=was_ready
                )

        # Provision guilds joined while the bot was down.
        for guild in self.guilds:
            ws_id = str(guild.id)
            if ws_id in known_guild_ids:
                continue
            result = await provision_tenant(
                self.runtime.sessionmaker,
                platform="discord",
                workspace_id=ws_id,
                signup_credit=self.runtime.settings.billing.signup_credit,
            )
            await set_provision_status(
                self.runtime.sessionmaker,
                tenant_id=result.tenant_id,
                status="pending",
                clear_archive=True,  # #132: rejoined guilds must not stay archived
            )
            await self._post_to_guild(
                guild, _build_welcome_embed(_resolve_bot_display_name(self.runtime.settings))
            )
            self._spawn(_bounded_seed(tenant_id=result.tenant_id, guild=guild, was_ready=False))

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
            self._spawn(
                _bounded_seed(
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
                clear_archive=True,  # #132: rejoined guilds must not stay archived
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
        self._spawn(
            self._seed_tenant_defaults(tenant_id=result.tenant_id, guild=guild, was_ready=False)
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Soft-archive: stamp archived_at=now(). NO row delete."""
        guild_id = str(guild.id)
        log.warning("guild_removed", guild_id=guild_id, guild_name=guild.name)
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
        await set_provision_status(self.runtime.sessionmaker, tenant_id=tenant_id, archive=True)

    def _release_inflight(self, tenant_id: uuid.UUID) -> None:
        """Release one per-tenant in-flight slot, dropping the key at zero."""
        self._inflight[tenant_id] = self._inflight.get(tenant_id, 1) - 1
        if self._inflight[tenant_id] <= 0:
            self._inflight.pop(tenant_id, None)

    async def on_message(self, message: discord.Message) -> None:
        """Gate on mention, resolve TenantContext once + run the non-ready self-heal gate,
        then orchestrate a turn in a thread."""
        discord_settings = self.runtime.settings.discord
        if not should_process_message(
            author_is_bot=message.author.bot,
            author_id=str(message.author.id),
            # Explicit @-mention only. `mentioned_in` returns True for @everyone/@here
            # (it short-circuits on message.mention_everyone), which would make the bot
            # reply to every mass ping. message.mentions excludes @everyone/@here and
            # role mentions, so this triggers only on a direct user mention of the bot.
            bot_mentioned=self.user is not None
            and any(user.id == self.user.id for user in message.mentions),
            guild_id=str(message.guild.id) if message.guild else None,
            self_user_id=str(self.user.id) if self.user is not None else None,
            qa_bot_user_ids=discord_settings.qa_bot_user_ids if discord_settings else (),
        ):
            return
        if self.draining:
            return  # stop admitting new mentions during drain
        assert message.guild is not None
        guild = message.guild
        guild_id = str(guild.id)
        bot_display_name = _resolve_bot_display_name(self.runtime.settings)

        # --- Unified non-ready self-heal gate through turn completion,
        # guarded end-to-end. A DB hiccup or an unclassified bug
        # anywhere in this block must never leave the mention silently dropped —
        # it always produces a best-effort error message and never re-raises out
        # of the event handler. In practice this backstop only fires for
        # liveness-read/mutex-bookkeeping failures, since the turn-execution path
        # already has its own boundary in _handle_mention.
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
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

            # Per-thread mention queueing. Check before claiming an in-flight slot
            # so that queued mentions never consume a slot they won't use.
            thread_id = message.channel.id
            if thread_id in self._processing:
                await message.add_reaction("⌛")
                self._pending.setdefault(thread_id, []).append(message)
                return

            # --- Per-tenant concurrency cap (SCALE-01) ---
            # Read-check-increment in one synchronous span (no await between read
            # and increment) to avoid a race where two coroutines both read 0 and
            # both increment past the cap. The queue check above is also synchronous,
            # so there is exactly one increment per coroutine that reaches this point
            # and one matching decrement in the finally block below.
            cap = self.runtime.settings.discord.max_concurrent_turns_per_tenant
            count = self._inflight.get(tenant_id, 0)
            if not should_admit_turn(current_in_flight=count, cap=cap):
                await message.channel.send(
                    "This server has too many chats in flight right now — try again in a moment."
                )
                return
            self._inflight[tenant_id] = count + 1

            # Channel-level mentions each open their own thread + MA session, so
            # they run in parallel — no serialization (bounded only by the
            # per-tenant concurrency cap claimed above). Serializing them by channel
            # id wedged the whole channel whenever a single turn stalled (e.g. an
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
                        self._processing.discard(created_id)
                        self._pending.pop(created_id, None)
                    self._release_inflight(tenant_id)
                return

            thread_id = message.channel.id
            if thread_id in self._processing:
                await message.add_reaction("⌛")
                self._pending.setdefault(thread_id, []).append(message)
                # This coroutine won't run a turn; the slot was claimed for the
                # already-processing path which will do the work.
                self._release_inflight(tenant_id)
                return

            self._processing.add(thread_id)
            try:
                await self._handle_mention(message, guild_id, tenant_id)
                await self._drain_pending_mentions(thread_id, guild_id, tenant_id)
            finally:
                self._processing.discard(thread_id)
                self._pending.pop(thread_id, None)
                self._release_inflight(tenant_id)
        except (DaimonError, _anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            await self._handle_prologue_failure(message, exc, guild_id)
        except Exception as exc:  # noqa: BLE001 — on_message event-handler boundary
            await self._handle_prologue_failure(message, exc, guild_id)

    async def _handle_prologue_failure(
        self, message: discord.Message, exc: Exception, guild_id: str
    ) -> None:
        """Best-effort error render for on_message prologue failures (#170 backstop).

        Never raises — a failure here would defeat the whole point of the
        boundary it's called from. Mirrors _flip_failed_best_effort's
        try/log-only shape for the send itself.
        """
        log.exception(
            "mention_prologue_failed", guild_id=guild_id, channel_id=str(message.channel.id)
        )
        sentry_sdk.capture_exception(exc)
        rid = generate_request_id()
        try:
            await message.channel.send(render_error(exc, request_id=rid))
        except discord.HTTPException:
            log.exception("mention_prologue_error_send_failed", guild_id=guild_id)

    async def _drain_pending_mentions(
        self, thread_id: int, guild_id: str, tenant_id: uuid.UUID
    ) -> None:
        """Drain ``self._pending[thread_id]`` into composite per-author follow-up turns.

        New mentions can arrive during a drain turn; they land in
        ``self._pending`` and get picked up by the next iteration.

        G1 (SCOPING §2c/§4): partition queued messages by author.id and run
        one composite turn per author in first-seen arrival order. Under
        per-caller sessions each author's turn resolves their own
        account/session from ``msgs[0].author`` — coalescing distinct authors
        onto one turn would route B's message onto A's session (the
        confused-deputy hole relocated to the hot path). One turn = one caller.

        Never raises: ``_handle_mention`` renders turn errors internally, so a
        failed drain turn still returns normally and the loop continues.
        """
        while queued := self._pending.pop(thread_id, []):
            by_author: dict[int, list[discord.Message]] = {}
            for q_msg in queued:
                by_author.setdefault(q_msg.author.id, []).append(q_msg)
            for author_msgs in by_author.values():
                await self._handle_mention(
                    author_msgs[0],
                    guild_id,
                    tenant_id,
                    content_override=_compose_queued_content(author_msgs),
                    # merge attachments from ALL of this author's
                    # queued messages (including author_msgs[0]'s own), in
                    # first-seen arrival order -- author_msgs[0].attachments
                    # alone would silently drop attachments on later messages.
                    attachments_override=[a for m in author_msgs for a in m.attachments],
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
            )
        except (DaimonError, _anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
            log.warning("turn.failed", error=str(exc), channel_id=str(message.channel.id))
            await self._render_turn_error(message, tenant_id, guild_id, rid, exc)
        except Exception as exc:  # noqa: BLE001 — mention-turn adapter boundary
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
        if isinstance(target, discord.Thread):
            await safe_thread_send(target, error_text)
        else:
            await target.send(error_text)

    async def _orchestrate(
        self,
        message: discord.Message,
        guild_id: str,
        tenant_id: uuid.UUID,
        *,
        content_override: str | None = None,
        created_thread_ids: list[int] | None = None,
        attachments_override: list[discord.Attachment] | None = None,
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
        """
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

        # --- Stage one: admission (identity, config cascade, missing-config
        # bail, MA resolve/retrieve, balance gate, cap gate) -- D-01 admit(). ---
        try:
            admission = await admit(
                self.runtime.turn_deps,
                tenant_id=tenant_id,
                platform="discord",
                external_user_id=str(message.author.id),
                channel_id=parent_channel_id,
                now=datetime.now(UTC),
            )
        except MissingTurnConfigError as err:
            log.info(
                "missing_config",
                guild_id=guild_id,
                channel_id=parent_channel_id,
                missing=list(err.missing),
            )
            target = thread or message.channel
            hints: list[str] = []
            if "agent" in err.missing:
                hints.append(
                    "An admin can set the default agent in `/agent-setup` -> "
                    "**Set as default...** -> [This channel] or [Whole server]."
                )
            if "environment" in err.missing:
                hints.append(
                    "Environment is operator-only -- an operator can set it via the CLI "
                    "(`daimon config set environment_name=...`)."
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
            target = thread or message.channel
            await target.send(
                "The configured agent or environment no longer exists. "
                "An admin can re-set the agent in `/agent-setup` -> "
                "**Set as default...**; the environment is "
                "operator-only via the CLI (`daimon config set environment_name=...`)."
            )
            return
        except AdmissionDenied as err:
            target = thread or message.channel
            if err.reason == "balance_depleted":
                log.info("turn.skipped.over_balance", guild_id=guild_id, tenant_id=str(tenant_id))
                await target.send(
                    _credit_depleted_message(_resolve_bot_display_name(self.runtime.settings))
                )
            else:
                log.info(
                    "turn.skipped.over_cap",
                    guild_id=guild_id,
                    user_id=str(message.author.id),
                )
                await target.send(
                    "Monthly usage cap reached for this guild. "
                    "An admin can adjust the cap with `/billing` (when available)."
                )
            return

        agent = admission.agent

        # --- Derive is_admin from Discord-native permissions ---
        # author is Union[User, Member]; guild_permissions is only on Member.
        # Non-Member (DM edge case) defaults to False.
        author = message.author
        is_admin = isinstance(author, discord.Member) and is_member_guild_admin(
            author, guild_owner_id=message.guild.owner_id if message.guild else None
        )

        # --- Per-turn role upsert: sync account.role from live Discord perms ---
        # This write is UNCONDITIONAL — it is NOT gated by per_caller_thread_sessions.
        # The admin-via-live-role mechanism (this write + the MCP gate) ships
        # active on every deploy regardless of the session-keying flag (B4 disposition).
        #
        # (a) Runs BEFORE run_turn so the live-role gate reads the fresh DB role
        #     when this turn's MCP calls arrive — ensuring the first post-deploy admin turn
        #     already has role=ADMIN when the gate evaluates.
        # (b) Idempotent per-(tenant, account_id) write: the value is derived solely from the
        #     caller's current guild-admin status, so concurrent same-account turns write the
        #     IDENTICAL value. No lock is needed — accounts are tenant-scoped so admin status
        #     is singular within a tenant (B1).
        # (c) Targets only admission.account_id — by construction a platform-principal account
        #     created by admit()'s identity resolution. CLI/operator accounts are distinct
        #     rows and are never touched by this write (T-88-04-03).
        async with self.runtime.sessionmaker() as _role_session:
            await set_role(
                _role_session,
                admission.account_id,
                Role.ADMIN if is_admin else Role.USER,
            )
            await _role_session.commit()

        # --- Create thread + status embed BEFORE session create ---
        # MA sessions.create can hold its HTTP response for minutes while it
        # provisions the session (the record exists server-side in ~1s; the
        # response is what stalls). The thread and a thinking embed go up
        # first so the user gets instant feedback; the lifecycle adopts the
        # embed and edits it in place once SSE events flow.
        is_thread_mention = thread is not None
        if thread is None:
            thread = await message.create_thread(
                name=f"Chat with {agent.name}",
                auto_archive_duration=10080,
            )
            # Register the thread as processing IMMEDIATELY — no await between
            # create_thread returning and this line. Registration precedes the
            # session lookup/create and the mapping commit below, so an
            # in-thread mention that arrives during that window queues instead
            # of racing a second turn onto this thread's (about-to-exist)
            # session. Cleanup is owned entirely by
            # on_message's channel-branch finally, not here — this call does
            # NOT discard/pop, so there is no path where a queued mention is
            # dropped without being drained.
            self._processing.add(thread.id)
            if created_thread_ids is not None:
                created_thread_ids.append(thread.id)

        # --- Wire lifecycle with send/edit callables ---
        async def _send_embed(**kwargs: Any) -> discord.Message:
            return await thread.send(**kwargs)

        async def _edit_message(msg: discord.Message, **kwargs: Any) -> None:
            await msg.edit(**kwargs)

        cancel = asyncio.Event()
        cancel_view = CancelView(allowed_user_id=message.author.id, cancel=cancel)
        lifecycle = DiscordTurnLifecycle(
            send=_send_embed,
            edit=_edit_message,
            agent_name=agent.name,
            model_id=agent.model.id,
            cancel_view=cancel_view,
        )
        await lifecycle.post_initial()

        # Compute the account_id used to key thread-session lookup and create.
        # When per_caller_thread_sessions is ON (default): use the caller's real
        # account_id so each caller in a thread gets their own durable session
        # (closing the #162 confused-deputy hole — a low-priv caller never
        # reuses the starter's session). When OFF (opt-out): use a
        # deterministic per-(tenant,thread) uuid5 as a sentinel that is
        # identical for every caller in this thread, preserving the legacy
        # single-session-per-thread behavior byte-for-byte.
        #
        # The sentinel is a uuid5 derived from NAMESPACE_URL — real accounts use
        # random uuid4, so the sentinel can NEVER collide with any real account row
        # (W1). The formula is stable across restarts so the OFF path always reuses
        # one session per thread deterministically.
        discord_settings = self.runtime.settings.discord
        assert discord_settings is not None, (
            "_orchestrate called without discord settings — entrypoint must validate at boot"
        )
        if discord_settings.per_caller_thread_sessions:
            session_account_id = admission.account_id
        else:
            session_account_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"legacy-thread-sentinel:{tenant_id}:{thread.id}",
            )

        # --- Stage two: bind_session (find-or-create, mapping write,
        # recorder binding) -- D-01 bind_session(). ---
        prepared = await bind_session(
            self.runtime.turn_deps,
            admission,
            tenant_id=tenant_id,
            platform="discord",
            external_user_id=str(message.author.id),
            thread_id=str(thread.id),
            session_account_id=session_account_id,
            reuse_existing=is_thread_mention,
        )

        log.info(
            "session.ready",
            session_id=prepared.ma_session_id,
            thread_id=thread.id,
            reused=prepared.reused,
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
                )
            else:
                user_message, _ = await build_context_xml(
                    thread,
                    trigger=message,
                    limit=100,
                    bot_user_id=self.user.id if self.user else None,
                    bot_display_name=discord_settings.bot_display_name,
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
            await target.send(
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
            )
            if synthetic_prefix:
                full_message = synthetic_prefix + "\n" + full_message
            return full_message

        def _recovery_lifecycle(cancel_event: asyncio.Event) -> TurnLifecycle:
            new_lifecycle = DiscordTurnLifecycle(
                send=_send_embed,
                edit=_edit_message,
                agent_name=agent.name,
                model_id=agent.model.id,
                cancel_view=CancelView(allowed_user_id=message.author.id, cancel=cancel_event),
                # Take over the failed attempt's message so its upstream-error
                # embed is edited into this turn's answer rather than left
                # standing next to a second, successful message.
                adopt_message_ref=lifecycle.message_ref,
            )
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
        try:
            outcome = await asyncio.wait_for(
                run_prepared_turn(
                    self.runtime.turn_deps,
                    prepared,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=str(thread.id),
                    external_user_id=str(message.author.id),
                    user_message=user_message,
                    lifecycle=lifecycle,
                    cancel=cancel,
                    reseed_user_message=_reseed_user_message,
                    recovery_lifecycle=_recovery_lifecycle,
                    image_blocks=image_blocks,
                    render_interval_s=2.0,
                ),
                timeout=_TURN_DEADLINE_S,
            )
        except TimeoutError:
            await self._retire_deadlocked_turn(
                mapping_id=prepared.mapping_id,
                thread_id=str(thread.id),
                session_id=prepared.ma_session_id,
                message_ref=lifecycle.message_ref,
            )
            return
        finally:
            # Runs on the deadline and on any exception, not just the happy
            # path: whatever else went wrong, the thread must not be left
            # holding its active_turn marker. Both ids are cleared -- recovery
            # moves the turn to a new mapping row and leaves the marker behind
            # on the old one, which is still pointing at this same message.
            _done_ids = {prepared.mapping_id}
            if outcome is not None:
                _done_ids.add(outcome.mapping_id)
            for _done_id in _done_ids - {None}:
                if _done_id is None:  # pragma: no cover - set difference guarantees this
                    continue
                async with self.runtime.sessionmaker() as _ct_session:
                    await clear_active_turn(_ct_session, id=_done_id)
                    await _ct_session.commit()

        if outcome is None:  # pragma: no cover - the deadline branch above returns
            return

        state = outcome.state
        mapping_id = outcome.mapping_id
        final_lifecycle = lifecycle_holder[0]

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

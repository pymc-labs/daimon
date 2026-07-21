"""DaimonBot -- Discord adapter event-driven controller."""

from __future__ import annotations

import asyncio
import functools
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
from daimon.core.billing import is_over_cap
from daimon.core.defaults.provisioning import provision_tenant, reconcile_tenant_defaults
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError, resolve_agent, resolve_environment
from daimon.core.pricing import MODEL_PRICING
from daimon.core.scope import ResolvedConfig, ScopeContext
from daimon.core.sessions import create_session
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from daimon.core.stores.tenants import (
    get_tenant_liveness,
    list_tenants_by_platform,
    set_provision_status,
)
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    mark_dead,
    update_watermark,
)
from daimon.core.tenant_balance import is_over_balance
from daimon.core.turn.driver import run_turn
from daimon.core.turn.gating import should_admit_turn
from daimon.core.turn.state import TurnState
from daimon.core.usage_recording import record_turn_usage
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

# Distinct from the MAResolverMissError "no longer exists" message so a
# still-provisioning state is never confused with a genuine misconfiguration.
_SETTING_UP_MESSAGE = "Daimon is setting up this server — try again in a moment."

# Bounded concurrency for the on_ready re-seed sweep.
_SWEEP_CONCURRENCY = 4


def _is_dead_session(state: TurnState) -> bool:
    """Return True if state.error signals a gone/expired MA session (HTTP 404).

    A 404 not_found_error from events.send means the session existed but is now
    gone (deleted / expired / GC'd). The bot recreates silently.
    A 400 means a malformed session id — not reachable with well-formed stored ids
    and must surface as a normal turn error (do NOT recreate on it).
    """
    err = state.error
    if err is None or err.kind != "upstream":
        return False
    cause = err.cause
    return isinstance(cause, _anthropic.APIStatusError) and cause.status_code == 404


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


def _build_welcome_embed() -> discord.Embed:
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
        value=("Mention `@daimon` anywhere to chat, or run `/agent-setup` to manage your agents."),
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

        await self.add_cog(HelpCog(self))
        await self.add_cog(AgentSetupCog(self))
        await self.add_cog(RoutinesCog(self))
        await self.add_cog(BillingCog(self))
        await self.add_cog(PrivacyCog(self))
        await self.add_cog(MemoryCog(self))

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

    async def _flip_failed_best_effort(self, tenant_id: uuid.UUID) -> None:
        """Best-effort pending→failed flip. A DB hiccup can make the flip itself
        raise; swallowing it here keeps the snag embed posting and the seed handler
        alive. The on_ready sweep is the designed backstop if the flip is lost."""
        try:
            await set_provision_status(
                self.runtime.sessionmaker, tenant_id=tenant_id, status="failed"
            )
        except SQLAlchemyError:
            log.exception("guild_seed_status_flip_failed", tenant_id=str(tenant_id))

    async def _seed_tenant_defaults(self, *, tenant_id: uuid.UUID, guild: discord.Guild) -> None:
        """Background MA seed. Owns the pending→ready/failed status flip.
        Posts the ✅/⚠️ follow-up on terminal state. In-flight guard prevents duplicate seeds.
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
                self.runtime.settings.defaults_root,
                tenant_id=tenant_id,
                public_url=public_url,
            )
            if report.is_failure():
                await set_provision_status(
                    self.runtime.sessionmaker, tenant_id=tenant_id, status="failed"
                )
            else:
                await set_provision_status(
                    self.runtime.sessionmaker, tenant_id=tenant_id, status="ready"
                )
            embed = _build_snag_embed() if report.is_failure() else _build_ready_embed()
            await self._post_to_guild(guild, embed)
        except (DaimonError, _anthropic.APIError, discord.HTTPException) as exc:
            log.warning("guild_seed_failed", tenant_id=str(tenant_id), error=str(exc))
            # Best-effort flip before posting so the tenant is never left wedged in
            # 'pending' — a raise inside this handler would NOT be caught by the
            # sibling except clause below and would skip the snag embed entirely.
            await self._flip_failed_best_effort(tenant_id)
            await self._post_to_guild(guild, _build_snag_embed())
        except Exception:  # noqa: BLE001 — background-task supervisor boundary
            log.exception("guild_seed_unexpected", tenant_id=str(tenant_id))
            await self._flip_failed_best_effort(tenant_id)
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
        self._spawn(self._seed_tenant_defaults(tenant_id=result.tenant_id, guild=guild))

    async def on_ready(self) -> None:
        """Forward-only reconcile sweep: provision-if-missing, re-seed pending/failed,
        sync the command tree. NO archive-on-absence."""
        log.info("bot_ready", user=str(self.user))
        tenants = await list_tenants_by_platform(self.runtime.sessionmaker, platform="discord")
        known_guild_ids = {tr.external_id for tr in tenants}
        sem = asyncio.Semaphore(_SWEEP_CONCURRENCY)

        async def _bounded_seed(*, tenant_id: uuid.UUID, guild: discord.Guild) -> None:
            async with sem:
                await self._seed_tenant_defaults(tenant_id=tenant_id, guild=guild)

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
            await self._post_to_guild(guild, _build_welcome_embed())
            self._spawn(_bounded_seed(tenant_id=result.tenant_id, guild=guild))

        # Re-seed any listed tenant stuck in pending/failed; per-guild permission
        # check + tree sync for joined guilds.
        for tr in tenants:
            guild = self.get_guild(int(tr.external_id))
            if guild is None:
                log.warning("registered_guild_not_joined", external_id=tr.external_id)
                continue
            if tr.provision_status in ("pending", "failed"):
                self._spawn(_bounded_seed(tenant_id=tr.id, guild=guild))
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
            await self._post_to_guild(guild, _build_welcome_embed())
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
        self._spawn(self._seed_tenant_defaults(tenant_id=result.tenant_id, guild=guild))

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
        if not should_process_message(
            author_is_bot=message.author.bot,
            # Explicit @-mention only. `mentioned_in` returns True for @everyone/@here
            # (it short-circuits on message.mention_everyone), which would make the bot
            # reply to every mass ping. message.mentions excludes @everyone/@here and
            # role mentions, so this triggers only on a direct user mention of the bot.
            bot_mentioned=self.user is not None
            and any(user.id == self.user.id for user in message.mentions),
            guild_id=str(message.guild.id) if message.guild else None,
        ):
            return
        if self.draining:
            return  # stop admitting new mentions during drain
        assert message.guild is not None
        guild = message.guild
        guild_id = str(guild.id)

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
                await message.channel.send(_SETTING_UP_MESSAGE)
                return
            if tr.provision_status == "failed":
                # Self-heal: re-seed if idle (in-flight guard). NEVER show "failed" to the user.
                self._spawn(self._seed_tenant_defaults(tenant_id=tr.id, guild=guild))
                await message.channel.send(_SETTING_UP_MESSAGE)
                return
            if tr.provision_status == "pending":
                await message.channel.send(_SETTING_UP_MESSAGE)
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

        # --- Identity resolution ---
        async with self.runtime.sessionmaker() as session:
            principal = await get_or_create_platform_principal(
                session,
                tenant_id=tenant_id,
                platform="discord",
                external_id=str(message.author.id),
            )
            await session.commit()

        # --- Config resolution (per turn) ---
        scope = ScopeContext(
            account_id=principal.account_id,
            tenant_id=tenant_id,
            channel_id=parent_channel_id,
        )
        async with self.runtime.sessionmaker() as session:
            config = await resolve_config(
                session, context=scope, default=self.runtime.deployment_default
            )

        # --- Missing config check (before thread creation) ---
        if config.agent_name is None or config.environment_name is None:
            missing: list[str] = []
            if config.agent_name is None:
                missing.append("agent")
            if config.environment_name is None:
                missing.append("environment")
            log.info(
                "missing_config",
                guild_id=guild_id,
                channel_id=parent_channel_id,
                missing=missing,
            )
            target = thread or message.channel
            hints: list[str] = []
            if config.agent_name is None:
                hints.append(
                    "An admin can set the default agent in `/agent-setup` -> "
                    "**Set as default...** -> [This channel] or [Whole server]."
                )
            if config.environment_name is None:
                hints.append(
                    "Environment is operator-only -- an operator can set it via the CLI "
                    "(`daimon config set environment_name=...`)."
                )
            await target.send(
                f"No {' or '.join(missing)} configured for this channel. " + " ".join(hints)
            )
            return

        # --- Resolve agent and environment via ma_resolver (self-heals on
        # archive / recreate). Resolver returns live MA ids; we re-retrieve
        # the full SDK objects so downstream code (thread name, create_session)
        # gets BetaManagedAgentsAgent / BetaEnvironment with all fields.
        async def _resolve_ids(cfg: ResolvedConfig) -> tuple[str, str]:
            assert cfg.agent_name is not None and cfg.environment_name is not None
            # Self-heal reconciles must thread public_url like the guild-join seed
            # does — without it the re-seeded agent loses its daimon-mcp entry.
            resolve_public_url = (
                str(self.runtime.settings.mcp.public_url)
                if self.runtime.settings.mcp.public_url is not None
                else None
            )
            agent_id = await resolve_agent(
                self.runtime.anthropic,
                tenant_id=tenant_id,
                daimon_tag=cfg.agent_name,
                cached_id=None,
                apply_callable=lambda: reconcile_tenant_defaults(
                    self.runtime.anthropic,
                    self.runtime.settings.defaults_root,
                    tenant_id=tenant_id,
                    public_url=resolve_public_url,
                ),
                cache=self.runtime.resolver_cache,
            )
            env_id = await resolve_environment(
                self.runtime.anthropic,
                tenant_id=tenant_id,
                daimon_tag=cfg.environment_name,
                cached_id=None,
                apply_callable=lambda: reconcile_tenant_defaults(
                    self.runtime.anthropic,
                    self.runtime.settings.defaults_root,
                    tenant_id=tenant_id,
                    public_url=resolve_public_url,
                ),
                cache=self.runtime.resolver_cache,
            )
            return agent_id, env_id

        try:
            agent_id, env_id = await _resolve_ids(config)
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
        agent = await self.runtime.anthropic.beta.agents.retrieve(agent_id)
        env = await self.runtime.anthropic.beta.environments.retrieve(env_id)

        # --- Admission gate: per-tenant balance — independent of Stripe config ---
        if await is_over_balance(sessionmaker=self.runtime.sessionmaker, tenant_id=tenant_id):
            log.info("turn.skipped.over_balance", guild_id=guild_id, tenant_id=str(tenant_id))
            target = thread or message.channel
            await target.send(
                "This server's daimon credit is depleted. An admin can top up with `/billing`."
            )
            return

        # --- Admission gate: monthly usage cap ---
        over_cap = await is_over_cap(
            billing_config=self.runtime.billing_config,
            sessionmaker=self.runtime.sessionmaker,
            tenant_id=tenant_id,
            user_id=str(message.author.id),
            now=datetime.now(UTC),
        )
        if over_cap:
            log.info(
                "turn.skipped.over_cap",
                guild_id=guild_id,
                user_id=str(message.author.id),
            )
            target = thread or message.channel
            await target.send(
                "Monthly usage cap reached for this guild. "
                "An admin can adjust the cap with `/billing` (when available)."
            )
            return

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
        # (c) Targets only principal.account_id — by construction a platform-principal account
        #     created by get_or_create_platform_principal. CLI/operator accounts are distinct
        #     rows and are never touched by this write (T-88-04-03).
        async with self.runtime.sessionmaker() as _role_session:
            await set_role(
                _role_session,
                principal.account_id,
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

        # --- Session find-or-create (session-per-thread reuse) ---
        # The per-thread mention mutex (bot.py:449-476) already serialises
        # concurrent mentions for the same thread, so there is no race in the
        # lookup-or-create below. Do NOT add an asyncio.Lock here.

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
            session_account_id = principal.account_id
        else:
            session_account_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"legacy-thread-sentinel:{tenant_id}:{thread.id}",
            )

        agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
        # Fernet decrypts the per-agent GitHub PAT so a bound repo clones into
        # the session workspace. None when no crypto keys are configured
        # (build_multifernet raises on empty) — repo injection is then skipped.
        crypto_keys = tuple(
            secret.get_secret_value() for secret in self.runtime.settings.crypto.keys
        )
        fernet = build_multifernet(crypto_keys) if crypto_keys else None

        # For thread mentions, check for a live mapping before creating.
        # Channel mentions always create (their thread was just created above,
        # so no mapping can exist for it yet).
        ma_session_id: str = ""  # overwritten in every live code path below
        mapping_id: uuid.UUID | None = None
        watermark: str | None = None
        reused = False

        if is_thread_mention:
            # Thread mention: look up existing live session for this thread.
            async with self.runtime.sessionmaker() as _lookup_session:
                existing = await get_live_thread_session(
                    _lookup_session,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=str(thread.id),
                    account_id=session_account_id,
                )
            if existing is not None:
                ma_session_id = existing.ma_session_id
                mapping_id = existing.id
                watermark = existing.watermark_message_id
                reused = True
                log.info(
                    "session.reused",
                    session_id=ma_session_id,
                    thread_id=thread.id,
                    watermark=watermark,
                )

        if not reused:
            # First turn for this thread, or channel mention — create a new session.
            ma_session = await create_session(
                self.runtime.anthropic,
                agent=agent,
                environment=env,
                mcp_settings=self.runtime.settings.mcp,
                account_id=principal.account_id,
                tenant_id=tenant_id,
                agent_uuid=agent_uuid,
                session_factory=self.runtime.sessionmaker,
                fernet=fernet,
                github_fallback_pat=(
                    self.runtime.settings.github.fallback_pat.get_secret_value()
                    if self.runtime.settings.github.fallback_pat is not None
                    else None
                ),
                github_app_id=self.runtime.settings.github.app_id,
                github_app_private_key=(
                    self.runtime.settings.github.app_private_key.get_secret_value()
                    if self.runtime.settings.github.app_private_key is not None
                    else None
                ),
            )
            ma_session_id = ma_session.id

        usage_record = functools.partial(
            record_turn_usage,
            sessionmaker=self.runtime.sessionmaker,
            platform_user_id=str(message.author.id),
            managed_session_id=ma_session_id,
            model_id=agent.model.id,
            tenant_id=tenant_id,
            markup=self.runtime.settings.billing.markup,
            pricing=MODEL_PRICING.get(agent.model.id),
        )

        log.info(
            "session.ready",
            session_id=ma_session_id,
            thread_id=thread.id,
            reused=reused,
        )

        # Persist mapping for the create path (after thread.id is known).
        if not reused:
            async with self.runtime.sessionmaker() as _map_session:
                row = await create_thread_session(
                    _map_session,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=str(thread.id),
                    account_id=session_account_id,
                    ma_session_id=ma_session_id,
                )
                await _map_session.commit()
            mapping_id = row.id

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
            if reused and watermark is not None:
                user_message, _ = await build_delta_xml(
                    thread,
                    trigger=message,
                    after_message_id=int(watermark),
                    bot_user_id=self.user.id if self.user else None,
                )
            else:
                user_message, _ = await build_context_xml(
                    thread,
                    trigger=message,
                    limit=100,
                    bot_user_id=self.user.id if self.user else None,
                )
        else:
            if content_override is not None:
                user_message = content_override
            elif isinstance(message.channel, discord.TextChannel):
                user_message, _ = await build_channel_context_xml(
                    message.channel,
                    trigger=message,
                    bot_user_id=self.user.id if self.user else None,
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

        # --- Run the turn ---
        log.info(
            "turn.started",
            guild_id=guild_id,
            channel_id=parent_channel_id,
            thread_id=thread.id,
            session_id=ma_session_id,
        )
        state = await run_turn(
            anthropic=self.runtime.anthropic,
            session_id=ma_session_id,
            user_message=user_message,
            lifecycle=lifecycle,
            cancel=cancel,
            render_interval_s=2.0,
            usage_record=usage_record,
            image_blocks=image_blocks,
        )

        # --- Recreate on dead session (404 not_found_error) ---
        # Dead = session existed but is gone (expired/GC'd); 404 is the confirmed signal.
        # On dead session: mark old row dead, create new session + new mapping row,
        # re-seed with full history, re-run once. If the retry also fails, let it
        # fall through to the normal error render — no infinite loop.
        if _is_dead_session(state) and mapping_id is not None:
            log.info(
                "session.dead_recreate",
                old_session_id=ma_session_id,
                thread_id=thread.id,
            )
            async with self.runtime.sessionmaker() as _dead_session:
                await mark_dead(_dead_session, id=mapping_id)
                await _dead_session.commit()

            ma_session_recreated = await create_session(
                self.runtime.anthropic,
                agent=agent,
                environment=env,
                mcp_settings=self.runtime.settings.mcp,
                account_id=principal.account_id,
                tenant_id=tenant_id,
                agent_uuid=agent_uuid,
                session_factory=self.runtime.sessionmaker,
                fernet=fernet,
                github_fallback_pat=(
                    self.runtime.settings.github.fallback_pat.get_secret_value()
                    if self.runtime.settings.github.fallback_pat is not None
                    else None
                ),
                github_app_id=self.runtime.settings.github.app_id,
                github_app_private_key=(
                    self.runtime.settings.github.app_private_key.get_secret_value()
                    if self.runtime.settings.github.app_private_key is not None
                    else None
                ),
            )
            new_session_id = ma_session_recreated.id
            async with self.runtime.sessionmaker() as _new_map_session:
                new_row = await create_thread_session(
                    _new_map_session,
                    tenant_id=tenant_id,
                    platform="discord",
                    thread_id=str(thread.id),
                    # session_account_id is always non-NULL (real account_id or
                    # deterministic sentinel). A NULL insert here would cause a
                    # permanent cold-create loop on the next turn (T-88-04-02).
                    account_id=session_account_id,
                    ma_session_id=new_session_id,
                )
                await _new_map_session.commit()
            mapping_id = new_row.id

            # Full history re-seed for the recreated session.
            user_message, _ = await build_context_xml(
                thread,
                trigger=message,
                limit=100,
                bot_user_id=self.user.id if self.user else None,
            )
            if synthetic_prefix:
                user_message = synthetic_prefix + "\n" + user_message

            cancel_retry = asyncio.Event()
            lifecycle = DiscordTurnLifecycle(
                send=_send_embed,
                edit=_edit_message,
                agent_name=agent.name,
                model_id=agent.model.id,
                cancel_view=CancelView(allowed_user_id=message.author.id, cancel=cancel_retry),
            )
            state = await run_turn(
                anthropic=self.runtime.anthropic,
                session_id=new_session_id,
                user_message=user_message,
                lifecycle=lifecycle,
                cancel=cancel_retry,
                render_interval_s=2.0,
                usage_record=usage_record,
                image_blocks=image_blocks,
            )
            ma_session_id = new_session_id

        if state.error is not None:
            log.warning(
                "turn.error",
                thread_id=thread.id,
                session_id=ma_session_id,
                kind=state.error.kind,
            )
        else:
            log.info("turn.completed", thread_id=thread.id, session_id=ma_session_id)
            # --- Write watermark (bot's reply message id) ---
            if mapping_id is not None and lifecycle.final_message_id is not None:
                async with self.runtime.sessionmaker() as _wm_session:
                    await update_watermark(
                        _wm_session,
                        id=mapping_id,
                        watermark_message_id=lifecycle.final_message_id,
                    )
                    await _wm_session.commit()

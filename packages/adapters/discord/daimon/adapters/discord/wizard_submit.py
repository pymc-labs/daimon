"""`WizardSubmitButton` -- the isolated dispatch entry point for a wizard's
Submit tap, plus `run_wizard_submit_turn`, the resumed turn it spawns.

This is the only part of the wizard feature that spends the operator's money,
and the only part that can double-spend it. Every other tap in this feature
(`daimon.adapters.discord.wizard`) writes state and re-renders; a Submit tap
additionally claims the row exactly once and starts a billed turn, so it gets
its own `discord.ui.DynamicItem` template (`SUBMIT_CUSTOM_ID_TEMPLATE`) and
its own module rather than sharing `WizardNavButton`'s dispatch.

Three things are load-bearing about `callback`'s shape:

1. **The acknowledgement comes first, the turn second, and the turn is never
   awaited from inside `callback`.** discord.py wraps a `DynamicItem`
   callback in a bare catch-and-log (verified against the installed
   `discord/ui/view.py`, same as every other class in this adapter's short
   list of persistent dynamic items). A turn can run for minutes; awaiting
   it here would hold the interaction open until it finished and would let
   any failure inside it vanish silently behind a message that already says
   "submitted". `DaimonBot._spawn` gives the turn its own tracked
   background task with its own failure logging instead.

2. **The claim is one predicated UPDATE statement, not a read-then-write,
   and the spawn follows it immediately.** `try_claim_submit`'s own row lock
   is what makes two concurrent Submit taps produce exactly one winner --
   the loser's WHERE clause simply matches zero rows. No advisory lock, no
   read-your-own-write race window. The claim is also irreversible, which is
   why nothing that can fail is allowed between it and the spawn: the
   collapse re-render runs AFTER the hand-off and swallows its own
   `HTTPException`, so a transient Discord failure on a cosmetic edit can
   never consume a submission without running the turn it claimed.

3. **The collapsed screen carries no interactive components.** Once a row's
   status flips to submitted, `to_screen` returns the collapsed screen
   (`button_rows=()`), so a submitted form's message can never be tapped
   again -- there is nothing left on it to dispatch a tap to.

The two gates a wizard turn DOES share with the mention path: `bot.draining`
(checked in `callback`, before the one-shot claim, so a refused submit leaves
a form that is still tappable after the restart) and the per-tenant
`_inflight` cap (claimed and released in `run_wizard_submit_turn`, because a
wizard turn costs the same upstream capacity as a mention turn).

Accepted limitation, deliberately not addressed here: a submit-spawned turn
does not participate in the mention path's per-thread `_processing`/`_pending`
queueing (`bot.py`'s `_drain_pending_for_thread`). That queue only mention
turns feed and drain; half-registering a wizard turn into it would strand
mentions that arrive while the wizard turn runs, with nothing left to drain
them. The practical effect is that a wizard turn and a mention turn can run
concurrently against the same thread's session -- an accepted trade-off, not
a bug, for the first cut of this feature. It also means `_drain_and_close`
(which polls `_processing`) does not wait for an already-running wizard turn;
the drain gate above is what keeps new ones from starting.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Self, cast

import anthropic as _anthropic
import sentry_sdk
import structlog
from daimon.adapters.discord.bot import (
    DaimonBot,
    _credit_depleted_message,  # pyright: ignore[reportPrivateUsage]  # reused verbatim: the same balance-depleted copy the mention path shows
    _resolve_bot_display_name,  # pyright: ignore[reportPrivateUsage]  # reused verbatim: the same bot-display-name resolution the mention path uses
)
from daimon.adapters.discord.checks import is_member_guild_admin
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.thread_send import safe_thread_send
from daimon.adapters.discord.views import CancelView
from daimon.adapters.discord.wizard import (
    _authorize_tap,  # pyright: ignore[reportPrivateUsage]  # shared requester-only gate the sibling dispatch classes use -- reused verbatim so authorization logic exists in exactly one place
    _load_row,  # pyright: ignore[reportPrivateUsage]  # shared row loader the sibling dispatch classes use -- reused verbatim
    _reply_or_followup,  # pyright: ignore[reportPrivateUsage]  # shared is_done()-gated reply helper the sibling dispatch classes use -- reused verbatim
)
from daimon.adapters.discord.wizard_render import build_wizard_view
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role, WizardSessionRow
from daimon.core.stores.thread_sessions import update_watermark
from daimon.core.stores.wizard_session import try_claim_submit
from daimon.core.turn.admission import AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.ceiling import turn_deadline
from daimon.core.turn.gating import should_admit_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.wizard.answers import format_answer_block
from daimon.core.wizard.apply import apply
from daimon.core.wizard.render import to_screen
from daimon.core.wizard.spec import WizardSpec
from daimon.core.wizard.state import (
    SUBMIT_CUSTOM_ID_TEMPLATE,
    ActionKind,
    WizardAction,
    WizardState,
    WizardStatus,
    build_custom_id,
)
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord.ext import commands

_log = structlog.get_logger()

_CALLBACK_FAILED = "Something went wrong submitting this form -- please try again."
_CHECK_FAILED = "Something went wrong checking this form -- please try again."
_DRAINING = "I'm restarting right now -- submit this form again in a moment."
_OVER_CAP = (
    "Your answers were recorded, but this server has too many chats in flight "
    "right now -- ask again in the thread in a moment."
)


class WizardSubmitButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=SUBMIT_CUSTOM_ID_TEMPLATE,
):
    """Dispatches the Submit tap. Registered ONCE as a class (not per-button)
    via `client.add_dynamic_items` in `bot.py`'s `setup_hook`, alongside
    `WizardNavButton`/`WizardSelect`. Authorization and lifecycle rejection
    are the SAME requester-only gate `WizardNavButton`/`WizardSelect` use
    (imported from `wizard.py`, not re-implemented) -- see this module's and
    `wizard.py`'s docstrings for why `from_custom_id`/`interaction_check`/
    `callback` each catch their own exceptions."""

    def __init__(self, *, short_id: str, wizard_row: WizardSessionRow | None) -> None:
        button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Submit",
            custom_id=build_custom_id(short_id, "submit"),
        )
        super().__init__(button)
        self.short_id = short_id
        self.wizard_row = wizard_row

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        short_id = match["short_id"]
        bot = cast(DaimonBot, interaction.client)
        wizard_row = await _load_row(bot, short_id)
        return cls(short_id=short_id, wizard_row=wizard_row)

    async def interaction_check(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot], /
    ) -> bool:
        try:
            return await _authorize_tap(self.wizard_row, interaction)
        except Exception as err:  # noqa: BLE001 -- dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own dispatcher swallows anything raised here
            _log.exception("wizard_submit.interaction_check_failed", err_type=type(err).__name__)
            await interaction.response.send_message(_CHECK_FAILED, ephemeral=True)
            return False

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            row = self.wizard_row
            if row is None:
                return
            bot = cast(DaimonBot, interaction.client)

            # The acknowledgement is unconditionally the FIRST statement --
            # see the module docstring, point 1.
            await interaction.response.defer()

            spec = WizardSpec.model_validate(row.spec)
            state = WizardState(
                short_id=row.id,
                current_step=row.current_step,
                answers=row.answers,
                status=WizardStatus(row.status),
            )
            submitted = apply(state, spec, WizardAction(kind=ActionKind.SUBMIT))

            if submitted.status is not WizardStatus.SUBMITTED:
                # A stale tap that is not at the review screen -- `apply`
                # already refused it (returned `state` unchanged). Re-render
                # whatever screen the row is actually on; no claim, no turn.
                await interaction.edit_original_response(
                    view=build_wizard_view(to_screen(spec, submitted)),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if bot.draining:
                # Checked BEFORE the claim: the claim is one-shot, so
                # refusing here leaves the form intact and tappable once a
                # fresh process picks it up. The mention path rejects the
                # same way in `on_message`.
                await _reply_or_followup(interaction, _DRAINING)
                return

            now = datetime.now(UTC)
            async with bot.runtime.sessionmaker() as session, session.begin():
                claimed = await try_claim_submit(
                    session,
                    short_id=row.id,
                    answers=submitted.answers,
                    current_step=submitted.current_step,
                    now=now,
                )

            if claimed is not None:
                # The claim is the point of no return, so the turn it paid
                # for is handed off IMMEDIATELY after it -- nothing that can
                # fail may sit between the two. A concurrent tap that lost
                # the claim spawns nothing: a second turn here would be a
                # second billed turn.
                bot._spawn(  # pyright: ignore[reportPrivateUsage]  # DaimonBot's tracked fire-and-forget helper; see module docstring point 1 for why the turn must never be awaited here
                    run_wizard_submit_turn(
                        bot=bot, interaction=interaction, row=claimed, spec=spec, state=submitted
                    )
                )

            # Collapse to the read-only, button-free summary regardless of
            # who won the claim -- see the module docstring, point 2. Both a
            # winning and a losing concurrent tapper must see the SAME
            # consistent, submitted-looking result. Purely cosmetic, and
            # caught here rather than at the callback's outer boundary: the
            # row is already claimed, so the outer handler's "please try
            # again" would invite a retry that can only ever be rejected.
            try:
                await interaction.edit_original_response(
                    view=build_wizard_view(to_screen(spec, submitted)),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                _log.exception("wizard_submit.collapse_edit_failed", short_id=row.id)
        except Exception as err:  # noqa: BLE001 -- dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own dispatcher swallows anything raised here
            _log.exception("wizard_submit.callback_failed", err_type=type(err).__name__)
            await _reply_or_followup(interaction, _CALLBACK_FAILED)


async def _render_wizard_turn_error(
    interaction: discord.Interaction[commands.Bot],
    *,
    tenant_id: uuid.UUID,
    rid: str,
    exc: Exception,
) -> None:
    """Sentry-tag + post a rendered error for a turn failure caught in
    `run_wizard_submit_turn`. Mirrors `bot.py`'s `_render_turn_error`.

    Takes `interaction` (not a pre-narrowed channel) because the caller sits
    inside an `except` block, where a value assigned earlier in the `try` is
    "possibly unbound" from pyright's perspective (an exception could, in
    principle, have been raised before that assignment ran) -- re-deriving
    the channel from a function parameter, which is always bound, sidesteps
    that without a runtime-meaningless `assert`.
    """
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("rid", rid)
        scope.set_tag("tenant_id", str(tenant_id))
        scope.set_tag(
            "guild_id", str(interaction.guild_id) if interaction.guild_id is not None else ""
        )
        sentry_sdk.capture_exception(exc)
    error_text = render_error(exc, request_id=rid)
    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        return
    if isinstance(channel, discord.Thread):
        await safe_thread_send(channel, error_text)
    else:
        await channel.send(error_text)


async def run_wizard_submit_turn(
    *,
    bot: DaimonBot,
    interaction: discord.Interaction[commands.Bot],
    row: WizardSessionRow,
    spec: WizardSpec,
    state: WizardState,
) -> None:
    """The background task `WizardSubmitButton.callback` spawns after winning
    the claim. An ordinary caller of the shared admission/session-binding/run
    chokepoint (`daimon.core.turn.admission.admit`,
    `daimon.core.turn.prepare.bind_session`,
    `daimon.core.turn.run.run_prepared_turn`) -- never a private turn path --
    resuming the thread's existing session so the agent continues with the
    full history of having asked. Mirrors `bot.py`'s `_orchestrate` sequence
    rather than inventing a parallel one; carries its OWN two-tier error
    boundary (the same one `_handle_mention` uses) because nothing above a
    fire-and-forget background task will otherwise report a failure to the
    user.

    Claims a per-tenant in-flight slot the same way `on_message` does, and
    releases it in the same `finally` that unbinds the log context: a wizard
    turn costs the same upstream capacity as a mention turn, so it must count
    against the same cap.
    """
    rid = generate_request_id()
    structlog.contextvars.bind_contextvars(rid=rid)
    inflight_claimed = False
    try:
        # The form lives in the conversation the resumed turn should
        # continue -- no new thread is created here.
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            _log.warning("wizard_submit.no_messageable_channel", short_id=row.id)
            return

        if isinstance(channel, discord.Thread):
            parent_channel_id = str(channel.parent_id)
        else:
            parent_channel_id = str(channel.id)
        thread_id = str(channel.id)

        discord_settings = bot.runtime.settings.discord
        assert discord_settings is not None, (
            "run_wizard_submit_turn called without discord settings -- entrypoint must "
            "validate at boot"
        )

        # --- Per-tenant concurrency cap, claimed exactly as `on_message`
        # claims it: read-check-increment with no await in between, and one
        # matching release in this function's `finally`. The claim already
        # committed, so an over-cap submit says the answers were recorded
        # rather than pretending nothing happened. ---
        count = bot._inflight.get(row.tenant_id, 0)  # pyright: ignore[reportPrivateUsage]  # the per-tenant cap bookkeeping DaimonBot owns; a wizard turn must count against the same cap the mention path claims
        if not should_admit_turn(
            current_in_flight=count, cap=discord_settings.max_concurrent_turns_per_tenant
        ):
            _log.info("wizard_submit.skipped.over_cap", tenant_id=str(row.tenant_id))
            await channel.send(_OVER_CAP)
            return
        bot._inflight[row.tenant_id] = count + 1  # pyright: ignore[reportPrivateUsage]  # see the read above
        inflight_claimed = True

        # --- Stage one: admission -- D-01 admit(). Same three branches
        # _orchestrate has, prefixed with a sentence saying the answers were
        # already recorded (the claim committed before this runs). ---
        try:
            admission = await admit(
                bot.runtime.turn_deps,
                tenant_id=row.tenant_id,
                platform="discord",
                external_user_id=str(interaction.user.id),
                channel_id=parent_channel_id,
                now=datetime.now(UTC),
            )
        except MissingTurnConfigError as err:
            _log.info(
                "wizard_submit.missing_config",
                short_id=row.id,
                missing=list(err.missing),
            )
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
            await channel.send(
                "Your answers were recorded, but no "
                f"{' or '.join(err.missing)} configured for this channel -- ask again "
                "in the thread once it's set up. " + " ".join(hints)
            )
            return
        except MAResolverMissError as err:
            _log.warning(
                "wizard_submit.resolver_miss",
                kind=err.kind,
                daimon_tag=err.daimon_tag,
                tenant_id=str(err.tenant_id),
            )
            await channel.send(
                "Your answers were recorded, but the configured agent or environment "
                "no longer exists -- ask again in the thread. An admin can re-set the "
                "agent in `/agent-setup` -> **Set as default...**; the environment is "
                "operator-only via the CLI (`daimon config set environment_name=...`)."
            )
            return
        except AdmissionDenied as err:
            if err.reason == "balance_depleted":
                _log.info("wizard_submit.skipped.over_balance", tenant_id=str(row.tenant_id))
                await channel.send(
                    "Your answers were recorded, but "
                    + _credit_depleted_message(_resolve_bot_display_name(bot.runtime.settings))
                )
            else:
                _log.info("wizard_submit.skipped.over_cap", user_id=str(interaction.user.id))
                await channel.send(
                    "Your answers were recorded, but the monthly usage cap was reached "
                    "for this guild -- ask again in the thread. An admin can adjust the "
                    "cap with `/billing` (when available)."
                )
            return

        # D-03 boundary, mirroring bot.py's mention path: the per-turn
        # ceiling clock starts here, once admission has passed, and covers
        # session bind (bind_session) plus the driver pump (run_prepared_turn)
        # as ONE shared budget. This call site had NO timeout of any kind
        # before this phase -- the old 45-minute wait_for lived only in
        # bot.py's mention path, so a wizard-submit turn could hang forever.
        turn_deadline_at = turn_deadline(now=datetime.now(UTC))

        agent = admission.agent

        # --- Per-turn role upsert -- unconditional, same rationale as
        # _orchestrate: the live-role gate the resumed turn's MCP calls hit
        # must read a fresh value. ---
        author = interaction.user
        is_admin = isinstance(author, discord.Member) and is_member_guild_admin(
            author, guild_owner_id=interaction.guild.owner_id if interaction.guild else None
        )
        async with bot.runtime.sessionmaker() as role_session:
            await set_role(
                role_session, admission.account_id, Role.ADMIN if is_admin else Role.USER
            )
            await role_session.commit()

        if discord_settings.per_caller_thread_sessions:
            session_account_id = admission.account_id
        else:
            session_account_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"legacy-thread-sentinel:{row.tenant_id}:{channel.id}",
            )

        # --- Stage two: bind_session -- D-01 bind_session(). Always reuses
        # the thread's existing session: the form lives in the conversation
        # the turn should continue, so the agent resumes with the history of
        # having asked. ---
        prepared = await bind_session(
            bot.runtime.turn_deps,
            admission,
            tenant_id=row.tenant_id,
            platform="discord",
            external_user_id=str(interaction.user.id),
            thread_id=thread_id,
            session_account_id=session_account_id,
            reuse_existing=True,
            deadline=turn_deadline_at,
        )

        _log.info(
            "wizard_submit.session_ready",
            session_id=prepared.ma_session_id,
            thread_id=thread_id,
            reused=prepared.reused,
        )

        user_message = format_answer_block(spec, state)

        async def _send_embed(**kwargs: Any) -> discord.Message:
            return await channel.send(**kwargs)

        async def _edit_message(msg: discord.Message, **kwargs: Any) -> None:
            await msg.edit(**kwargs)

        cancel = asyncio.Event()
        cancel_view = CancelView(allowed_user_id=interaction.user.id, cancel=cancel)
        lifecycle = DiscordTurnLifecycle(
            send=_send_embed,
            edit=_edit_message,
            agent_name=agent.name,
            model_id=agent.model.id,
            cancel_view=cancel_view,
        )
        await lifecycle.post_initial()

        # lifecycle_holder tracks whichever DiscordTurnLifecycle actually
        # completed the turn -- recovery_lifecycle rebuilds a fresh one
        # against a recreated session, and the watermark write below must
        # read final_message_id off THAT lifecycle, not the pre-recovery one.
        lifecycle_holder: list[DiscordTurnLifecycle] = [lifecycle]

        async def _reseed_user_message() -> str:
            """The answer block IS the full message -- re-seeding a
            dead-session recovery cycle sends the same block again."""
            return format_answer_block(spec, state)

        def _recovery_lifecycle(cancel_event: asyncio.Event) -> TurnLifecycle:
            new_lifecycle = DiscordTurnLifecycle(
                send=_send_embed,
                edit=_edit_message,
                agent_name=agent.name,
                model_id=agent.model.id,
                cancel_view=CancelView(allowed_user_id=interaction.user.id, cancel=cancel_event),
                # Take over the failed attempt's message so its upstream-error
                # embed is edited into this turn's answer rather than left
                # standing next to a second, successful message.
                adopt_message_ref=lifecycle.message_ref,
            )
            lifecycle_holder[0] = new_lifecycle
            return new_lifecycle

        _log.info(
            "wizard_submit.turn_started",
            thread_id=thread_id,
            session_id=prepared.ma_session_id,
        )
        outcome = await run_prepared_turn(
            bot.runtime.turn_deps,
            prepared,
            tenant_id=row.tenant_id,
            platform="discord",
            thread_id=thread_id,
            external_user_id=str(interaction.user.id),
            user_message=user_message,
            lifecycle=lifecycle,
            cancel=cancel,
            reseed_user_message=_reseed_user_message,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=2.0,
            deadline=turn_deadline_at,
        )
        turn_state = outcome.state
        mapping_id = outcome.mapping_id
        final_lifecycle = lifecycle_holder[0]

        if turn_state.error is not None:
            _log.warning(
                "wizard_submit.turn_error",
                thread_id=thread_id,
                session_id=outcome.ma_session_id,
                kind=turn_state.error.kind,
            )
        else:
            _log.info(
                "wizard_submit.turn_completed",
                thread_id=thread_id,
                session_id=outcome.ma_session_id,
            )
            if mapping_id is not None and final_lifecycle.final_message_id is not None:
                async with bot.runtime.sessionmaker() as wm_session:
                    await update_watermark(
                        wm_session,
                        id=mapping_id,
                        watermark_message_id=final_lifecycle.final_message_id,
                    )
    except (DaimonError, _anthropic.APIError, discord.HTTPException, SQLAlchemyError) as exc:
        _log.warning("wizard_submit.turn_failed", error=str(exc), short_id=row.id)
        await _render_wizard_turn_error(interaction, tenant_id=row.tenant_id, rid=rid, exc=exc)
    except Exception as exc:  # noqa: BLE001 -- background-task adapter boundary (see module docstring); nothing above this task will otherwise report the failure
        _log.exception("wizard_submit.turn_failed_unexpected", error=str(exc), short_id=row.id)
        await _render_wizard_turn_error(interaction, tenant_id=row.tenant_id, rid=rid, exc=exc)
    finally:
        if inflight_claimed:
            bot._release_inflight(row.tenant_id)  # pyright: ignore[reportPrivateUsage]  # the matching release for the claim above
        structlog.contextvars.unbind_contextvars("rid")

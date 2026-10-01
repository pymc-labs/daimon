"""Persistent dispatch for wizard navigation, options, multi-select taps, and
typed answers -- `WizardNavButton`, `WizardSelect`, `WizardCustomTextModal`.

The process that posts a wizard's first screen (the MCP server) is never the
process that dispatches a tap on it (this bot) -- there is no live in-memory
`View` instance in this process to attach an ordinary non-persistent handler
to. `discord.ui.DynamicItem` is the primitive that matches a template regex
against an arbitrary future `custom_id` and reconstructs per-tap state via
`from_custom_id`, so `WizardNavButton` and `WizardSelect` are two more
entries in this adapter's short list of persistent dynamic items (see
`views.py`'s module docstring for the first, `CredentialRequestButton`).
`WizardCustomTextModal` is not itself a dynamic item -- it is opened
directly, mid-callback, exactly like `EnvCredentialModal`/`McpCredentialModal`
are opened from `CredentialRequestButton.callback`.

Authorization is requester-only, with no admin gate: `interaction_check`
compares `str(interaction.user.id)` against the row's stored
`requester_platform_user_id` and fails closed on any lookup miss, an
already-submitted row, or an abandoned/expired row. That comparison is the
ONLY authorization on this write path -- mirroring `CredentialRequestButton`,
the one existing precedent. It is joined by one binding check: the tap must
come from the message the row was posted as, so a stateless custom_id cannot
be replayed against a component on some other message, in some other channel
or guild.

`from_custom_id`, `interaction_check`, and `callback` (on both dynamic-item
classes) each catch broadly and log via structlog, because discord.py's own
dispatcher swallows any exception raised inside those three methods --
verified against the installed `discord/ui/view.py`. Propagating would mean
the exception vanishes into a dispatcher we don't control and the user is
left with a permanently stuck interaction and nothing in our own logs. That
is a deliberate, documented suspension of the ordinary let-failures-
propagate rule at exactly this boundary, not a general license to swallow
exceptions elsewhere in this module.

The Submit button is deliberately NOT dispatched here. It gets its own
dispatch class in a later plan, because the path it opens -- claiming the
submission and starting a billed turn -- needs an isolated entry point
rather than sharing one with ordinary navigation.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.wizard_render import build_wizard_view
from daimon.core.stores.domain import WizardSessionRow
from daimon.core.stores.wizard_session import get_wizard_session, update_wizard_state
from daimon.core.wizard.apply import apply, build_action
from daimon.core.wizard.render import to_screen
from daimon.core.wizard.spec import WizardSpec
from daimon.core.wizard.state import (
    NAV_CUSTOM_ID_TEMPLATE,
    SELECT_CUSTOM_ID_TEMPLATE,
    WizardState,
    WizardStatus,
    build_custom_id,
)
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord.ext import commands

_log = structlog.get_logger()

_NOT_AVAILABLE = "This form is no longer available."
_WRONG_REQUESTER = "This form was for someone else."
_ALREADY_SUBMITTED = "This form was already submitted."
_EXPIRED = "This form has expired."
_STALE = "This form changed before your tap landed."
_CHECK_FAILED = "Something went wrong checking this form -- please try again."
_CALLBACK_FAILED = "Something went wrong updating this form -- please try again."
_EMPTY_TEXT = "Your answer can't be empty -- try again."

_OPENER_ACTION_RE = re.compile(r"^s(?P<step>\d{1,2})_(?:enter|custom)$")


async def _load_row(bot: DaimonBot, short_id: str) -> WizardSessionRow | None:
    """One indexed primary-key read through `bot.runtime.sessionmaker()`.

    Runs before the 3s ack budget starts (called from `from_custom_id`).
    Never raises: discord.py logs and discards any exception raised from
    `from_custom_id`, so a raised DB error here would leave the user with a
    permanently stuck interaction and nothing in our own logs.
    """
    try:
        async with bot.runtime.sessionmaker() as session:
            return await get_wizard_session(session, short_id=short_id)
    except SQLAlchemyError:
        _log.exception("wizard.lookup_failed", short_id=short_id)
        return None


async def _authorize_tap(row: WizardSessionRow | None, interaction: discord.Interaction) -> bool:
    """Shared requester/lifecycle gate for `WizardNavButton` and `WizardSelect`.

    Sends the matching ephemeral rejection and returns False for: a missing
    row, a tap that did not come from the row's own message, a non-requester
    tap, an already-submitted row, or an abandoned or past-expiry row. This
    replaces what would otherwise be a silent no-op on a stale form. Returns
    True only for the requester tapping an open, unexpired row on the message
    the form was posted as.
    """
    if row is None:
        await interaction.response.send_message(_NOT_AVAILABLE, ephemeral=True)
        return False
    message = interaction.message
    if message is not None and str(message.id) != row.message_id:
        # A custom_id carries no state, so nothing else stops the requester
        # replaying their own form's id against a component on some other
        # message -- in another channel or another guild -- which would run
        # and bill the turn somewhere the form never was. The row records the
        # message it was posted as; fail closed when they disagree.
        _log.warning(
            "wizard.message_mismatch",
            short_id=row.id,
            expected_message_id=row.message_id,
            actual_message_id=str(message.id),
        )
        await interaction.response.send_message(_NOT_AVAILABLE, ephemeral=True)
        return False
    if str(interaction.user.id) != row.requester_platform_user_id:
        await interaction.response.send_message(_WRONG_REQUESTER, ephemeral=True)
        return False
    if row.status == WizardStatus.SUBMITTED:
        await interaction.response.send_message(_ALREADY_SUBMITTED, ephemeral=True)
        return False
    if row.status == WizardStatus.ABANDONED or row.expires_at < datetime.now(UTC):
        await interaction.response.send_message(_EXPIRED, ephemeral=True)
        return False
    return True


async def _reply_or_followup(interaction: discord.Interaction, message: str) -> None:
    """Send `message` ephemerally, via the response if it is still open or
    via the followup once it has already been used (deferred or replied)."""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def _apply_and_render(
    interaction: discord.Interaction,
    *,
    bot: DaimonBot,
    row: WizardSessionRow,
    action: str,
    values: list[str],
    text: str | None,
) -> None:
    """Rebuild state, run the pure transition, persist, and re-render.

    Must run AFTER the interaction has already been acknowledged (a deferred
    component tap, or a modal's already-deferred `on_submit`) -- this does a
    predicated UPDATE and is not bounded by the 3s ack budget. Raises
    `ValueError` when `build_action`/`apply` reject the action (e.g. empty
    typed text) -- callers that can produce that case catch it narrowly.

    `row` is both the state the transition is computed from and the
    optimistic-concurrency token the write is predicated on (`row.updated_at`),
    so a second tap that lands between this one's read and write loses
    instead of silently erasing the first's answer. Callers must therefore
    pass a freshly read row, never a snapshot held across a user-paced wait.
    """
    spec = WizardSpec.model_validate(row.spec)
    state = WizardState(
        short_id=row.id,
        current_step=row.current_step,
        answers=row.answers,
        status=WizardStatus(row.status),
    )
    action_obj = build_action(action, spec=spec, values=values, text=text)
    new_state = apply(state, spec, action_obj)
    now = datetime.now(UTC)
    async with bot.runtime.sessionmaker() as session, session.begin():
        rowcount = await update_wizard_state(
            session,
            short_id=row.id,
            answers=new_state.answers,
            current_step=new_state.current_step,
            expected_updated_at=row.updated_at,
            now=now,
        )
    if rowcount == 0:
        # The row changed under this tap (expired, erased, submitted, or
        # written by a tap that landed first) -- leave the message alone
        # rather than re-rendering a screen that no longer matches the row.
        await interaction.followup.send(_STALE, ephemeral=True)
        return
    # A re-render carries the agent-authored prompt and question verbatim in
    # its head text; nothing in a form legitimately needs to mention anyone.
    await interaction.edit_original_response(
        view=build_wizard_view(to_screen(spec, new_state)),
        allowed_mentions=discord.AllowedMentions.none(),
    )


class WizardNavButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=NAV_CUSTOM_ID_TEMPLATE,
):
    """Dispatches Back/Next/choice-option/opener taps. Registered ONCE as a
    class (not per-button) via `client.add_dynamic_items` in `bot.py`'s
    `setup_hook`. See the module docstring for the authorization model and
    why `from_custom_id`/`interaction_check`/`callback` catch their own
    exceptions."""

    def __init__(
        self,
        *,
        short_id: str,
        action: str,
        label: str,
        style: discord.ButtonStyle,
        wizard_row: WizardSessionRow | None,
        row: int | None = None,
    ) -> None:
        button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            style=style,
            label=label,
            custom_id=build_custom_id(short_id, action),
            row=row,
        )
        super().__init__(button, row=row)
        self.short_id = short_id
        self.action = action
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
        action = match["action"]
        bot = cast(DaimonBot, interaction.client)
        wizard_row = await _load_row(bot, short_id)
        label = item.label if isinstance(item, discord.ui.Button) and item.label else action
        style = item.style if isinstance(item, discord.ui.Button) else discord.ButtonStyle.secondary
        return cls(
            short_id=short_id, action=action, label=label, style=style, wizard_row=wizard_row
        )

    async def interaction_check(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot], /
    ) -> bool:
        try:
            return await _authorize_tap(self.wizard_row, interaction)
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own
            # dispatcher swallows anything raised here
            _log.exception("wizard.nav_interaction_check_failed", err_type=type(err).__name__)
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

            opener_match = _OPENER_ACTION_RE.match(self.action)
            if opener_match is not None:
                # A modal cannot follow a defer -- it must be the FIRST
                # response to this interaction.
                step_index = int(opener_match["step"])
                step = WizardSpec.model_validate(row.spec).steps[step_index]
                await interaction.response.send_modal(
                    WizardCustomTextModal(
                        bot=bot, row=row, step_index=step_index, question=step.question
                    )
                )
                return

            # This handler needs an indexed read, a pure transition, and an
            # indexed write before it could otherwise answer -- more work
            # inside the 3s ack budget than any other handler in this
            # adapter does, so the deferral is unconditionally the first
            # statement on every non-opener path.
            await interaction.response.defer()
            await _apply_and_render(
                interaction, bot=bot, row=row, action=self.action, values=[], text=None
            )
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own
            # dispatcher swallows anything raised here
            _log.exception("wizard.nav_callback_failed", err_type=type(err).__name__)
            await _reply_or_followup(interaction, _CALLBACK_FAILED)


class WizardSelect(
    discord.ui.DynamicItem[discord.ui.Select[discord.ui.LayoutView]],
    template=SELECT_CUSTOM_ID_TEMPLATE,
):
    """Dispatches a multi-select change. See `WizardNavButton`'s docstring
    for the shared authorization and exception-boundary rationale."""

    def __init__(
        self,
        *,
        short_id: str,
        action: str,
        placeholder: str,
        min_values: int,
        max_values: int,
        options: list[discord.SelectOption],
        wizard_row: WizardSessionRow | None,
        row: int | None = None,
    ) -> None:
        select: discord.ui.Select[discord.ui.LayoutView] = discord.ui.Select(
            custom_id=build_custom_id(short_id, action),
            placeholder=placeholder,
            min_values=min_values,
            max_values=max_values,
            options=options or [discord.SelectOption(label="unavailable", value="_")],
            row=row,
        )
        super().__init__(select, row=row)
        self.short_id = short_id
        self.action = action
        self.wizard_row = wizard_row

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # see WizardNavButton.from_custom_id
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        short_id = match["short_id"]
        action = match["action"]
        bot = cast(DaimonBot, interaction.client)
        wizard_row = await _load_row(bot, short_id)

        # Rebuilt from the row's spec/answers, not from the raw component on
        # the message, so the reconstructed widget's options and defaults
        # always reflect the state a tap will actually be evaluated against.
        placeholder = ""
        min_values, max_values = 0, 1
        options: list[discord.SelectOption] = []
        if wizard_row is not None:
            spec = WizardSpec.model_validate(wizard_row.spec)
            state = WizardState(
                short_id=short_id,
                current_step=wizard_row.current_step,
                answers=wizard_row.answers,
                status=WizardStatus(wizard_row.status),
            )
            screen_select = to_screen(spec, state).select
            if screen_select is not None:
                placeholder = screen_select.placeholder
                min_values = screen_select.min_values
                max_values = screen_select.max_values
                options = [
                    discord.SelectOption(
                        label=option.label,
                        value=option.value,
                        description=option.description,
                        default=option.default,
                    )
                    for option in screen_select.options
                ]
        return cls(
            short_id=short_id,
            action=action,
            placeholder=placeholder,
            min_values=min_values,
            max_values=max_values,
            options=options,
            wizard_row=wizard_row,
        )

    async def interaction_check(  # type: ignore[override]  # see WizardNavButton.interaction_check
        self, interaction: discord.Interaction[commands.Bot], /
    ) -> bool:
        try:
            return await _authorize_tap(self.wizard_row, interaction)
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own
            # dispatcher swallows anything raised here
            _log.exception("wizard.select_interaction_check_failed", err_type=type(err).__name__)
            await interaction.response.send_message(_CHECK_FAILED, ephemeral=True)
            return False

    async def callback(  # type: ignore[override]  # see WizardNavButton.callback
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            row = self.wizard_row
            if row is None:
                return
            bot = cast(DaimonBot, interaction.client)
            await interaction.response.defer()
            await _apply_and_render(
                interaction,
                bot=bot,
                row=row,
                action=self.action,
                values=list(self.item.values),
                text=None,
            )
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own
            # dispatcher swallows anything raised here
            _log.exception("wizard.select_callback_failed", err_type=type(err).__name__)
            await _reply_or_followup(interaction, _CALLBACK_FAILED)


class WizardCustomTextModal(discord.ui.Modal):
    """The 'type your own' opener's text form.

    Not itself a dynamic item -- opened directly from `WizardNavButton.callback`
    for the interaction that triggered it, exactly like
    `EnvCredentialModal`/`McpCredentialModal` are opened from
    `CredentialRequestButton.callback`. Its own `on_submit` runs the same
    rebuild / `build_action` / `apply` / `update_wizard_state` /
    `edit_original_response` sequence as the dynamic items above.

    The row captured at modal-open time is kept only for its short id: the
    window between opening this modal and submitting it is user-paced and
    unbounded, so `on_submit` re-reads the row rather than writing from a
    snapshot that every select and nav tap made in the meantime has already
    invalidated.
    """

    def __init__(
        self, *, bot: DaimonBot, row: WizardSessionRow, step_index: int, question: str
    ) -> None:
        super().__init__(title=question[:45])
        self._bot = bot
        self._row = row
        self._step_index = step_index
        self.answer_input: discord.ui.TextInput[WizardCustomTextModal] = discord.ui.TextInput(
            label="Your answer",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
        )
        self.add_item(self.answer_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            # For a component-originated modal submit, the deferred update
            # and `edit_original_response` target the wizard message itself.
            await interaction.response.defer()
            row = await _load_row(self._bot, self._row.id)
            if row is None:
                await interaction.followup.send(_NOT_AVAILABLE, ephemeral=True)
                return
            text = str(self.answer_input.value or "")
            action = f"s{self._step_index}_custom"
            try:
                await _apply_and_render(
                    interaction, bot=self._bot, row=row, action=action, values=[], text=text
                )
            except ValueError:
                # The pure transition already refuses empty/whitespace text
                # -- surface that as an ephemeral notice, not a state change.
                await interaction.followup.send(_EMPTY_TEXT, ephemeral=True)
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own
            # dispatcher swallows anything raised here
            _log.exception("wizard.modal_submit_failed", err_type=type(err).__name__)
            await _reply_or_followup(interaction, _CALLBACK_FAILED)

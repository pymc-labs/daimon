"""FeedbackButton -- persistent button that opens the private feedback form.

A reaction is not an interaction: Discord gives no way to open a modal
directly from a reaction-add event. This button exists solely to obtain an
interaction that legally can open one -- it is posted into a direct message
after a down-vote, and its only job is to hand the click straight to
`FeedbackModal`.

Like `CredentialRequestButton`, a per-row button cannot be served by a
single long-lived View instance registered with `bot.add_view()` -- this
button's custom_id is minted fresh per feedback row.
`discord.ui.DynamicItem` is the primitive that matches a *template* regex
against an arbitrary future custom_id and reconstructs per-click state via
`from_custom_id`, so this is the second (and, per `views.py`'s module
docstring, still documented) place in this adapter that carries a
persistent, dispatchable custom_id.

Unlike `CredentialRequestButton`, `from_custom_id` here performs NO database
read: the feedback row's own primary key is embedded in the custom_id and is
entirely self-sufficient to reconstruct the button, so there is no lookup
that could fail inside discord.py's exception-swallowing dispatcher.

discord.py's own dispatcher swallows every exception raised inside
`from_custom_id` and `callback` (logs internally and returns) -- the same
behavior `credential_button.py`'s module docstring documents against the
installed `discord/ui/view.py`. That makes those two methods a genuine
adapter boundary here too: the ordinary let-failures-propagate rule does not
apply, because propagating would mean the exception vanishes into a
dispatcher we don't control and the user sees nothing but a stuck
interaction. `callback` below catches broadly with a structlog record and an
ephemeral reply for exactly that reason.

Deliberately defines NO `interaction_check`: the button is delivered into a
one-to-one direct message, so Discord's own channel membership is the outer
access boundary, and the custom_id carries no user identity to compare a
clicker against -- there is nothing here to check against. The authoritative
gate lives in the write path: `FeedbackModal.on_submit` calls
`attach_feedback_text`, which predicates its update on the clicking user's
own `platform_user_id`, so a click carrying somebody else's row id writes
nothing. Adding a check here that compared a user against a value the
custom_id does not carry would be theatre; do not "restore" one without a
deliberate decision to put a user id in the custom_id.

Template-disjointness constraint: discord.py's dispatcher fires every
registered dynamic item whose pattern fullmatches an incoming custom_id,
with no early break, so this template's `mfb:` prefix must never overlap
another registered template's prefix (`ztc:` for the credential button, the
wizard's own prefixes).
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Self, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.feedback_modal import FeedbackModal
from daimon.core.message_feedback import CUSTOM_ID_TEMPLATE, build_custom_id

import discord
from discord.ext import commands

_log = structlog.get_logger()

_CALLBACK_FAILED = "Something went wrong opening the feedback form -- please try again."


class FeedbackButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Persistent, per-row feedback button reconstructed from its custom_id.

    Registered ONCE as a class (not per-button) via `client.add_dynamic_items`
    in `bot.py`'s `setup_hook`. See the module docstring for why there is no
    `interaction_check` and why `from_custom_id`/`callback` catch broadly.
    """

    def __init__(self, *, feedback_id: str) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Tell us what went wrong",
            custom_id=build_custom_id(feedback_id),
        )
        super().__init__(button)
        self.feedback_id = feedback_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        """Rebuild the item from its row id alone -- no database read.

        Unlike `CredentialRequestButton.from_custom_id`, there is no lookup
        here that could fail: the feedback row's primary key is the entire
        state this button needs, so reconstruction can never raise from a DB
        error inside discord.py's exception-swallowing dispatcher.
        """
        return cls(feedback_id=match["feedback_id"])

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        """Open the free-text form for this feedback row."""
        try:
            bot = cast(DaimonBot, interaction.client)
            await interaction.response.send_modal(
                FeedbackModal(runtime=bot.runtime, feedback_id=uuid.UUID(self.feedback_id))
            )
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own
            # dispatcher swallows anything raised here
            _log.exception("feedback_button.callback_failed", err_type=type(err).__name__)
            # The caught error may have left the interaction already responded
            # (`send_modal` raising `discord.InteractionResponded`, or failing
            # after the response type was set). An unconditional
            # `send_message` would then raise a SECOND time from inside this
            # handler and vanish into the same dispatcher, leaving the user
            # with neither the modal nor the apology. Same guard
            # `FeedbackModal.on_error` uses.
            if interaction.response.is_done():
                await interaction.followup.send(_CALLBACK_FAILED, ephemeral=True)
            else:
                await interaction.response.send_message(_CALLBACK_FAILED, ephemeral=True)

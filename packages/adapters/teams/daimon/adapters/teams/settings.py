"""The `settings` command: the shared Settings panel (`daimon.core.scope_settings`) as a
card with one button per choice, answered for the sender only.

A press re-verifies the clicker (`card_actor`) and is applied by the core writer. A card
in a channel post takes its place from the conversation it was pressed in; a card in
the 1:1 chat carries its post in the button, which is trusted for a thread only from
an admin.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Final

import structlog
from daimon.adapters.teams.card import notice_card
from daimon.adapters.teams.card_actions import button, card_actor, heading, submitted_fields, toast
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import DaimonError
from daimon.core.scope_settings import (
    SettingsActor,
    SettingsPanel,
    SettingsPlace,
    apply_choice,
    chat_actor,
    decode_choice,
    load_panel,
)
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import Action, ActionSet, AdaptiveCard, CardElement, TextBlock
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

VERB: Final = "settings"
IN_A_CHANNEL: Final = (
    "Settings work in a channel post. Send `settings` in the post you want to change."
)
CANNOT_VERIFY: Final = "I couldn't check who pressed this. Try again."
OPEN_IN_POST: Final = "Send `settings` in that channel post to change its thread."
FAILED: Final = "Something went wrong saving that setting. Try again later."
_THREAD_MARK: Final = ";messageid="


def settings_card(panel: SettingsPanel, place: SettingsPlace) -> AdaptiveCard:
    """The shared panel as a card; each choice is a button routed to `VERB`."""
    body: list[CardElement] = [heading(panel.title), TextBlock(text=panel.subline, wrap=True)]
    channel, thread = place.channel_id or "", place.thread_id or ""
    for row in panel.rows:
        body.append(TextBlock(text=row.name, weight="Bolder", spacing="Medium", wrap=True))
        body.append(TextBlock(text=row.summary, wrap=True))
        if row.choices:
            actions: list[Action] = [
                button(VERB, label, value, channel=channel, thread=thread)
                for value, label in row.choices
            ]
            body.append(ActionSet(actions=actions))
    return AdaptiveCard(body=body, fallback_text=panel.title)


async def _actor(
    runtime: TeamsRuntime,
    tenant_id: uuid.UUID,
    user_id: str,
    admin: Callable[[DelegatedAction | None], bool],
) -> SettingsActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return admin(action)

    return await chat_actor(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="teams",
        user_id=user_id,
        admin_for=admin_for,
    )


async def show_settings(context: CommandContext) -> None:
    runtime, asked = context.runtime, context.asked_in or context.inbound
    if asked.kind != "channel":
        await context.send(notice_card(IN_A_CHANNEL, error=False))
        return
    place = SettingsPlace.of("teams", asked.channel_id, asked.thread_id)
    try:
        actor = await _actor(runtime, context.tenant_id, asked.user_id, lambda _: context.is_admin)
        panel = await load_panel(runtime.sessionmaker, runtime.settings, actor, place)
    except (DaimonError, SQLAlchemyError) as exc:
        log.warning("teams.settings.failed", exc_info=exc)
        await context.send(notice_card(FAILED))
        return
    await context.send_card(settings_card(panel, place))


def pressed_place(
    activity: AdaptiveCardInvokeActivity, data: dict[str, object] | object
) -> tuple[SettingsPlace, bool] | None:
    """Where a press applies, and whether it came from the post itself (a channel card
    names its post through its conversation; a 1:1 card names it in the button)."""
    conversation = activity.conversation
    if conversation.conversation_type == "channel":
        thread = conversation.id if _THREAD_MARK in conversation.id else None
        return SettingsPlace.of("teams", conversation.id.split(";", 1)[0], thread), True
    fields = submitted_fields(data)
    channel, thread = fields.get("channel"), fields.get("thread")
    if not isinstance(channel, str) or not channel.startswith("19:") or not isinstance(thread, str):
        return None
    if thread and not thread.startswith(f"{channel}{_THREAD_MARK}"):
        return None
    return SettingsPlace.of("teams", channel, thread or None), False


class SettingsActions:
    """Applies a pressed choice for the verified clicker."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self.runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self.runtime, ctx.activity, remember_name=False)
        data = ctx.activity.value.action.data
        raw = submitted_fields(data).get("op")
        choice, found = decode_choice(raw), pressed_place(ctx.activity, data)
        if actor is None or actor.foreign or choice is None or found is None:
            return toast(CANNOT_VERIFY)
        place, in_post = found
        if not in_post and choice[1] == "thread" and not actor.admin_for(None):
            return toast(OPEN_IN_POST)
        try:
            done = await apply_choice(
                self.runtime.sessionmaker,
                self.runtime.settings,
                await _actor(self.runtime, actor.tenant_id, actor.user_id, actor.admin_for),
                raw,
                place,
            )
        except (DaimonError, SQLAlchemyError) as exc:
            log.warning("teams.settings.change_failed", exc_info=exc)
            return toast(FAILED)
        return toast(done[1] if done is not None else CANNOT_VERIFY)

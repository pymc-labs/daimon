"""The `settings` command: the shared panel (`daimon.core.settings_panel`) as a card with
one button per choice, answered for the sender only. A press replaces the card with the
next screen.

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
from daimon.adapters.teams.card_actions import (
    button,
    card_actor,
    error_text,
    heading,
    replace_card,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import DaimonError
from daimon.core.features.service import FeatureActor
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.surface import parse_choice
from daimon.core.settings_panel import PanelScreen, chat_actor, open_panel, press, tenant_agents
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


_BUTTONS_PER_ROW: Final = 6


def settings_card(screen: PanelScreen, place: FeaturePlace) -> AdaptiveCard:
    """A screen as a card; each choice is a button routed to `VERB`."""
    channel, thread = place.channel_id or "", place.thread_id or ""
    body: list[CardElement] = [heading(screen.title)]
    body += [TextBlock(text=line, wrap=True) for line in screen.lines]
    if screen.notice:
        body.append(
            TextBlock(text=screen.notice, wrap=True, color="Good")
            if screen.ok
            else error_text(screen.notice)
        )
    for control in screen.selects:
        body.append(
            TextBlock(text=control.placeholder, weight="Bolder", spacing="Medium", wrap=True)
        )
        buttons: list[Action] = [
            button(VERB, option.label, option.value, channel=channel, thread=thread)
            for option in control.options
        ]
        for start in range(0, len(buttons), _BUTTONS_PER_ROW):
            body.append(ActionSet(actions=buttons[start : start + _BUTTONS_PER_ROW]))
    if screen.back is not None:
        back = button(VERB, screen.back.label, screen.back.value, channel=channel, thread=thread)
        body.append(ActionSet(actions=[back]))
    return AdaptiveCard(body=body, fallback_text=screen.title)


async def _actor(
    runtime: TeamsRuntime,
    tenant_id: uuid.UUID,
    user_id: str,
    admin: Callable[[DelegatedAction | None], bool],
) -> FeatureActor:
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
    place = FeaturePlace.of(context.tenant_id, "teams", asked.channel_id, asked.thread_id)
    try:
        actor = await _actor(runtime, context.tenant_id, asked.user_id, lambda _: context.is_admin)
        screen = await open_panel(
            runtime.sessionmaker,
            runtime.settings,
            actor,
            place,
            agents=tenant_agents(runtime.anthropic, context.tenant_id),
        )
    except (DaimonError, SQLAlchemyError) as exc:
        log.warning("teams.settings.failed", exc_info=exc)
        await context.send(notice_card(FAILED))
        return
    await context.send_card(settings_card(screen, place))


def pressed_place(
    tenant_id: uuid.UUID, activity: AdaptiveCardInvokeActivity, data: dict[str, object] | object
) -> tuple[FeaturePlace, bool] | None:
    """Where a press applies, and whether it came from the post itself (a channel card
    names its post through its conversation; a 1:1 card names it in the button)."""
    conversation = activity.conversation
    if conversation.conversation_type == "channel":
        thread = conversation.id if _THREAD_MARK in conversation.id else None
        return FeaturePlace.of(tenant_id, "teams", conversation.id.split(";", 1)[0], thread), True
    fields = submitted_fields(data)
    channel, thread = fields.get("channel"), fields.get("thread")
    if not isinstance(channel, str) or not channel.startswith("19:") or not isinstance(thread, str):
        return None
    if thread and not thread.startswith(f"{channel}{_THREAD_MARK}"):
        return None
    return FeaturePlace.of(tenant_id, "teams", channel, thread or None), False


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
        if actor is None or actor.foreign:
            return toast(CANNOT_VERIFY)
        choice, found = parse_choice(raw), pressed_place(actor.tenant_id, ctx.activity, data)
        if found is None:
            return toast(CANNOT_VERIFY)
        place, in_post = found
        if not in_post and choice and choice.tier == "thread" and not actor.admin_for(None):
            return toast(OPEN_IN_POST)
        try:
            screen = await press(
                self.runtime.sessionmaker,
                self.runtime.settings,
                await _actor(self.runtime, actor.tenant_id, actor.user_id, actor.admin_for),
                raw,
                place,
                agents=tenant_agents(self.runtime.anthropic, actor.tenant_id),
            )
        except (DaimonError, SQLAlchemyError) as exc:
            log.warning("teams.settings.change_failed", exc_info=exc)
            return toast(FAILED)
        return (
            toast(CANNOT_VERIFY) if screen is None else replace_card(settings_card(screen, place))
        )

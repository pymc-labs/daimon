"""The `settings` command: this post's Settings group, as a card with buttons.

Typed in a channel post it shows that post's thread, its channel and the
organisation (`daimon.core.settings_card`), answered for the sender only. Each
button carries one encoded choice; a press re-verifies the clicker
(`card_actor`) and is applied by `daimon.core.scope_settings.change_setting`,
which re-checks who may make it. The place comes from the conversation the
card was pressed in; a card answered in the 1:1 chat carries its post in the
button, and there only an admin's change is taken.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from daimon.adapters.teams.card import notice_card
from daimon.adapters.teams.card_actions import (
    Actor,
    button,
    card_actor,
    heading,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import DaimonError, StaleChannelValue
from daimon.core.scope_settings import (
    SettingsActor,
    SettingsDefaults,
    SettingsPlace,
    change_setting,
    may_change_approvals,
    read_settings,
)
from daimon.core.settings_card import (
    SettingsPanel,
    build_settings_panel,
    decode_choice,
    may_change_for,
)
from daimon.core.stores.identity import find_platform_principal
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
                button(VERB, choice.label, choice.value, channel=channel, thread=thread)
                for choice in row.choices
            ]
            body.append(ActionSet(actions=actions))
    return AdaptiveCard(body=body, fallback_text=panel.title)


def _actor(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    user_id: str,
    admin_for: Actor | bool,
) -> SettingsActor:
    async def check(action: DelegatedAction | None) -> bool:
        return admin_for.admin_for(action) if isinstance(admin_for, Actor) else admin_for

    return SettingsActor(
        tenant_id=tenant_id,
        account_id=account_id,
        surface="panel",
        native_user_id=user_id,
        admin_for=check,
    )


async def _account(runtime: TeamsRuntime, tenant_id: uuid.UUID, user_id: str) -> uuid.UUID | None:
    async with runtime.sessionmaker() as session:
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="teams", external_id=user_id
        )
    return principal.account_id if principal is not None else None


async def show_settings(context: CommandContext) -> None:
    runtime, asked = context.runtime, context.asked_in or context.inbound
    if asked.kind != "channel":
        await context.send(notice_card(IN_A_CHANNEL, error=False))
        return
    thread_id = asked.thread_id if asked.thread_id != asked.channel_id else None
    place = SettingsPlace.of("teams", asked.channel_id, thread_id)
    try:
        account_id = await _account(runtime, context.tenant_id, asked.user_id)
        actor = _actor(context.tenant_id, account_id, asked.user_id, context.is_admin)
        native = await may_change_approvals(
            runtime.sessionmaker, runtime.settings, actor, place.platform
        )
        views = await read_settings(
            runtime.sessionmaker,
            tenant_id=context.tenant_id,
            place=place,
            defaults=SettingsDefaults.from_settings(runtime.settings, "teams"),
        )
    except (DaimonError, SQLAlchemyError) as exc:
        log.warning("teams.settings.failed", exc_info=exc)
        await context.send(notice_card(FAILED))
        return
    panel = build_settings_panel(
        views, place=place, may_change=may_change_for(admin=context.is_admin, native_admin=native)
    )
    await context.send_card(settings_card(panel, place))


def pressed_place(
    activity: AdaptiveCardInvokeActivity, data: dict[str, object] | object
) -> tuple[SettingsPlace, bool] | None:
    """Where a press applies, and whether it came from the post itself.

    A channel card names its post through the conversation it was pressed in;
    a 1:1 chat card names it in the button, which a member cannot be trusted with.
    """
    conversation = activity.conversation
    if conversation.conversation_type == "channel":
        conversation_id = conversation.id
        channel = conversation_id.split(";", 1)[0]
        thread = conversation_id if _THREAD_MARK in conversation_id else None
        return SettingsPlace.of("teams", channel, thread), True
    fields = submitted_fields(data)
    channel, thread = fields.get("channel"), fields.get("thread")
    if not isinstance(channel, str) or not channel.startswith("19:"):
        return None
    if not isinstance(thread, str):
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
        if actor is None or actor.foreign:
            return toast(CANNOT_VERIFY)
        data = ctx.activity.value.action.data
        choice = decode_choice(submitted_fields(data).get("op"))
        found = pressed_place(ctx.activity, data)
        if choice is None or found is None:
            return toast(CANNOT_VERIFY)
        key, level, value = choice
        place, in_post = found
        if not in_post and level == "thread" and not actor.admin_for(None):
            return toast(OPEN_IN_POST)
        try:
            account_id = await _account(self.runtime, actor.tenant_id, actor.user_id)
            change = await change_setting(
                self.runtime.sessionmaker,
                self.runtime.settings,
                _actor(actor.tenant_id, account_id, actor.user_id, actor),
                key=key,
                level=level,
                value=value,
                place=place,
            )
        except (ChannelChangeRefused, StaleChannelValue) as exc:
            return toast(str(exc))
        except (DaimonError, SQLAlchemyError) as exc:
            log.warning("teams.settings.change_failed", exc_info=exc)
            return toast(FAILED)
        return toast(change.note)


__all__ = ["VERB", "SettingsActions", "pressed_place", "settings_card", "show_settings"]

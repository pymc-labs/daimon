"""The `model` command: one conversation's model, as a card with a button per choice.

The buttons list the agent's catalog with prices; a pick applies at once and the reply names
anything off on the new provider. The shared flow is `daimon.core.thread_model_flow`; a press
re-verifies the clicker (`card_actor`) and takes the conversation from the activity, never
from the button.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from daimon.adapters.teams.card import notice_adaptive_card, notice_card
from daimon.adapters.teams.card_actions import (
    button,
    card_actor,
    heading,
    replace_card,
    submitted_fields,
    text_lines,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import DaimonError
from daimon.core.thread_model_flow import (
    NOTHING_CHANGED,
    ThreadOption,
    build_thread_pick,
    confirm_thread_pick,
    pick_thread_model,
    thread_options,
)
from daimon.core.thread_models import may_choose_thread_model
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import Action, ActionSet, AdaptiveCard, CardElement
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

VERB: Final = "thread_model"
TITLE: Final = "Model for this conversation"
PICK_PROMPT: Final = "Pick a model."
NOT_ALLOWED: Final = "You can't change the model here. Ask an admin or an owner."
CANNOT_VERIFY: Final = "I couldn't verify you just now. Try again."
IN_THE_CONVERSATION: Final = "Send `model` in the conversation you want to change."
FAILED: Final = "That didn't work. Try again."
_BUTTONS_PER_ROW: Final = 3
_THREAD_MARK: Final = ";messageid="
_PICK: Final = "pick:"


def options_card(options: tuple[ThreadOption, ...]) -> AdaptiveCard:
    """The choices, one button each."""
    buttons: list[Action] = [
        button(VERB, option.label, f"{_PICK}{option.value}") for option in options
    ]
    body: list[CardElement] = [heading(TITLE), *text_lines(PICK_PROMPT)]
    body += [
        ActionSet(actions=buttons[start : start + _BUTTONS_PER_ROW])
        for start in range(0, len(buttons), _BUTTONS_PER_ROW)
    ]
    return AdaptiveCard(body=body, fallback_text=TITLE)


def _done(text: str) -> AdaptiveCardInvokeResponse:
    return replace_card(notice_adaptive_card(text, error=False))


def where(conversation_id: str, conversation_type: str | None) -> tuple[str, str] | None:
    """The channel and the conversation a card was pressed in; None for a channel post
    that names no thread."""
    if conversation_type == "channel":
        if _THREAD_MARK not in conversation_id:
            return None
        return conversation_id.split(";", 1)[0], conversation_id
    return conversation_id, conversation_id


async def _pick_for(
    runtime: TeamsRuntime,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    admin: bool,
    channel_id: str,
    conversation_id: str,
):
    return await build_thread_pick(
        turn_deps=runtime.turn_deps,
        sessionmaker=runtime.sessionmaker,
        anthropic=runtime.anthropic,
        settings=runtime.settings,
        default=runtime.deployment_default,
        tenant_id=tenant_id,
        platform="teams",
        user_id=user_id,
        admin=admin,
        channel_id=channel_id,
        conversation_id=conversation_id,
    )


async def _may(
    runtime: TeamsRuntime,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    admin: bool,
    channel_id: str,
    conversation_id: str,
) -> bool:
    return await may_choose_thread_model(
        runtime.sessionmaker,
        runtime.turn_deps.membership_resolver,
        tenant_id=tenant_id,
        platform="teams",
        user_id=user_id,
        is_admin=admin,
        channel_id=channel_id,
        thread_id=conversation_id,
    )


async def show_model(context: CommandContext) -> None:
    """`model`: the choices for the conversation it was sent in, for the sender only."""
    runtime, inbound = context.runtime, context.inbound
    if context.asked_in is not None:
        await context.send(notice_card(IN_THE_CONVERSATION, error=False))
        return
    try:
        if not await _may(
            runtime,
            tenant_id=context.tenant_id,
            user_id=inbound.user_id,
            admin=context.is_admin,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
        ):
            await context.send(notice_card(NOT_ALLOWED, error=False))
            return
        pick = await _pick_for(
            runtime,
            tenant_id=context.tenant_id,
            user_id=inbound.user_id,
            admin=context.is_admin,
            channel_id=inbound.channel_id,
            conversation_id=inbound.conversation_id,
        )
        options = await thread_options(runtime.settings, pick)
    except (DaimonError, SQLAlchemyError) as exc:
        log.warning("teams.thread_model.failed", exc_info=exc)
        await context.send(notice_card(FAILED))
        return
    await context.send_card(options_card(options))


class ThreadModelActions:
    """Applies a pressed choice for the verified clicker; also Switch and Cancel on old cards."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self.runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self.runtime, ctx.activity, remember_name=False)
        if actor is None or actor.foreign:
            return toast(CANNOT_VERIFY)
        data = submitted_fields(ctx.activity.value.action.data)
        op = str(data.get("op") or "")
        if op == "cancel":
            return _done(NOTHING_CHANGED)
        found = where(actor.conversation_id, ctx.activity.conversation.conversation_type)
        if found is None:
            return toast(CANNOT_VERIFY)
        channel_id, conversation_id = found
        try:
            if not await _may(
                self.runtime,
                tenant_id=actor.tenant_id,
                user_id=actor.user_id,
                admin=actor.admin_for(DelegatedAction.MODEL),
                channel_id=channel_id,
                conversation_id=conversation_id,
            ):
                return _done(f"{NOT_ALLOWED} {NOTHING_CHANGED}")
            admin = actor.admin_for(DelegatedAction.AGENT_SETTINGS)
            pick = await _pick_for(
                self.runtime,
                tenant_id=actor.tenant_id,
                user_id=actor.user_id,
                admin=admin,
                channel_id=channel_id,
                conversation_id=conversation_id,
            )
            if op.startswith(_PICK):
                note = await pick_thread_model(
                    sessionmaker=self.runtime.sessionmaker,
                    settings=self.runtime.settings,
                    pick=pick,
                    tenant_id=actor.tenant_id,
                    platform="teams",
                    conversation_id=conversation_id,
                    choice=op.removeprefix(_PICK),
                    admin=admin,
                )
                return _done(note)
            if op == "confirm":
                value = _read_confirm(data)
                if value is None or pick is None:
                    return toast(CANNOT_VERIFY)
                note = await confirm_thread_pick(
                    pick, value, conversation_id=conversation_id, admin=admin
                )
                return _done(note)
        except (DaimonError, SQLAlchemyError) as exc:
            log.warning("teams.thread_model.press_failed", exc_info=exc)
            return toast(FAILED)
        return toast(CANNOT_VERIFY)


def _read_confirm(data: object) -> str | None:
    """The pick on a Switch from before picks applied at once."""
    value = submitted_fields(data).get("v")
    return value if isinstance(value, str) else None

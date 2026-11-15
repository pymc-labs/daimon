"""The `model` command: one conversation's model, as a card with a button per choice.

The buttons list the agent's catalog with prices; a pick that changes provider shows what
changes and asks once. The same card is posted in the conversation when an agent edit leaves
its consent stale (`daimon.core.thread_model_reask`). The shared flow is
`daimon.core.thread_model_flow`; a press re-verifies the clicker (`card_actor`) and takes the
conversation from the activity, never from the button.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Final, cast

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
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.errors import DaimonError
from daimon.core.thread_model_flow import (
    NOTHING_CHANGED,
    ThreadConfirm,
    ThreadOption,
    build_thread_pick,
    confirm_thread_pick,
    pick_thread_model,
    thread_options,
)
from daimon.core.thread_model_reask import ThreadReask, ThreadReaskNotifier
from daimon.core.thread_models import may_choose_thread_model
from daimon.core.turn.deps import TurnDeps
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import Action, ActionSet, AdaptiveCard, CardElement
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

VERB: Final = "thread_model"
TITLE: Final = "Model for this conversation"
PICK_PROMPT: Final = "Pick a model."
CONFIRM_PROMPT: Final = "Confirm to switch this conversation."
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


def confirm_card(lines: tuple[str, ...], confirm: ThreadConfirm | None) -> AdaptiveCard:
    """What a pick changes; with Confirm and Cancel when it can go ahead."""
    body: list[CardElement] = [heading(TITLE), *text_lines(*lines)]
    if confirm is not None:
        body += text_lines(CONFIRM_PROMPT)
        yes = button(
            VERB,
            "Confirm",
            "confirm",
            style="positive",
            v=confirm.value,
            d=confirm.digest,
            r=confirm.revision,
        )
        body.append(ActionSet(actions=[yes, button(VERB, "Cancel", "cancel")]))
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
    """Applies a pressed choice, Confirm or Cancel for the verified clicker."""

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
                result = await pick_thread_model(
                    sessionmaker=self.runtime.sessionmaker,
                    settings=self.runtime.settings,
                    pick=pick,
                    tenant_id=actor.tenant_id,
                    platform="teams",
                    conversation_id=conversation_id,
                    choice=op.removeprefix(_PICK),
                    admin=admin,
                )
                if result.confirm is not None:
                    return replace_card(confirm_card(result.lines, result.confirm))
                if result.lines:
                    return _done("\n\n".join((*result.lines, NOTHING_CHANGED)))
                return _done(result.text)
            if op == "confirm":
                confirm = _read_confirm(data)
                if confirm is None or pick is None:
                    return toast(CANNOT_VERIFY)
                note = await confirm_thread_pick(
                    pick, confirm, conversation_id=conversation_id, admin=admin
                )
                return _done(note)
        except (DaimonError, SQLAlchemyError) as exc:
            log.warning("teams.thread_model.press_failed", exc_info=exc)
            return toast(FAILED)
        return toast(CANNOT_VERIFY)


def _read_confirm(data: object) -> ThreadConfirm | None:
    fields = submitted_fields(data)
    value, digest, revision = fields.get("v"), fields.get("d"), fields.get("r")
    if not isinstance(value, str) or not isinstance(digest, str):
        return None
    try:
        return ThreadConfirm(value, digest, int(cast(int, revision)))
    except (TypeError, ValueError):
        return None


def teams_thread_reask_notifier(direct: DirectChats) -> ThreadReaskNotifier:
    """Post the card in the conversation; a failed post raises so a later message retries."""

    async def send(ask: ThreadReask) -> None:
        card = confirm_card(ask.lines, ask.confirm)
        try:
            await direct.post_card(ask.conversation_id, MessageActivityInput().add_card(card))
        except TEAMS_SEND_ERRORS as exc:
            log.info("teams.thread_model.reask_undelivered", err_type=type(exc).__name__)
            raise

    return send


def with_thread_reask_notifier(runtime: TeamsRuntime, direct: DirectChats | None) -> TeamsRuntime:
    """`runtime` whose turns post the card; unchanged without a way to post or for a stand-in."""
    real = cast(object, runtime)
    deps = cast(object, runtime.turn_deps)
    if direct is None or not isinstance(real, TeamsRuntime) or not isinstance(deps, TurnDeps):
        return runtime
    return replace(
        real, turn_deps=replace(deps, thread_reask_notifier=teams_thread_reask_notifier(direct))
    )

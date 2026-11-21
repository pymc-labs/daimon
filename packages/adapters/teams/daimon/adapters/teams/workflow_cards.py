"""Workflow cards on Teams: presses, the run form, `workflows` and the run card editor.

Every decision is `daimon.core.workflows.chat_cards`, as the person who pressed. A Teams
press answers on the pressed card itself: a form, the run card or the workflow's card
replaces it, and the run card follower edits it while the run goes.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from daimon.adapters.teams.answer_access import AnswerPlace
from daimon.adapters.teams.card import notice_adaptive_card
from daimon.adapters.teams.card_actions import (
    card_actor,
    replace_card,
    submitted_fields,
    toast,
    web_links,
)
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import SEND_TIMEOUT_S, TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.posted_controls import PostedCard
from daimon.core.posted_controls.teams_card import CONTROL_VERB, build_adaptive_card
from daimon.core.teams_bot_framework import SERVICE_URL
from daimon.core.web_links import TenantLinks, WebAt
from daimon.core.workflows import chat_cards, mentions
from daimon.core.workflows.chat_cards import ChatPlace, Presser, RunForm
from daimon.core.workflows.views import seq
from microsoft_teams.api import MessageActivityInput
from microsoft_teams.cards import AdaptiveCard

if TYPE_CHECKING:
    from daimon.adapters.teams.commands import CommandContext
    from daimon.adapters.teams.runtime import TeamsRuntime
    from daimon.core.workflows import views
    from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse

log = structlog.get_logger(__name__)

PREFIXES = ("wfc:", "wfl:", "wff:")
#: Fields a run form shows before the web app runs it instead.
FORM_LIMIT = 10
PICK_FIELD = "workflow"
THREAD = ";messageid="


def web_at(runtime: TeamsRuntime, *, foreign: bool = False) -> WebAt:
    """The web links a workflow card may offer in a conversation, where `web_awareness` is On
    at its channel; none to a sender from another organisation. Teams names a post
    ``<channel>;messageid=<id>``, which splits into its channel and thread."""

    async def at(
        tenant_id: uuid.UUID, channel_id: str | None, thread_id: str | None
    ) -> TenantLinks | None:
        if channel_id is not None and ";" in channel_id:
            channel_id, thread_id = channel_id.split(";", 1)[0], thread_id or channel_id
        elif channel_id and thread_id and ";" not in thread_id:  # a mention's root post id
            thread_id = f"{channel_id}{THREAD}{thread_id}"
        return await web_links(
            runtime,
            tenant_id,
            admin=False,
            foreign=foreign,
            channel_id=channel_id,
            thread_id=thread_id,
        )

    return at


def is_workflow_press(custom_id: str) -> bool:
    return custom_id.startswith(PREFIXES)


def _adaptive(card: PostedCard) -> AdaptiveCard:
    return AdaptiveCard.model_validate(build_adaptive_card(card))


def _execute(title: str, custom_id: str) -> dict[str, Any]:
    data = {"action": CONTROL_VERB, "custom_id": custom_id}
    return {"type": "Action.Execute", "title": title, "verb": CONTROL_VERB, "data": data}


def form_card(form: RunForm) -> AdaptiveCard:
    """The run form in place of the pressed card; Run submits what was typed."""
    body: list[dict[str, Any]] = [{"type": "TextBlock", "text": form.title, "weight": "Bolder"}]
    for field in form.fields:
        box: dict[str, Any] = {
            "type": "Input.Text",
            "id": field.key,
            "label": field.label,
            "isRequired": field.required,
            "isMultiline": field.kind == "text",
            "value": field.default,
        }
        if field.choices:
            box["placeholder"] = " or ".join(field.choices)
        if field.max_length:
            box["maxLength"] = field.max_length
        body.append(box)
    card = {"type": "AdaptiveCard", "version": "1.5", "body": body}
    return AdaptiveCard.model_validate(card | {"actions": [_execute("Run", form.submit_id)]})


def panel_card(listed: list[views.WorkflowSummary]) -> AdaptiveCard:
    """`workflows`: the headline and a picker of up to 25 workflows, then Open."""
    card = build_adaptive_card(chat_cards.notice(chat_cards.panel_headline(listed)))
    if rows := chat_cards.panel_rows(listed):
        choices = [{"title": f"{name}: {status}", "value": wid} for wid, name, status in rows]
        picker = {"type": "Input.ChoiceSet", "id": PICK_FIELD, "choices": choices}
        card["body"] = [*seq(card.get("body")), picker]
        card["actions"] = [_execute("Open", chat_cards.PICK_ID)]
    return AdaptiveCard.model_validate(card)


async def answer(
    runtime: TeamsRuntime, activity: AdaptiveCardInvokeActivity
) -> AdaptiveCardInvokeResponse:
    """One press on a workflow card, the run form or the panel, as the person who pressed."""
    fields = submitted_fields(activity.value.action.data)
    custom_id = str(fields.get("custom_id") or "")
    actor = await card_actor(runtime, activity)
    if actor is None or not activity.reply_to_id:
        return toast(chat_cards.UNAVAILABLE)
    presser = Presser(actor.tenant_id, "teams", actor.user_id, channel_id=activity.conversation.id)
    place = ChatPlace("teams", activity.conversation.id, activity.reply_to_id)
    links = web_at(runtime, foreign=actor.foreign)
    sm, settings = runtime.sessionmaker, runtime.settings
    try:
        if custom_id == chat_cards.PICK_ID:
            picked = str(fields.get(PICK_FIELD) or "")
            return replace_card(
                _adaptive(await chat_cards.pick(sm, settings, presser, picked, links))
            )
        if custom_id.startswith("wff:"):
            raw = {k: str(v) for k, v in fields.items() if k not in ("action", "custom_id")}
            submitted = await chat_cards.submit_form(
                sm,
                settings,
                presser=presser,
                form_id=custom_id,
                raw=raw,
                limit=FORM_LIMIT,
                client=runtime.anthropic,
            )
            if isinstance(submitted, str):
                return toast(submitted)
            env, form, inputs = submitted
            shown = await chat_cards.start_on_card(
                env, form.workflow_id, inputs, test=form.test, place=place, links=links
            )
            return replace_card(_adaptive(shown))
        # An invoke carries no card text: a confirmation's inputs cannot be read back here.
        pressed = await chat_cards.press(
            sm,
            settings,
            presser=presser,
            custom_id=custom_id,
            place=place,
            links=links,
            form_limit=FORM_LIMIT,
            client=runtime.anthropic,
        )
        if pressed.form is not None:
            return replace_card(form_card(pressed.form))
        if pressed.card is not None:
            return replace_card(_adaptive(pressed.card))
        if pressed.start is not None:
            env = await chat_cards.env_of(sm, settings, presser, client=runtime.anthropic)
            if env is None:
                return toast(chat_cards.UNAVAILABLE)
            workflow_id, test = pressed.start
            shown = await chat_cards.start_on_card(
                env, workflow_id, {}, test=test, place=place, links=links
            )
            return replace_card(_adaptive(shown))
        say = pressed.say or chat_cards.UNAVAILABLE
        return toast(f"{say} {pressed.link}" if pressed.link else say)
    except Exception:
        # Boundary: a failed press must answer the presser, not leave the click hanging.
        log.exception("workflow_card.press_failed", custom_id=custom_id)
        return toast(chat_cards.UNAVAILABLE)


async def command(runtime: TeamsRuntime, context: CommandContext) -> None:
    """`workflows`: the panel for the person who asked, or the refusal."""
    presser = Presser(context.tenant_id, "teams", context.inbound.user_id)
    listed = await chat_cards.list_for(runtime.sessionmaker, runtime.settings, presser)
    if isinstance(listed, str):
        await context.send_card(notice_adaptive_card(listed))
        return
    await context.send_card(panel_card(listed))


async def claim_mention(
    runtime: TeamsRuntime, parsed: TeamsInbound, tenant_id: uuid.UUID, sender: TeamsSender
) -> bool:
    """Start the workflow a tag of Daimon triggers in a channel; True when it did (no turn
    follows). The run's card is posted in the message's thread. Only a real @mention counts:
    a typed name (`by_name`) waits for its plain-text switch on the turn path."""
    if (
        parsed.kind != "channel"
        or parsed.unprompted
        or parsed.by_name
        or THREAD not in parsed.conversation_id
    ):
        return False
    root = parsed.conversation_id.partition(THREAD)[2]
    place = AnswerPlace(parsed.conversation_id, parsed.activity_id, is_channel=True)
    claimed = await mentions.claim_mention(
        runtime.sessionmaker,
        runtime.settings,
        mentions.Mention(
            tenant_id=tenant_id,
            platform="teams",
            author=parsed.user_id,
            message_id=parsed.activity_id,
            text=parsed.text.strip(),
            channel_id=parsed.channel_id,
            thread_id=root,
            files=tuple(f.name for f in parsed.files),
            link=place.link(parsed.entra_tenant_id),
            thread_link=AnswerPlace(place.conversation_id, root, True).link(parsed.entra_tenant_id),
        ),
        web_at(runtime, foreign=parsed.is_foreign),
        datetime.now(UTC),
    )
    if claimed is None:
        return False
    if claimed.card is not None:
        update = MessageActivityInput().add_card(_adaptive(claimed.card))
        with contextlib.suppress(*TEAMS_SEND_ERRORS):
            sent = await asyncio.wait_for(
                sender.send(parsed.conversation_id, update, service_url=parsed.service_url),
                SEND_TIMEOUT_S,
            )
            if claimed.run_id is not None and sent.id:
                shown = ChatPlace("teams", parsed.conversation_id, sent.id)
                await chat_cards.show_on_card(runtime.sessionmaker, claimed.run_id, shown)
    return True


def card_editor(sender: TeamsSender) -> chat_cards.CardEditor:
    """The run card follower's editor: the card's conversation, sent again with its id."""

    async def edit(_: uuid.UUID, place: ChatPlace, card: PostedCard) -> None:
        update = MessageActivityInput(id=place.message_id).add_card(_adaptive(card))
        await sender.send(place.channel_id, update, service_url=SERVICE_URL)

    return edit


__all__ = [
    "answer",
    "card_editor",
    "claim_mention",
    "command",
    "form_card",
    "is_workflow_press",
    "panel_card",
]

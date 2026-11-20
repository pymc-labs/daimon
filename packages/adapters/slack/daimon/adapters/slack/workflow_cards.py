"""Workflow cards on Slack: presses, the run form, `/workflows` and the run card editor.

Every decision is `daimon.core.workflows.chat_cards`, as the person who pressed; this module
only reads the press, shows the answer and posts or edits the cards. The `/workflows` panel
is ephemeral, so its cards are replaced through the press's response URL.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import structlog
from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.posted_controls import PostedCard
from daimon.core.posted_controls.slack_blocks import build_card_blocks, card_notification_text
from daimon.core.stores.tenants import get_tenant
from daimon.core.workflows import chat_cards
from daimon.core.workflows.chat_cards import ChatPlace, Presser, RunForm
from daimon.core.workflows.views import obj, seq
from slack_sdk.errors import SlackApiError
from slack_sdk.webhook.async_client import AsyncWebhookClient

if TYPE_CHECKING:
    from daimon.adapters.slack.runtime import SlackRuntime
    from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)

PREFIXES = ("wfc:", "wfl:")
FORM_CALLBACK_ID = "workflow_run_form"
#: Fields a run form shows before the web app runs it instead.
FORM_LIMIT = 10


def is_workflow_action(action_id: str) -> bool:
    return action_id.startswith(PREFIXES)


def card_text(blocks: object) -> str:
    """The text a posted card shows, read back from its blocks."""
    if items := seq(blocks):
        return "\n".join(t for t in map(card_text, items) if t)
    node = obj(blocks)
    parts = [v if k == "text" and isinstance(v, str) else card_text(v) for k, v in node.items()]
    return "\n".join(p for p in parts if p)


def _card_fields(card: PostedCard) -> dict[str, Any]:
    return {"text": card_notification_text(card), "blocks": build_card_blocks(card)}


def form_view(form: RunForm, channel: str, thread: str | None) -> dict[str, Any]:
    """The run form as a modal; the submit names the form and where to post the run card."""
    blocks: list[dict[str, Any]] = []
    for field in form.fields:
        element: dict[str, Any] = {
            "type": "plain_text_input",
            "action_id": "v",
            "multiline": field.kind == "text",
        }
        if field.default:
            element["initial_value"] = field.default
        if field.choices:
            element["placeholder"] = {
                "type": "plain_text",
                "text": " or ".join(field.choices)[:150],
            }
        if field.max_length:
            element["max_length"] = field.max_length
        blocks.append(
            {
                "type": "input",
                "block_id": field.key,
                "optional": not field.required,
                "label": {"type": "plain_text", "text": field.label},
                "element": element,
            }
        )
    meta = {"form": form.submit_id, "channel": channel, "thread": thread}
    return {
        "type": "modal",
        "callback_id": FORM_CALLBACK_ID,
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": form.title[:24]},
        "submit": {"type": "plain_text", "text": "Run"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def panel_fields(listed: list[Any]) -> dict[str, Any]:
    """`/workflows`: the headline and a picker of up to 25 workflows."""
    blocks = build_card_blocks(chat_cards.notice(chat_cards.panel_headline(listed)))
    rows = chat_cards.panel_rows(listed)
    if rows:
        options = [
            {
                "text": {"type": "plain_text", "text": name[:75]},
                "description": {"type": "plain_text", "text": status[:75]},
                "value": wid,
            }
            for wid, name, status in rows
        ]
        picker = {
            "type": "static_select",
            "action_id": chat_cards.PICK_ID,
            "placeholder": {"type": "plain_text", "text": "Pick a workflow"},
            "options": options,
        }
        blocks.append({"type": "actions", "elements": [picker]})
    return {"text": chat_cards.panel_headline(listed), "blocks": blocks}


class _Chat:
    """One workspace's client, as a press or the panel uses it."""

    def __init__(self, client: AsyncWebClient, channel: str, user: str, thread: str | None) -> None:
        self.client, self.channel, self.user, self.thread = client, channel, user, thread

    async def say(self, text: str, link: str | None = None) -> None:
        body = f"{text}\n{link}" if link else text
        try:
            await self.client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=self.channel, user=self.user, thread_ts=self.thread, **notice_fields(body)
            )
        except SlackApiError as err:
            log.warning("workflow_card.ephemeral_failed", error=str(err))

    async def post(self, card: PostedCard) -> ChatPlace:
        sent = await self.client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=self.channel, thread_ts=self.thread, **_card_fields(card)
        )
        return ChatPlace("slack", self.channel, str(sent.get("ts") or ""), self.thread)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

    async def edit(self, place: ChatPlace, card: PostedCard) -> None:
        await self.client.chat_update(  # pyright: ignore[reportUnknownMemberType]
            channel=place.channel_id, ts=place.message_id, **_card_fields(card)
        )


async def handle_workflow_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """One press on a workflow card or the panel's picker, as the person who pressed."""
    team = str(obj(payload.get("team")).get("id") or "")
    user = str(obj(payload.get("user")).get("id") or "")
    channel = str(obj(payload.get("channel")).get("id") or "")
    container: dict[str, Any] = payload.get("container") or {}
    message_ts = str(container.get("message_ts") or "")
    thread = str(container.get("thread_ts") or "") or None
    action = obj((seq(payload.get("actions")) or [{}])[0])
    action_id = str(action.get("action_id") or "")
    client = await resolve_web_client(runtime, team_id=team) if team and user else None
    if client is None or not channel:
        return
    chat = _Chat(client, channel, user, thread)
    presser = Presser(derive_tenant_uuid(platform="slack", workspace_id=team), "slack", user)
    response_url = str(payload.get("response_url") or "")

    async def show(card: PostedCard) -> None:
        if container.get("is_ephemeral") and response_url:
            await AsyncWebhookClient(response_url).send_dict(
                {"replace_original": True, **_card_fields(card)}
            )
        else:
            await chat.edit(ChatPlace("slack", channel, message_ts), card)

    sm, settings = runtime.sessionmaker, runtime.settings
    try:
        if action_id == chat_cards.PICK_ID:
            picked = str(obj(action.get("selected_option")).get("value") or "")
            await show(await chat_cards.pick(sm, settings, presser, picked))
            return
        pressed = await chat_cards.press(
            sm,
            settings,
            presser=presser,
            custom_id=action_id,
            place=ChatPlace("slack", channel, message_ts, thread),
            card_text=card_text(obj(payload.get("message")).get("blocks")),
            form_limit=FORM_LIMIT,
        )
        if pressed.form is not None:
            await client.views_open(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=str(payload.get("trigger_id") or ""),
                view=form_view(pressed.form, channel, thread),
            )
        elif pressed.card is not None:
            await show(pressed.card)
        elif pressed.start is not None:
            workflow_id, test = pressed.start
            refusal = await chat_cards.open_pressed(
                sm, settings, presser, workflow_id, test=test, post=chat.post, edit=chat.edit
            )
            if refusal:
                await chat.say(refusal)
        else:
            await chat.say(pressed.say or chat_cards.UNAVAILABLE, pressed.link)
    except Exception:
        # Boundary: a failed press must answer the presser, not leave the click hanging.
        log.exception("workflow_card.press_failed", action_id=action_id)
        await chat.say(chat_cards.UNAVAILABLE)


def submit_job(
    runtime: SlackRuntime, payload: dict[str, Any]
) -> Callable[[], Coroutine[Any, Any, None]]:
    """The run form's submit, after the ack closed the modal: post a run card and start it."""

    async def job() -> None:
        team = str(obj(payload.get("team")).get("id") or "")
        user = str(obj(payload.get("user")).get("id") or "")
        view: dict[str, Any] = payload.get("view") or {}
        meta: dict[str, Any] = json.loads(str(view.get("private_metadata") or "{}"))
        values = obj(obj(view.get("state")).get("values"))
        raw = {
            key: str(obj(obj(block).get("v")).get("value") or "") for key, block in values.items()
        }
        client = await resolve_web_client(runtime, team_id=team)
        if client is None or not meta.get("channel"):
            return
        chat = _Chat(client, str(meta["channel"]), user, meta.get("thread"))
        presser = Presser(derive_tenant_uuid(platform="slack", workspace_id=team), "slack", user)
        submitted = await chat_cards.submit_form(
            runtime.sessionmaker,
            runtime.settings,
            presser=presser,
            form_id=str(meta.get("form") or ""),
            raw=raw,
            limit=FORM_LIMIT,
        )
        if isinstance(submitted, str):
            await chat.say(submitted)
            return
        env, form, inputs = submitted
        await chat_cards.open_run(
            env,
            form.workflow_id,
            inputs,
            test=form.test,
            name=form.title,
            post=chat.post,
            edit=chat.edit,
        )

    return job


async def handle_workflows_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """`/workflows`: an ephemeral panel to pick a workflow, or the refusal."""
    team, user = str(payload.get("team_id") or ""), str(payload.get("user_id") or "")
    channel = str(payload.get("channel_id") or "")
    client = await resolve_web_client(runtime, team_id=team)
    if client is None:
        return
    chat = _Chat(client, channel, user, None)
    presser = Presser(derive_tenant_uuid(platform="slack", workspace_id=team), "slack", user)
    listed = await chat_cards.list_for(runtime.sessionmaker, runtime.settings, presser)
    if isinstance(listed, str):
        await chat.say(listed)
        return
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel, user=user, **panel_fields(listed)
    )


def card_editor(runtime: SlackRuntime) -> chat_cards.CardEditor:
    """The run card follower's editor: the run's workspace client, then `chat.update`."""

    async def edit(tenant_id: uuid.UUID, place: ChatPlace, card: PostedCard) -> None:
        async with runtime.sessionmaker() as session:
            tenant = await get_tenant(session, tenant_id)
        client = (
            None
            if tenant is None
            else await resolve_web_client(runtime, team_id=tenant.external_id)
        )
        if client is not None:
            await _Chat(client, place.channel_id, "", None).edit(place, card)

    return edit


__all__ = [
    "FORM_CALLBACK_ID",
    "card_editor",
    "card_text",
    "handle_workflow_click",
    "handle_workflows_command",
    "is_workflow_action",
    "submit_job",
]

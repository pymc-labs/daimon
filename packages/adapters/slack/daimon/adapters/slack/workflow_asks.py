"""A workflow ask's buttons, its answer form and typed answers on Slack.

Buttons carry `wfa:` action ids (`daimon.core.workflows.answers`); Answer and a reject open
a modal (`ASK_FORM`). `claim_event` is what the message handlers run before admitting a
turn: a message in a thread under an ask is checked as its answer. Slack's channel access
is the outer boundary; core checks the person against the ask.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Final, cast

import httpx
import structlog
from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.slack_files import fetch_slack_file
from daimon.core.workflows import answers
from daimon.core.workflows.answers import Attachment, Claimed
from daimon.core.workflows.asks import Box
from pydantic import JsonValue

__all__ = ["ASK_FORM", "ask_blocks", "claim_event", "handle_ask_action", "submit_ask_form"]

ASK_FORM: Final = "wfa_form"
#: Slack's `<@U123>`, `<#C123|name>` and `<!here>` tokens: a reply that mentions the bot or a
#: person still starts with its answer word.
_MENTION: Final = re.compile(r"<[@#!][^>]*>")
log = structlog.get_logger(__name__)


def _part(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return cast("dict[str, Any]", payload.get(key) or {})


def _field(payload: dict[str, Any], outer: str, inner: str) -> str:
    return str(_part(payload, outer).get(inner) or "")


def ask_blocks(text: str, ask: dict[str, Any]) -> list[dict[str, Any]]:
    elements = [
        {
            "type": "button",
            "action_id": b.custom_id,
            "text": {"type": "plain_text", "text": b.label[:75]},
            **({"style": "primary"} if b.primary else {}),
        }
        for b in answers.buttons(ask)
    ]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
        {"type": "actions", "elements": elements[:25]},
    ]


def _notice(claimed: Claimed) -> dict[str, Any] | None:
    if claimed.text is None:
        return None
    return notice_fields(claimed.text, neutral=claimed.status != "refused")


def _files(runtime: SlackRuntime, event: dict[str, Any], team_id: str) -> answers.Ingest | None:
    """The message's files as Files API refs, read with the bot's token when it answers."""
    listed = cast("list[dict[str, Any]]", event.get("files") or [])
    ids = [str(f["id"]) for f in listed if f.get("id")][: answers.MAX_FILES]
    if not ids:
        return None

    async def ingest() -> list[dict[str, JsonValue]]:
        client = await resolve_web_client(runtime, team_id=team_id)
        token = None if client is None else client.token
        if not token:
            return []
        read: list[Attachment] = []
        for file_id in ids:
            try:
                content, mime, name = await fetch_slack_file(
                    runtime.http_client,
                    bot_token=token,
                    file_id=file_id,
                    max_bytes=answers.MAX_FILE_BYTES,
                )
            except httpx.HTTPError as err:
                # The class only: Slack's error text can carry the signed download URL.
                log.warning("workflow_ask.file_read_failed", err_type=type(err).__name__)
                continue
            read.append(Attachment(name, mime, content))
        return await answers.ingest_files(runtime.anthropic, read)

    return ingest


def message_text(event: dict[str, Any]) -> str:
    """The message's words without its tags."""
    return " ".join(_MENTION.sub(" ", str(event.get("text") or "")).split())


async def claim_event(runtime: SlackRuntime, event: dict[str, Any], *, team_id: str) -> bool:
    """Answer an ask with a message in a thread under it. True when no turn should follow."""
    thread_ts, channel = event.get("thread_ts"), str(event.get("channel") or "")
    if not thread_ts or not channel or event.get("bot_id") or not team_id:
        return False
    claimed = await answers.claim_reply(
        runtime.sessionmaker,
        runtime.settings,
        answers.Reply(
            platform="slack",
            tenant_ids=(derive_tenant_uuid(platform="slack", workspace_id=team_id),),
            author=str(event.get("user") or ""),
            message_id=str(event.get("ts") or ""),
            text=message_text(event),
            thread_id=f"{channel}:{thread_ts}",
            files=_files(runtime, event, team_id),
        ),
    )
    if claimed is None:
        return False
    fields = _notice(claimed)
    if fields is not None and (client := await resolve_web_client(runtime, team_id=team_id)):
        await client.chat_postMessage(channel=channel, thread_ts=str(thread_ts), **fields)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    return True


async def _tell(runtime: SlackRuntime, payload: dict[str, Any], claimed: Claimed) -> None:
    team_id = _field(payload, "team", "id")
    user = _field(payload, "user", "id")
    channel = _field(payload, "channel", "id")
    fields = _notice(claimed) or notice_fields("Your answer is in.", neutral=True)
    if channel and (client := await resolve_web_client(runtime, team_id=team_id)):
        await client.chat_postEphemeral(channel=channel, user=user, **fields)  # pyright: ignore[reportUnknownMemberType]


def _form_view(form: answers.AskForm, meta: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": ASK_FORM,
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": form.title[:24] or "Answer"},
        "submit": {"type": "plain_text", "text": "Send"},
        "blocks": [
            {
                "type": "input",
                "block_id": box.name,
                "optional": not box.required,
                "label": {"type": "plain_text", "text": box.title[:2000] or box.name},
                "element": _element(box),
            }
            for box in form.fields
        ],
    }


def _typed(box: dict[str, Any]) -> str:
    """A modal input's value: the text typed, or the option picked."""
    return _field(box, "value", "value") or str(
        _part(_part(box, "value"), "selected_option").get("value") or ""
    )


def _element(box: Box) -> dict[str, Any]:
    if not box.choices:
        return {"type": "plain_text_input", "action_id": "value", "multiline": True}
    return {
        "type": "static_select",
        "action_id": "value",
        "options": [
            {"text": {"type": "plain_text", "text": c[:75]}, "value": c[:150]}
            for c in box.choices[:100]
        ],
    }


def _pressed(team_id: str, channel: str, message_id: str, wait_id: uuid.UUID) -> answers.Pressed:
    """A Slack `ts` is unique only within its channel, so the press names both."""
    return answers.Pressed(
        platform="slack",
        tenant_ids=(derive_tenant_uuid(platform="slack", workspace_id=team_id),),
        channel_id=channel,
        message_id=message_id,
        wait_id=wait_id,
    )


async def handle_ask_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """A press of an ask's button: its form opens, or it answers at once."""
    action = cast("list[dict[str, Any]]", payload.get("actions") or [{}])[0]
    parsed = answers.parse_action(str(action.get("action_id") or ""))
    message_id = _field(payload, "container", "message_ts")
    team_id, channel = _field(payload, "team", "id"), _field(payload, "channel", "id")
    if parsed is None or not message_id or not team_id:
        return
    wait_id, route = parsed
    at = _pressed(team_id, channel, message_id, wait_id)
    form = await answers.ask_form(runtime.sessionmaker, at=at, route=route)
    if form is not None and form.fields:
        meta = {
            "message_id": message_id,
            "route": route,
            "team_id": team_id,
            "channel": channel,
            "wait_id": str(wait_id),
        }
        if client := await resolve_web_client(runtime, team_id=team_id):
            await client.views_open(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=str(payload.get("trigger_id") or ""), view=_form_view(form, meta)
            )
        return
    claimed = await answers.press(
        runtime.sessionmaker,
        runtime.settings,
        at=at,
        author=_field(payload, "user", "id"),
        route=route,
    )
    await _tell(runtime, payload, claimed)


async def submit_ask_form(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """A sent answer form, after the modal closed."""
    view = cast("dict[str, Any]", payload.get("view") or {})
    meta = cast("dict[str, str]", json.loads(str(view.get("private_metadata") or "{}")))
    state = cast("dict[str, dict[str, Any]]", _part(view, "state").get("values") or {})
    values = {name: _typed(box) for name, box in state.items()}
    route = meta.get("route", "")
    sent, comment = answers.form_answer(route, values)
    try:
        wait_id = uuid.UUID(meta.get("wait_id", ""))
    except ValueError:
        return  # a form opened before presses named their wait
    at = _pressed(
        meta.get("team_id", ""), meta.get("channel", ""), meta.get("message_id", ""), wait_id
    )
    claimed = await answers.press(
        runtime.sessionmaker,
        runtime.settings,
        at=at,
        author=_field(payload, "user", "id"),
        route=route,
        values=dict(sent),
        comment=comment,
    )
    payload = payload | {
        "team": {"id": meta.get("team_id")},
        "channel": {"id": meta.get("channel")},
    }
    await _tell(runtime, payload, claimed)

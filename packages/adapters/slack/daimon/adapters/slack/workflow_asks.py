"""A workflow ask's buttons, its answer form and typed answers on Slack.

Buttons carry `wfa:` action ids (`daimon.core.workflows.answers`); Answer and a reject open
a modal (`ASK_FORM`). `claim_event` is what the message handlers run before admitting a
turn: a message in a thread under an ask is checked as its answer. Slack's channel access
is the outer boundary; core checks the person against the ask.
"""

from __future__ import annotations

import json
from typing import Any, Final, cast

from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.thread_naming import strip_mentions
from daimon.core.workflows import answers
from daimon.core.workflows.answers import Claimed

__all__ = ["ASK_FORM", "ask_blocks", "claim_event", "handle_ask_action", "submit_ask_form"]

ASK_FORM: Final = "wfa_form"


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
            text=strip_mentions(str(event.get("text") or "")),
            thread_id=f"{channel}:{thread_ts}",
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
                "block_id": name,
                "label": {"type": "plain_text", "text": title[:2000] or name},
                "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
            }
            for name, title in form.fields
        ],
    }


async def handle_ask_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """A press of an ask's button: its form opens, or it answers at once."""
    action = cast("list[dict[str, Any]]", payload.get("actions") or [{}])[0]
    parsed = answers.parse_action(str(action.get("action_id") or ""))
    message_id = _field(payload, "container", "message_ts")
    if parsed is None or not message_id:
        return
    route = parsed[1]
    form = await answers.ask_form(
        runtime.sessionmaker, platform="slack", message_id=message_id, route=route
    )
    if form is not None and form.fields:
        team_id = _field(payload, "team", "id")
        channel = _field(payload, "channel", "id")
        meta = {"message_id": message_id, "route": route, "team_id": team_id, "channel": channel}
        if client := await resolve_web_client(runtime, team_id=team_id):
            await client.views_open(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=str(payload.get("trigger_id") or ""), view=_form_view(form, meta)
            )
        return
    claimed = await answers.press(
        runtime.sessionmaker,
        runtime.settings,
        platform="slack",
        author=_field(payload, "user", "id"),
        message_id=message_id,
        route=route,
    )
    await _tell(runtime, payload, claimed)


async def submit_ask_form(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """A sent answer form, after the modal closed."""
    view = cast("dict[str, Any]", payload.get("view") or {})
    meta = cast("dict[str, str]", json.loads(str(view.get("private_metadata") or "{}")))
    state = cast("dict[str, dict[str, Any]]", _part(view, "state").get("values") or {})
    values = {name: _field(box, "value", "value") for name, box in state.items()}
    route = meta.get("route", "")
    sent, comment = answers.form_answer(route, values)
    claimed = await answers.press(
        runtime.sessionmaker,
        runtime.settings,
        platform="slack",
        author=_field(payload, "user", "id"),
        message_id=meta.get("message_id", ""),
        route=route,
        values=dict(sent),
        comment=comment,
    )
    payload = payload | {
        "team": {"id": meta.get("team_id")},
        "channel": {"id": meta.get("channel")},
    }
    await _tell(runtime, payload, claimed)

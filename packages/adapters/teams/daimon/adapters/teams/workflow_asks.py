"""A workflow ask's card, its presses and typed answers on Teams.

The card carries its answer boxes inline (a reject's reason, the answer, a form's fields)
and `wfa` Action.Execute buttons (`daimon.core.workflows.answers`). `claim_inbound` is
what the message dispatch runs before admitting a turn: a reply in a channel thread an
ask opened is checked as its answer. Teams' channel access is the outer boundary; core
checks the person against the ask.
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import uuid
from typing import TYPE_CHECKING, Any, Final

import httpx
import structlog
from daimon.adapters.teams.attachments import FetchRefused, fetch_bytes
from daimon.adapters.teams.card import notice_card
from daimon.adapters.teams.card_actions import card_actor, submitted_fields, toast
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import SEND_TIMEOUT_S, TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.media.vision import sniff_image_media_type
from daimon.core.teams_graph import is_graph_url, is_sharepoint_host
from daimon.core.workflows import answers, waits
from daimon.core.workflows.answers import Attachment
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    CardElement,
    Choice,
    ChoiceSetInput,
    ExecuteAction,
    TextBlock,
    TextInput,
)

if TYPE_CHECKING:
    from daimon.adapters.teams.runtime import TeamsRuntime
    from daimon.adapters.teams.thread_reader import ThreadReader
    from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
    from microsoft_teams.apps import ActivityContext
    from pydantic import JsonValue

__all__ = ["VERB", "AskActions", "ask_card", "claim_inbound"]

log = structlog.get_logger(__name__)
VERB: Final = answers.ASK_ACTION
_FAILED = "That didn't work. Try again."


def _boxes(ask: dict[str, Any]) -> answers.AskForm | None:
    shape = ask.get("shape")
    if shape == "choice":
        return None
    return answers.form_of(ask, waits.REJECT if shape == "approve" else waits.ANSWER)


def ask_card(text: str, ask: dict[str, Any]) -> AdaptiveCard:
    """The ask's words, its answer boxes and its buttons."""
    form = _boxes(ask)
    body: list[CardElement] = [TextBlock(text=text, wrap=True)]
    for box in () if form is None else form.fields:
        body.append(
            ChoiceSetInput(
                id=box.name,
                label=box.title,
                is_required=box.required,
                choices=[Choice(title=c, value=c) for c in box.choices],
            )
            if box.choices
            else TextInput(id=box.name, label=box.title, is_multiline=True, max_length=2000)
        )
    actions = [
        ExecuteAction(
            title=b.label,
            verb=VERB,
            data={"action": VERB, "custom_id": b.custom_id},
            style="positive" if b.primary else None,
        )
        for b in answers.buttons(ask)
    ]
    body.append(ActionSet(actions=list(actions)))
    return AdaptiveCard(body=body, fallback_text=text)


class AskActions:
    """Answers presses on ask cards."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        try:
            return toast(await self._press(ctx.activity))
        except Exception as err:
            # Boundary: a failed press must answer the presser, not leave the click hanging.
            log.exception("workflow_ask.press_failed", err_type=type(err).__name__)
            return toast(_FAILED)

    async def _press(self, activity: AdaptiveCardInvokeActivity) -> str:
        runtime = self._runtime
        fields = submitted_fields(activity.value.action.data)
        parsed = answers.parse_action(str(fields.get("custom_id") or ""))
        actor = await card_actor(runtime, activity)
        message_id = activity.reply_to_id
        if parsed is None or actor is None or not message_id:
            return "This request is gone."
        wait_id, route = parsed
        at = answers.Pressed(
            platform="teams",
            tenant_ids=(actor.tenant_id,),
            channel_id=actor.conversation_id,
            message_id=message_id,
            wait_id=wait_id,
        )
        form = await answers.ask_form(runtime.sessionmaker, at=at, route=route)
        values = (
            {} if form is None else {b.name: str(fields.get(b.name) or "") for b in form.fields}
        )
        if form is not None and form.fields and not any(v.strip() for v in values.values()):
            return "Fill in the box above, then press the button again."
        comment = str(fields.get("comment") or "").strip() or None
        sent, reason = answers.form_answer(route, values) if form else ({}, comment)
        claimed = await answers.press(
            runtime.sessionmaker,
            runtime.settings,
            at=at,
            author=actor.user_id,
            route=route,
            values=sent,
            comment=reason,
        )
        return claimed.text or "Your answer is in."


def _files(
    runtime: TeamsRuntime, parsed: TeamsInbound, reader: ThreadReader | None
) -> answers.Ingest | None:
    """The reply's images and files as Files API refs, read through Graph when it answers.

    A channel activity carries only text, so its media are Graph's copy: hosted images with
    the Graph token, shared files from SharePoint once an admin granted the channel's files."""
    if reader is None or not parsed.files:
        return None

    async def ingest() -> list[dict[str, JsonValue]]:
        media = await reader.read_media(parsed)
        if media is None:
            return []
        token = await reader.token()
        wanted = [(f"image-{i + 1}", url, True) for i, url in enumerate(media.image_urls)]
        wanted += [(f.name, f.download_url, False) for f in media.files if f.download_url]
        read: list[Attachment] = []
        for name, url, hosted in wanted[: answers.MAX_FILES]:
            try:
                content = await fetch_bytes(
                    runtime.http_client,
                    url,
                    is_allowed=is_graph_url if hosted else is_sharepoint_host,
                    token=token if hosted else None,
                    max_bytes=answers.MAX_FILE_BYTES,
                )
            except (FetchRefused, httpx.HTTPError) as err:
                # Never the URL: a download URL is itself a credential.
                log.warning("workflow_ask.file_read_failed", err_type=type(err).__name__)
                continue
            mime = sniff_image_media_type(content) if hosted else mimetypes.guess_type(name)[0]
            read.append(Attachment(name, mime or "application/octet-stream", content))
        return await answers.ingest_files(runtime.anthropic, read)

    return ingest


async def claim_inbound(
    runtime: TeamsRuntime,
    parsed: TeamsInbound,
    tenant_id: uuid.UUID,
    sender: TeamsSender,
    reader: ThreadReader | None = None,
) -> bool:
    """Answer an ask with a reply in a channel thread it opened. True when no turn follows.

    Personal chats have no threads, so an ask sent by DM is answered on its card."""
    if parsed.kind != "channel" or ";messageid=" not in parsed.conversation_id:
        return False
    claimed = await answers.claim_reply(
        runtime.sessionmaker,
        runtime.settings,
        answers.Reply(
            platform="teams",
            tenant_ids=(tenant_id,),
            author=parsed.user_id,
            message_id=parsed.activity_id,
            text=parsed.text.strip(),
            thread_id=parsed.conversation_id,
            files=_files(runtime, parsed, reader),
        ),
    )
    if claimed is None:
        return False
    if claimed.text is not None:
        card = notice_card(claimed.text, error=claimed.status == "refused")
        with contextlib.suppress(*TEAMS_SEND_ERRORS):
            await asyncio.wait_for(
                sender.send(parsed.conversation_id, card, service_url=parsed.service_url),
                SEND_TIMEOUT_S,
            )
    return True

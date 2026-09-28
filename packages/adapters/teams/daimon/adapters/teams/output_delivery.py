"""Session output files to Teams: a consent card in 1:1 chats, a note in channels.

Thin adapter over `sweep_session_outputs`. In a 1:1 chat each file is offered
with a FileConsentCard and the post defers, so the file stays listed until the
person accepts (upload, then delete) or declines (delete). The card's context
round-trips through the client, so it carries only a random token keyed to a
server-side offer that is checked against the clicker and expires. Bots
cannot upload into channels without Microsoft Graph, so a channel thread gets
a short note per file instead, through the sweep's skip path.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, cast

import anthropic
import httpx
import structlog
from daimon.adapters.teams.attachments import FetchRefused, is_sharepoint_host
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.interactions import resolve_actor
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.media.filenames import display_filename_for, sanitize_title
from daimon.core.output_delivery import (
    MAX_BYTES_PER_FILE,
    DeliverableFile,
    OutputDeliveryDeferred,
    SkippedFile,
    delete_output_file,
    download_output_file,
    render_oversize_notice,
    sweep_session_outputs,
)
from microsoft_teams.api import (
    Attachment,
    FileConsentCard,
    FileConsentInvokeActivity,
    FileInfoCard,
    FileUploadInfo,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext

log = structlog.get_logger()

FILE_CONSENT_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
FILE_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.info"
OFFER_TTL_S = 3600.0
_EXPIRED = "That file offer has expired. Ask me again and I'll resend it."
_UPLOAD_FAILED = "I couldn't upload `{name}`. Ask me again to retry."
_DECLINED = "Okay, I won't send `{name}`."
_CHANNEL_SKIP = (
    "I made `{name}`, but I can't attach files in channels. Ask me in a 1:1 chat when you "
    "need a file."
)


class Spawn(Protocol):
    """`TeamsApp._spawn`: runs a tracked background task the drain waits for."""

    def __call__(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]: ...


@dataclass(frozen=True)
class _Offer:
    """A consent card awaiting a click. Nothing here comes from the client."""

    file_id: str
    filename: str
    session_id: str
    user_id: str
    conversation_id: str
    service_url: str | None
    expires_at: float


def consent_card(name: str, size_bytes: int, token: str) -> MessageActivityInput:
    context = {"offer": token}
    card = FileConsentCard(
        description=name, size_in_bytes=size_bytes, accept_context=context, decline_context=context
    )
    attachment = Attachment(content_type=FILE_CONSENT_CONTENT_TYPE, name=name, content=card)
    return MessageActivityInput().add_attachments(attachment)


def file_info_message(info: FileUploadInfo) -> MessageActivityInput:
    card = FileInfoCard(unique_id=info.unique_id, file_type=info.file_type)
    attachment = Attachment(
        content_type=FILE_INFO_CONTENT_TYPE,
        name=info.name,
        content_url=info.content_url,
        content=card,
    )
    return MessageActivityInput().add_attachments(attachment)


class TeamsOutputDelivery:
    """Post-turn output sweeps and the consent-card replies they lead to."""

    def __init__(
        self,
        *,
        runtime: TeamsRuntime,
        sender: TeamsSender,
        spawn: Spawn,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._runtime = runtime
        self._sender = sender
        self._spawn = spawn
        self._clock = clock
        self._sleep = sleep
        self._offers: dict[str, _Offer] = {}
        self._sweeps: dict[str, asyncio.Task[Any]] = {}

    async def _say(self, conversation_id: str, service_url: str | None, text: str) -> None:
        await self._sender.send(
            conversation_id, MessageActivityInput(text=text), service_url=service_url
        )

    async def sweep(self, inbound: TeamsInbound, session_id: str) -> None:
        """Deliver the session's outputs. Chained per session: two sweeps never overlap."""
        previous = self._sweeps.get(session_id)
        current = asyncio.current_task()
        if current is not None:
            self._sweeps[session_id] = current
        try:
            if previous is not None:
                with contextlib.suppress(Exception):
                    await previous
            channel = inbound.kind == "channel"
            await sweep_session_outputs(
                self._runtime.anthropic,
                session_id=session_id,
                post=functools.partial(self._offer, inbound, session_id),
                on_skip=functools.partial(self._skip_notice, inbound, channel),
                sleep=self._sleep,
                # Zero sends every channel file down the skip path: a note, then delete.
                max_bytes=0 if channel else MAX_BYTES_PER_FILE,
            )
        finally:
            if self._sweeps.get(session_id) is current:
                self._sweeps.pop(session_id, None)

    async def _skip_notice(self, inbound: TeamsInbound, channel: bool, file: SkippedFile) -> None:
        text = render_oversize_notice(file)
        if channel:
            text = _CHANNEL_SKIP.format(name=sanitize_title(file.filename))
        await self._say(inbound.conversation_id, inbound.service_url, text)

    async def _offer(self, inbound: TeamsInbound, session_id: str, file: DeliverableFile) -> None:
        """Send a consent card, then defer: the click decides the file's fate."""
        now = self._clock()
        self._offers = {k: v for k, v in self._offers.items() if v.expires_at > now}
        if any(offer.file_id == file.file_id for offer in self._offers.values()):
            raise OutputDeliveryDeferred(file.file_id)  # Still waiting on the last card.
        token = secrets.token_urlsafe(24)
        name = display_filename_for(file.filename, file.mime_type)
        self._offers[token] = _Offer(
            file_id=file.file_id,
            filename=name,
            session_id=session_id,
            user_id=inbound.user_id,
            conversation_id=inbound.conversation_id,
            service_url=inbound.service_url,
            expires_at=now + OFFER_TTL_S,
        )
        try:
            await self._sender.send(
                inbound.conversation_id,
                consent_card(name, file.size_bytes, token),
                service_url=inbound.service_url,
            )
        except BaseException:
            self._offers.pop(token, None)
            raise
        raise OutputDeliveryDeferred(file.file_id)

    async def handle_consent(self, ctx: ActivityContext[FileConsentInvokeActivity]) -> None:
        """Accept or Decline on a consent card, from its offer's own person only."""
        activity = ctx.activity
        actor = await resolve_actor(
            self._runtime,
            conversation=activity.conversation,
            aad_object_id=activity.from_.aad_object_id,
        )
        if actor is None:
            return
        context = activity.value.context
        wire = cast("dict[str, object]", context) if isinstance(context, dict) else {}
        token = str(wire.get("offer", ""))
        offer = self._offers.get(token)
        if (
            offer is None
            or offer.expires_at <= self._clock()
            or (offer.user_id, offer.conversation_id) != (actor.user_id, actor.conversation_id)
        ):
            service_url = ctx.conversation_ref.service_url
            say = self._say(activity.conversation.id, service_url, _EXPIRED)
            self._spawn(say, name="teams.file-consent")
            return
        del self._offers[token]
        upload = activity.value.upload_info
        if activity.value.action == "accept" and upload is not None:
            self._spawn(self._upload(offer, upload), name="teams.file-consent")
        else:
            self._spawn(self._decline(offer), name="teams.file-consent")

    async def _upload(self, offer: _Offer, info: FileUploadInfo) -> None:
        """PUT the bytes to the upload session, delete the output, show the file."""
        client = self._runtime.anthropic
        try:
            url = httpx.URL(info.upload_url or "")
            if not is_sharepoint_host(url):
                raise FetchRefused("upload URL is not on SharePoint")
            content = await download_output_file(client, offer.file_id)
            response = await self._runtime.http_client.put(
                url,
                content=content,
                headers={"Content-Range": f"bytes 0-{len(content) - 1}/{len(content)}"},
            )
            if not response.is_success:
                raise FetchRefused(f"upload failed with HTTP {response.status_code}")
        except (FetchRefused, anthropic.APIError, httpx.InvalidURL, *TEAMS_SEND_ERRORS) as err:
            # The output stays listed, so the next sweep offers it again.
            log.warning("teams.file_upload.failed", file_id=offer.file_id, error=type(err).__name__)
            await self._say(
                offer.conversation_id, offer.service_url, _UPLOAD_FAILED.format(name=offer.filename)
            )
            return
        await delete_output_file(client, session_id=offer.session_id, file_id=offer.file_id)
        await self._sender.send(
            offer.conversation_id, file_info_message(info), service_url=offer.service_url
        )

    async def _decline(self, offer: _Offer) -> None:
        await delete_output_file(
            self._runtime.anthropic, session_id=offer.session_id, file_id=offer.file_id
        )
        await self._say(
            offer.conversation_id, offer.service_url, _DECLINED.format(name=offer.filename)
        )

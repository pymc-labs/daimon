"""Session output files to Teams: a consent card in 1:1 chats, the channel's Files in channels.

Thin adapter over `sweep_session_outputs`. In a 1:1 chat each file is offered
with a FileConsentCard and the post defers, so the file stays listed until the
person accepts (upload, then delete) or declines (delete). The card's context
round-trips through the client, so it carries only a random token keyed to a
server-side offer that is checked against the clicker and expires. A file the
agent posted with send_message is offered by the MCP server instead, with a
signed token (`daimon.core.teams_file_offers`); its bytes are a staged upload.

In a channel each file is uploaded to the channel's Files folder through
Graph (`channel_files`), and the links are edited in below the answer, or
sent as one message when they do not fit (never after an unprompted
answer). A failed, oversize or declined file is only logged: status text
around an answer is thread clutter. Without access a file goes down the
skip path to be logged, and the agent guidance has the agent say so in its
reply.
Either way the listing entry, the delivery ledger, is deleted; the sandbox
keeps its copy, so the agent can still read or paste it later.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
import httpx
import structlog
from daimon.adapters.teams.attachments import FetchRefused
from daimon.adapters.teams.card_actions import Actor, card_actor, submitted_fields
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.media.filenames import display_filename_for, sanitize_title
from daimon.core.output_delivery import (
    DeliverableFile,
    OutputDeliveryDeferred,
    SkippedFile,
    delete_output_file,
    download_output_file,
    sweep_session_outputs,
)
from daimon.core.stores.file_uploads import get_upload
from daimon.core.teams_file_offers import FILE_CONSENT_CONTENT_TYPE, UPLOAD_KEY, verify_offer
from daimon.core.teams_graph import GraphUnavailable, is_sharepoint_host
from daimon.core.teams_sharepoint import file_link
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

FILE_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.info"
OFFER_TTL_S = 3600.0
_EXPIRED = "That file offer has expired. Ask me again and I'll resend it."
_UPLOAD_FAILED = "I couldn't upload `{name}`. Ask me again to retry."
_SAVED = "Saved to this channel's files:"

# Edits a line in below the answer on screen; False if it cannot go there.
AppendToAnswer = Callable[[str], Awaitable[bool]]


async def _log_channel_skip(file: SkippedFile) -> None:
    log.info("teams.channel_output.skipped", file_id=file.file_id, size_bytes=file.size_bytes)


async def _log_skip(file: SkippedFile) -> None:
    log.info("teams.output.oversize", file_id=file.file_id, size_bytes=file.size_bytes)


class Spawn(Protocol):
    """`TeamsApp.spawn`: runs a tracked background task the drain waits for."""

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
        files: ChannelFiles | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._runtime = runtime
        self._sender = sender
        self._spawn = spawn
        self._files = files
        self._clock = clock
        self._sleep = sleep
        self._offers: dict[str, _Offer] = {}
        # Files whose click is being handled: still listed, never offered again.
        self._claimed: set[str] = set()
        self._sweeps: dict[str, asyncio.Task[Any]] = {}

    async def _say(self, conversation_id: str, service_url: str | None, text: str) -> None:
        await self._sender.send(
            conversation_id, MessageActivityInput(text=text), service_url=service_url
        )

    async def sweep(
        self, inbound: TeamsInbound, session_id: str, *, append: AppendToAnswer | None = None
    ) -> None:
        """Deliver the session's outputs. Chained per session: two sweeps never overlap.

        `append` puts a channel's file links below the turn's answer.
        """
        previous = self._sweeps.get(session_id)
        current = asyncio.current_task()
        if current is not None:
            self._sweeps[session_id] = current
        try:
            if previous is not None:
                with contextlib.suppress(Exception):
                    await previous
            if inbound.kind == "channel":
                await self._sweep_channel(inbound, session_id, append)
                return
            await sweep_session_outputs(
                self._runtime.anthropic,
                session_id=session_id,
                post=functools.partial(self._offer, inbound, session_id),
                on_skip=_log_skip,
                sleep=self._sleep,
            )
        finally:
            if self._sweeps.get(session_id) is current:
                self._sweeps.pop(session_id, None)

    async def _sweep_channel(
        self, inbound: TeamsInbound, session_id: str, append: AppendToAnswer | None
    ) -> None:
        files = self._files
        if files is None or not await files.is_available(inbound):
            # Zero sends every file down the skip path: a log line, then delete.
            await sweep_session_outputs(
                self._runtime.anthropic,
                session_id=session_id,
                post=functools.partial(self._offer, inbound, session_id),
                on_skip=_log_channel_skip,
                sleep=self._sleep,
                max_bytes=0,
            )
            return
        links: list[str] = []

        async def upload(file: DeliverableFile) -> None:
            name = display_filename_for(file.filename, file.mime_type)
            try:
                item = await files.upload(inbound, name, file.content)
            except GraphUnavailable as err:
                log.warning(
                    "teams.channel_output.upload_failed",
                    file_id=file.file_id,
                    status=err.status,
                    reason=err.reason,
                )
                return
            links.append(file_link(sanitize_title(item.name or name), item.web_url))

        await sweep_session_outputs(
            self._runtime.anthropic,
            session_id=session_id,
            post=upload,
            on_skip=_log_skip,
            sleep=self._sleep,
        )
        if not links:
            return
        text = "\n".join([_SAVED, *links])
        if append is not None and await append(text):
            return
        if inbound.unprompted:
            # Nobody asked: an unprompted turn posts its answer and nothing else.
            log.info("teams.channel_output.links_withheld", reason="unprompted")
            return
        try:
            await self._sender.send(
                inbound.conversation_id,
                MessageActivityInput(text=text, text_format="markdown"),
                service_url=inbound.service_url,
            )
        except TEAMS_SEND_ERRORS:
            log.warning("teams.channel_output.links_failed", exc_info=True)

    async def _offer(self, inbound: TeamsInbound, session_id: str, file: DeliverableFile) -> None:
        """Send a consent card, then defer: the click decides the file's fate."""
        now = self._clock()
        self._offers = {k: v for k, v in self._offers.items() if v.expires_at > now}
        pending = {offer.file_id for offer in self._offers.values()} | self._claimed
        if file.file_id in pending:
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
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return
        fields = submitted_fields(activity.value.context)
        service_url = ctx.conversation_ref.service_url
        if UPLOAD_KEY in fields:
            staged = self._staged(activity, actor, str(fields[UPLOAD_KEY]), service_url)
            self._spawn(staged, name="teams.file-consent")
            return
        token = str(fields.get("offer", ""))
        offer = self._offers.get(token)
        if (
            offer is None
            or offer.expires_at <= self._clock()
            or (offer.user_id, offer.conversation_id) != (actor.user_id, actor.conversation_id)
        ):
            say = self._say(activity.conversation.id, service_url, _EXPIRED)
            self._spawn(say, name="teams.file-consent")
            return
        del self._offers[token]
        self._claimed.add(offer.file_id)
        upload = activity.value.upload_info
        if activity.value.action == "accept" and upload is not None:
            work = self._upload(offer, upload)
        else:
            work = self._decline(offer)
        self._spawn(self._unclaim_after(offer.file_id, work), name="teams.file-consent")

    async def _unclaim_after(self, file_id: str, work: Awaitable[None]) -> None:
        try:
            await work
        finally:
            self._claimed.discard(file_id)

    async def _upload(self, offer: _Offer, info: FileUploadInfo) -> None:
        """PUT the bytes, show the file, then remove its output listing."""
        client = self._runtime.anthropic
        try:
            await self._put(info, await download_output_file(client, offer.file_id))
        except (FetchRefused, anthropic.APIError, httpx.InvalidURL, *TEAMS_SEND_ERRORS) as err:
            # The output stays listed, so the next sweep offers it again.
            log.warning("teams.file_upload.failed", file_id=offer.file_id, error=type(err).__name__)
            await self._say(
                offer.conversation_id, offer.service_url, _UPLOAD_FAILED.format(name=offer.filename)
            )
            return
        await self._sender.send(
            offer.conversation_id, file_info_message(info), service_url=offer.service_url
        )
        await delete_output_file(client, session_id=offer.session_id, file_id=offer.file_id)

    async def _put(self, info: FileUploadInfo, content: bytes) -> None:
        """PUT `content` to the person's OneDrive upload session; `FetchRefused` on failure."""
        url = httpx.URL(info.upload_url or "")
        if not is_sharepoint_host(url):
            raise FetchRefused("upload URL is not on SharePoint")
        response = await self._runtime.http_client.put(
            url,
            content=content,
            headers={"Content-Range": f"bytes 0-{len(content) - 1}/{len(content)}"},
        )
        if not response.is_success:
            raise FetchRefused(f"upload failed with HTTP {response.status_code}")

    async def _staged(
        self,
        activity: FileConsentInvokeActivity,
        actor: Actor,
        token: str,
        service_url: str | None,
    ) -> None:
        """A file the agent posted with send_message: its offer is signed, its bytes staged."""
        conversation = activity.conversation.id
        teams = self._runtime.settings.teams
        secret = teams.client_secret.get_secret_value() if teams else ""
        offer = verify_offer(token, secret=secret, now=time.time()) if secret else None
        if offer is None or (offer.user_id, offer.conversation_id) != (
            actor.user_id,
            actor.conversation_id,
        ):
            await self._say(conversation, service_url, _EXPIRED)
            return
        info = activity.value.upload_info
        if activity.value.action != "accept" or info is None:
            return  # Declined: the staged upload expires on its own.
        async with self._runtime.sessionmaker() as session:
            row = await get_upload(session, tenant_id=actor.tenant_id, handle_id=offer.handle_id)
        if row is None or row.content is None:
            await self._say(conversation, service_url, _EXPIRED)
            return
        try:
            await self._put(info, row.content)
        except (FetchRefused, httpx.InvalidURL, *TEAMS_SEND_ERRORS) as err:
            log.warning("teams.file_upload.failed", handle_id=row.id, error=type(err).__name__)
            name = row.display_filename
            await self._say(conversation, service_url, _UPLOAD_FAILED.format(name=name))
            return
        await self._sender.send(conversation, file_info_message(info), service_url=service_url)

    async def _decline(self, offer: _Offer) -> None:
        await delete_output_file(
            self._runtime.anthropic, session_id=offer.session_id, file_id=offer.file_id
        )
        log.info("teams.file_consent.declined", file_id=offer.file_id)

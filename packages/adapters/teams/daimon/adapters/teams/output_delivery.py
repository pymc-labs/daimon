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
Image outputs also travel as inline previews (`image_preview`), up to
MAX_PREVIEWS in delivery order, edited into the answer's last message: in a
channel in the same edit as the links, in a 1:1 chat above the consent
cards. Where files cannot be saved, a group chat included, none is shown.
Up to MAX_PREVIEWS uploaded Office files and PDFs then show their first
page, Graph's thumbnail of each, in a second edit. Previews the answer
cannot take (a notice card, a refused edit) go in a message of their own,
as with no answer to edit. A failed preview is only logged.
An output named `<file>.preview.png` (`is_preview_output`) is the agent's own
picture of `<file>`, such as a document's first page in a 1:1 chat, where
Graph makes no thumbnail: it is only shown as a preview, never offered or
uploaded, then deleted. In a 1:1 chat it shows with the other previews, in
its document's place; in a channel it stands in for Graph's thumbnail.
Transient channel upload failures leave the listing entry for a later sweep,
and an oversize file stays listed on every path, including a channel without
file access. Otherwise the listing entry, the delivery
ledger, is deleted; the sandbox keeps its copy, so the agent can still read or
paste it later.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import anthropic
import httpx
import structlog
from daimon.adapters.teams.attachments import FetchRefused, fetch_bytes
from daimon.adapters.teams.card_actions import Actor, card_actor, submitted_fields
from daimon.adapters.teams.channel_files import ChannelFiles
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.image_preview import MAX_PREVIEWS, preview_attachment
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.media.filenames import display_filename_for, sanitize_title
from daimon.core.media.vision import sniff_image_media_type
from daimon.core.output_delivery import (
    MAX_BYTES_PER_FILE,
    DeliverableFile,
    OutputDeliveryDeferred,
    SkippedFile,
    delete_output_file,
    download_output_file,
    sweep_session_outputs,
)
from daimon.core.stores.file_uploads import get_upload
from daimon.core.teams_file_offers import (
    FILE_CONSENT_CONTENT_TYPE,
    UPLOAD_KEY,
    consent_description,
    verify_offer,
)
from daimon.core.teams_graph import GraphUnavailable, is_sharepoint_host
from daimon.core.teams_sharepoint import DriveItem, file_link
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
PREVIEW_SUFFIX = ".preview.png"

# Edits text, attachments or both in below the answer on screen; False if they cannot go there.
AppendToAnswer = Callable[[str | None, Sequence[Attachment]], Awaitable[bool]]

# Graph renders a first-page thumbnail of these, often seconds after the upload.
THUMBNAIL_SUFFIXES = (".docx", ".pptx", ".xlsx", ".pdf", ".doc", ".ppt", ".xls")
THUMBNAIL_ATTEMPTS = 3
THUMBNAIL_RETRY_S = 3.0
# All thumbnails together: the message they share waits at most this long.
THUMBNAIL_BUDGET_S = 10.0
THUMBNAIL_MAX_BYTES = 4 * 1024 * 1024
# Graph serves SharePoint thumbnails from SharePoint or from Microsoft's media
# service on `svc.ms`. The URL is pre-authorised and short-lived, so it gets no
# token; the host check keeps a URL in a Graph reply from sending us anywhere else.
_THUMBNAIL_HOST_SUFFIX = ".svc.ms"


def _is_thumbnail_host(url: httpx.URL) -> bool:
    return is_sharepoint_host(url) or (
        url.scheme == "https" and url.host.endswith(_THUMBNAIL_HOST_SUFFIX)
    )


def is_preview_output(filename: str) -> bool:
    """An output the agent made only to be seen inline: `<file>.preview.png`."""
    return filename.lower().endswith(PREVIEW_SUFFIX)


def preview_label(filename: str) -> str:
    """The name a preview output shows: the file it pictures."""
    return filename[: -len(PREVIEW_SUFFIX)] or filename


def picture_key(filename: str) -> str:
    """Matches a preview output's label to its document, as saved (`sanitize_title`)."""
    return sanitize_title(filename).lower()


def previews_first(filenames: Sequence[str]) -> list[int]:
    """A 1:1 sweep's order: each preview output just before its document, else in place.

    The agent renders a document before picturing it, so the listing may name
    the picture second; the previews then keep the order of the cards below.
    """
    documents = {picture_key(name) for name in filenames if not is_preview_output(name)}
    held: dict[str, list[int]] = {}
    for index, name in enumerate(filenames):
        if is_preview_output(name) and (key := picture_key(preview_label(name))) in documents:
            held.setdefault(key, []).append(index)
    order: list[int] = []
    for index, name in enumerate(filenames):
        if not is_preview_output(name):
            order.extend(held.pop(picture_key(name), []))
            order.append(index)
        elif picture_key(preview_label(name)) not in documents:
            order.append(index)
    return order


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
    # Neutral bytes stay server-side until consent; None retains native behavior.
    content: bytes | None = field(default=None, repr=False)


def consent_card(name: str, size_bytes: int, token: str) -> MessageActivityInput:
    context = {"offer": token}
    card = FileConsentCard(
        description=consent_description(name),
        size_in_bytes=size_bytes,
        accept_context=context,
        decline_context=context,
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
        # Files already previewed in a 1:1 chat: a re-offer (expired card, failed send)
        # repeats only the card. Per process, so a restart previews once more.
        self._previewed: set[str] = set()
        self._neutral_claimed_bytes: dict[str, int] = {}
        self._sweeps: dict[str, asyncio.Task[Any]] = {}
        # A decode can take hundreds of MB: at most two at once across all sweeps. Its own
        # pool, so queued decodes never fill the loop's default executor (to_thread, DNS).
        self._decoder = ThreadPoolExecutor(max_workers=2, thread_name_prefix="teams-preview")

    async def _say(self, conversation_id: str, service_url: str | None, text: str) -> None:
        await self._sender.send(
            conversation_id, MessageActivityInput(text=text), service_url=service_url
        )

    def close(self) -> None:
        """Drop queued decodes; one already running finishes on its thread."""
        self._decoder.shutdown(wait=False, cancel_futures=True)

    async def _preview(self, name: str, content: bytes, mime_type: str) -> Attachment | None:
        # Off the event loop: decoding and re-encoding a large image would stall other turns.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._decoder, preview_attachment, name, content, mime_type
        )

    async def _send_previews(self, inbound: TeamsInbound, previews: list[Attachment]) -> None:
        # An extra, never the file itself: a failure is logged and the delivery stands.
        if not previews:
            return
        try:
            await self._send_attachments(inbound, previews)
            return
        except httpx.HTTPStatusError as err:
            # Bot Framework's message size cap is undocumented: several previews together
            # may be refused (413, 400) where each alone fits. Only those are retried
            # one by one; any other status is not about size.
            status = err.response.status_code
            if len(previews) == 1 or status not in (400, 413):
                log.warning("teams.output_preview.send_failed", count=len(previews), status=status)
                return
        except TEAMS_SEND_ERRORS:
            # A timed-out or dropped send may have landed: a retry could show them twice.
            log.warning("teams.output_preview.send_failed", count=len(previews), exc_info=True)
            return
        for preview in previews:
            try:
                await self._send_attachments(inbound, [preview])
            except TEAMS_SEND_ERRORS:
                log.warning("teams.output_preview.send_failed", count=1, exc_info=True)

    async def _send_attachments(self, inbound: TeamsInbound, attachments: list[Attachment]) -> None:
        await self._sender.send(
            inbound.conversation_id,
            MessageActivityInput().add_attachments(*attachments),
            service_url=inbound.service_url,
        )

    async def sweep(
        self, inbound: TeamsInbound, session_id: str, *, append: AppendToAnswer | None = None
    ) -> None:
        """Deliver the session's outputs. Chained per session: two sweeps never overlap.

        `append` puts file links and previews below the turn's answer.
        """
        previous = self._sweeps.get(session_id)
        current = asyncio.current_task()
        if current is not None:
            self._sweeps[session_id] = current
        try:
            if previous is not None:
                with contextlib.suppress(Exception):
                    await previous
            if inbound.kind != "dm":  # a group chat has no files: each is skipped
                await self._sweep_channel(inbound, session_id, append)
                return
            previews: list[Attachment] = []
            await sweep_session_outputs(
                self._runtime.anthropic,
                session_id=session_id,
                post=functools.partial(self._post_direct, inbound, session_id, previews),
                on_skip=_log_skip,
                sleep=self._sleep,
                order=previews_first,
            )
            # In the answer, above the consent cards; else one message each.
            inline = previews[:MAX_PREVIEWS]
            if inline and append is not None and await append(None, inline):
                previews = previews[MAX_PREVIEWS:]
            for preview in previews:
                await self._send_previews(inbound, [preview])
        finally:
            if self._sweeps.get(session_id) is current:
                self._sweeps.pop(session_id, None)

    async def _post_direct(
        self,
        inbound: TeamsInbound,
        session_id: str,
        previews: list[Attachment],
        file: DeliverableFile,
    ) -> None:
        """A 1:1 chat's output: offered, or a preview output only shown and then deleted.

        Either one's preview goes into `previews`, shown once the sweep ends.
        """
        if not is_preview_output(file.filename):
            await self._offer(inbound, session_id, file, previews=previews)
            return
        if (preview := await self._preview_output(file)) is not None:
            previews.append(preview)

    async def _preview_output(self, file: DeliverableFile) -> Attachment | None:
        mime_type = sniff_image_media_type(file.content) or file.mime_type
        return await self._preview(preview_label(file.filename), file.content, mime_type)

    async def _sweep_channel(
        self, inbound: TeamsInbound, session_id: str, append: AppendToAnswer | None
    ) -> None:
        files = self._files
        if files is None or not await files.is_available(inbound):
            await self._sweep_unsaved(inbound, session_id)
            return
        links: list[str] = []
        previews: list[Attachment] = []
        # Uploaded documents, by the name asked for, whose first page is looked up once
        # every file is in; one the agent made a preview output of needs none.
        documents: list[tuple[set[str], DriveItem]] = []
        pictured: set[str] = set()

        async def upload(file: DeliverableFile) -> None:
            if is_preview_output(file.filename):
                pictured.add(picture_key(preview_label(file.filename)))
                if (
                    not inbound.unprompted
                    and len(previews) < MAX_PREVIEWS
                    and (preview := await self._preview_output(file)) is not None
                ):
                    previews.append(preview)
                return  # Never uploaded: the sweep deletes it.
            name = display_filename_for(file.filename, file.mime_type)
            preview = None
            if not inbound.unprompted and len(previews) < MAX_PREVIEWS:
                # Before the upload: a preview error must never re-upload a saved file.
                preview = await self._preview(name, file.content, file.mime_type)
            try:
                item = await files.upload(inbound, name, file.content)
            except GraphUnavailable as err:
                log.warning(
                    "teams.channel_output.upload_failed",
                    file_id=file.file_id,
                    status=err.status,
                    reason=err.reason,
                )
                # No status covers token/transport failures; 408, 423 (a locked
                # file or folder), 429 and 5xx can clear on a later sweep. Keep
                # 401/403 and other permanent responses on the existing skip path
                # rather than repeatedly retrying denied access.
                if err.status is None or err.status in (408, 423, 429) or 500 <= err.status < 600:
                    raise
                return
            links.append(file_link(sanitize_title(item.name or name), item.web_url))
            if preview is not None:
                previews.append(preview)
            elif (
                not inbound.unprompted
                and len(documents) < MAX_PREVIEWS
                and (item.name or name).lower().endswith(THUMBNAIL_SUFFIXES)
            ):
                # The name asked for, or as saved: a mime type may have added an extension.
                documents.append(({picture_key(file.filename), picture_key(name)}, item))

        await sweep_session_outputs(
            self._runtime.anthropic,
            session_id=session_id,
            post=upload,
            on_skip=_log_skip,
            sleep=self._sleep,
        )
        # Links and images in one edit of the answer; refused, each goes as on its own.
        text = "\n".join([_SAVED, *links])
        if not links:
            await self._show_previews(inbound, previews, append)
        elif not (previews and append is not None and await append(text, previews)):
            await self._post_links(inbound, text, append)
            await self._send_previews(inbound, previews)
        # Images first, as they are ready: a thumbnail may take seconds or never come,
        # so documents get an edit of their own rather than images' places.
        unpictured = [item for keys, item in documents if not keys & pictured]
        await self._show_previews(inbound, await self._thumbnails(files, unpictured), append)

    async def _show_previews(
        self, inbound: TeamsInbound, previews: list[Attachment], append: AppendToAnswer | None
    ) -> None:
        """`previews` below the answer, else in a message of their own."""
        if previews and append is not None and await append(None, previews):
            return
        await self._send_previews(inbound, previews)

    async def _sweep_unsaved(self, inbound: TeamsInbound, session_id: str) -> None:
        """A channel or group chat whose files cannot be saved: every file takes the skip path.

        A skipped file is logged, then deleted if it fits the cap; one over it
        is kept, as on every other path: no channel could take it either.
        """
        await sweep_session_outputs(
            self._runtime.anthropic,
            session_id=session_id,
            post=functools.partial(self._offer, inbound, session_id),
            on_skip=_log_channel_skip,
            sleep=self._sleep,
            max_bytes=0,
            discard_skipped_up_to=MAX_BYTES_PER_FILE,
        )

    async def _thumbnails(
        self, files: ChannelFiles, documents: list[DriveItem]
    ) -> list[Attachment]:
        """The first-page thumbnails of `documents` that came within the budget, in order."""
        thumbnails: dict[int, Attachment] = {}

        async def fetch(index: int, item: DriveItem) -> None:
            if (thumbnail := await self._thumbnail(files, item)) is not None:
                thumbnails[index] = thumbnail

        if documents:
            try:
                async with asyncio.timeout(THUMBNAIL_BUDGET_S):
                    await asyncio.gather(*(fetch(n, item) for n, item in enumerate(documents)))
            except TimeoutError:
                log.info("teams.output_thumbnail.skipped", reason="timed_out")
        return [thumbnails[index] for index in sorted(thumbnails)]

    async def _thumbnail(self, files: ChannelFiles, item: DriveItem) -> Attachment | None:
        """Graph's first-page thumbnail of an uploaded document, re-encoded as a preview.

        Graph may not have rendered one yet, so a missing thumbnail is asked
        for again a few times; any other failure is logged once and skipped.
        """
        for attempt in range(THUMBNAIL_ATTEMPTS):
            if attempt:
                await self._sleep(THUMBNAIL_RETRY_S)
            try:
                url = await files.thumbnail_url(item)
            except GraphUnavailable as err:
                if err.status == 404:  # The item's thumbnails are not there yet.
                    continue
                log.info("teams.output_thumbnail.skipped", reason=err.reason, status=err.status)
                return None
            if url is None:
                continue
            try:
                content = await fetch_bytes(
                    self._runtime.http_client,
                    url,
                    is_allowed=_is_thumbnail_host,
                    token=None,
                    max_bytes=THUMBNAIL_MAX_BYTES,
                )
            except (FetchRefused, httpx.HTTPError, httpx.InvalidURL) as err:
                log.info("teams.output_thumbnail.skipped", reason=type(err).__name__)
                return None
            mime_type = sniff_image_media_type(content) or "application/octet-stream"
            preview = await self._preview(item.name or "preview", content, mime_type)
            if preview is None:
                log.info("teams.output_thumbnail.skipped", reason="not_an_image")
            return preview
        log.info("teams.output_thumbnail.skipped", reason="not_ready")
        return None

    async def _post_links(
        self, inbound: TeamsInbound, text: str, append: AppendToAnswer | None
    ) -> None:
        if append is not None and await append(text, ()):
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

    async def deliver_provider_output(
        self,
        inbound: TeamsInbound,
        session_id: str,
        file: DeliverableFile,
        *,
        append: AppendToAnswer | None = None,
    ) -> bool:
        """Deliver verified neutral bytes via Graph or the existing consent/PUT path.

        Foreign artifacts are retained at their provider. Neither acceptance
        nor decline calls the Anthropic output helpers.
        """
        if (
            not file.content
            or file.size_bytes != len(file.content)
            or file.size_bytes > MAX_BYTES_PER_FILE
        ):
            return False
        if inbound.kind == "dm":
            # A consent ledger key binds the bytes and their actual destination.
            # It is local identity, never a provider resource ID or byte-read authority.
            identity = hashlib.sha256()
            for value in (
                file.file_id,
                session_id,
                inbound.user_id,
                inbound.conversation_id,
                inbound.service_url or "",
            ):
                identity.update(value.encode())
                identity.update(b"\x00")
            identity.update(file.content)
            file = replace(file, file_id=f"neutral:{identity.hexdigest()}")
            now = self._clock()
            self._offers = {k: v for k, v in self._offers.items() if v.expires_at > now}
            if file.file_id in self._claimed or any(
                v.file_id == file.file_id for v in self._offers.values()
            ):
                return True
            retained = sum(len(v.content or b"") for v in self._offers.values()) + sum(
                self._neutral_claimed_bytes.values()
            )
            if retained + file.size_bytes > 4 * MAX_BYTES_PER_FILE:
                return False
            try:
                await self._offer(inbound, session_id, file, content=file.content)
            except OutputDeliveryDeferred:
                return True
            return False
        files = self._files
        if files is None or not await files.is_available(inbound):
            return False
        name = display_filename_for(file.filename, file.mime_type)
        item = await files.upload(inbound, name, file.content)
        text = f"{_SAVED}\n{file_link(sanitize_title(item.name or name), item.web_url)}"
        if append is not None and await append(text, ()):
            return True
        if inbound.unprompted:
            return False
        await self._sender.send(
            inbound.conversation_id,
            MessageActivityInput(text=text, text_format="markdown"),
            service_url=inbound.service_url,
        )
        return True

    async def _offer(
        self,
        inbound: TeamsInbound,
        session_id: str,
        file: DeliverableFile,
        *,
        content: bytes | None = None,
        previews: list[Attachment] | None = None,
    ) -> None:
        """Send a consent card, then defer: the click decides the file's fate.

        The file's preview goes into `previews` when given, else just before the card.
        """
        now = self._clock()
        self._offers = {k: v for k, v in self._offers.items() if v.expires_at > now}
        pending = {offer.file_id for offer in self._offers.values()} | self._claimed
        if file.file_id in pending:
            raise OutputDeliveryDeferred(file.file_id)  # Still waiting on the last card.
        name = display_filename_for(file.filename, file.mime_type)
        if file.file_id not in self._previewed:
            self._previewed.add(file.file_id)
            preview = await self._preview(name, file.content, file.mime_type)
            if preview is not None and previews is not None:
                previews.append(preview)
            elif preview is not None:
                await self._send_previews(inbound, [preview])
        token = secrets.token_urlsafe(24)
        self._offers[token] = _Offer(
            file_id=file.file_id,
            filename=name,
            session_id=session_id,
            user_id=inbound.user_id,
            conversation_id=inbound.conversation_id,
            service_url=inbound.service_url,
            expires_at=now + OFFER_TTL_S,
            content=content,
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
        now = self._clock()
        self._offers = {k: v for k, v in self._offers.items() if v.expires_at > now}
        offer = self._offers.get(token)
        if (
            offer is None
            or offer.expires_at <= self._clock()
            or (offer.user_id, offer.conversation_id) != (actor.user_id, actor.conversation_id)
            or (offer.content is not None and offer.service_url != service_url)
        ):
            say = self._say(activity.conversation.id, service_url, _EXPIRED)
            self._spawn(say, name="teams.file-consent")
            return
        del self._offers[token]
        self._claimed.add(offer.file_id)
        self._previewed.discard(offer.file_id)
        if offer.content is not None:
            self._neutral_claimed_bytes[offer.file_id] = len(offer.content)
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
            self._neutral_claimed_bytes.pop(file_id, None)

    async def _upload(self, offer: _Offer, info: FileUploadInfo) -> None:
        """PUT the bytes, show the file, then remove its output listing."""
        client = self._runtime.anthropic
        try:
            content = offer.content
            if content is None:
                content = await download_output_file(client, offer.file_id)
            await self._put(info, content)
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
        if offer.content is None:
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
        if offer.content is None:
            await delete_output_file(
                self._runtime.anthropic, session_id=offer.session_id, file_id=offer.file_id
            )
        log.info("teams.file_consent.declined", file_id=offer.file_id)

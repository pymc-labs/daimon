"""Slack delivery of Managed Agents session output files.

Thin adapter over :func:`daimon.core.output_delivery.sweep_session_outputs`:
it contributes the Slack poster, the oversize skip notice, and the
degrade-not-block handling for the two workspace-wide error codes
(``missing_scope``, ``storage_limit_reached``). Everything else — polling,
the downloadable gate, post-then-delete ordering, per-file isolation — lives
in the core engine.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.slack.blockkit import notice_attachments
from daimon.core.media.filenames import display_filename_for
from daimon.core.output_delivery import (
    MAX_BYTES_CEILING,
    DeliverableFile,
    NoticedFiles,
    OutputPostingUnavailable,
    SkippedFile,
    render_oversize_notice,
    sweep_session_outputs,
)
from slack_sdk.errors import SlackApiError, SlackRequestError
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)

MAX_NOTICE_KEYS = 4096

# Slack takes far more, but the sweep holds the whole file in memory, so Daimon
# stops at its own ceiling.
SLACK_MAX_BYTES_PER_FILE = MAX_BYTES_CEILING

# The shared Slack session times every call out at 30 s. A file that would not
# upload in that time at a floor rate of 512 KiB/s (about 4 Mbit/s) goes through
# a client of its own, whose timeout is the 30 s plus its bytes at that rate:
# 200 MiB gets about 7 minutes. Smaller files keep the shared session.
_UPLOAD_BASE_TIMEOUT_S = 30
_UPLOAD_MIN_BYTES_PER_S = 512 * 1024
_SHARED_SESSION_MAX_BYTES = _UPLOAD_BASE_TIMEOUT_S * _UPLOAD_MIN_BYTES_PER_S


class NoticeKeys:
    """Bounded app-lifetime dedup state with temporary per-key post locks."""

    def __init__(self, *, max_keys: int = MAX_NOTICE_KEYS) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self.max_keys = max_keys
        self._keys: OrderedDict[str, None] = OrderedDict()
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    def __contains__(self, key: str) -> bool:
        if key not in self._keys:
            return False
        self._keys.move_to_end(key)
        return True

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key: str) -> None:
        self._keys[key] = None
        self._keys.move_to_end(key)
        self._trim()

    def _trim(self) -> None:
        # A posting key with waiters must remain visible until they check it.
        # The cache can exceed the cap only while those posts are active.
        if len(self._keys) <= self.max_keys:
            return
        for key in list(self._keys):
            if len(self._keys) <= self.max_keys:
                break
            if key not in self._locks:
                del self._keys[key]

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        # No await between lookup and reservation: concurrent callers share
        # one lock. References include waiters, so cleanup cannot split them.
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._locks[key]
            if users == 1:
                del self._locks[key]
                self._trim()
            else:
                self._locks[key] = (lock, users - 1)


# Workspace-wide, persistent failures: retrying per file (or per turn) is pure
# noise, so these abort the sweep and produce one notice per affected thread.
_ABORT_NOTICES: dict[str, str] = {
    # The app is missing the `files:write` scope.
    "missing_scope": (
        "I couldn't attach the file.\n\n"
        "Ask a workspace admin to allow file uploads and reinstall Daimon."
    ),
    "storage_limit_reached": (
        "I couldn't attach the file. This Slack workspace is out of file space.\n\n"
        "Ask a workspace admin to free some up."
    ),
}


def _slack_error_code(err: SlackApiError | SlackRequestError) -> str:
    """Read the API error code; a SlackRequestError (bytes-POST failure) has
    no ``.response`` attribute, so it carries no code."""
    if isinstance(err, SlackApiError):
        return str(err.response.get("error", ""))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
    return ""


def _abort_code(exc: OutputPostingUnavailable) -> str:
    """Re-derive the Slack error code from the abort's ``__cause__``."""
    cause = exc.__cause__
    if isinstance(cause, (SlackApiError, SlackRequestError)):
        return _slack_error_code(cause)
    return ""


def _uploader(web_client: AsyncWebClient, *, size_bytes: int) -> AsyncWebClient:
    """The client to upload ``size_bytes`` with: ``web_client``, or for a large
    file one whose timeout grows with the file.

    The shared session's timeout replaces the client's, so a client that keeps
    the session cannot be given more time. The client built here has no session:
    slack_sdk opens one per call with the client's timeout.
    """
    if size_bytes <= _SHARED_SESSION_MAX_BYTES:
        return web_client
    return AsyncWebClient(
        token=web_client.token,
        base_url=web_client.base_url,
        timeout=_UPLOAD_BASE_TIMEOUT_S + math.ceil(size_bytes / _UPLOAD_MIN_BYTES_PER_S),
        retry_handlers=web_client.retry_handlers,
        proxy=web_client.proxy,
        ssl=web_client.ssl,
    )


def _build_poster(
    web_client: AsyncWebClient, *, channel_id: str, thread_ts: str
) -> Callable[[DeliverableFile], Awaitable[None]]:
    async def post(file: DeliverableFile) -> None:
        display_name = display_filename_for(file.filename, file.mime_type)
        try:
            # No content_type kwarg: files_upload_v2 forwards it into the
            # files.completeUploadExternal query string, where it is a silent
            # no-op — Slack derives the preview from the filename extension.
            await _uploader(web_client, size_bytes=len(file.content)).files_upload_v2(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel_id,
                thread_ts=thread_ts,
                filename=display_name,
                title=display_name,
                content=file.content,
            )
        except (SlackApiError, SlackRequestError) as err:
            code = _slack_error_code(err)
            if code in _ABORT_NOTICES:
                raise OutputPostingUnavailable(code) from err
            # Anything else — including every SlackRequestError — is a
            # per-file failure the core engine isolates without deleting.
            raise

    return post


def _build_skip_notice(
    web_client: AsyncWebClient, *, channel_id: str, thread_ts: str, max_bytes: int
) -> Callable[[SkippedFile], Awaitable[None]]:
    async def on_skip(skipped: SkippedFile) -> None:
        await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id,
            thread_ts=thread_ts,
            attachments=notice_attachments(render_oversize_notice(skipped, max_bytes=max_bytes)),
        )

    return on_skip


async def deliver_session_outputs(
    anthropic_client: AsyncAnthropic,
    web_client: AsyncWebClient,
    *,
    session_id: str,
    channel_id: str,
    thread_ts: str,
    notice_keys: NoticeKeys,
    team_id: str,
    noticed_files: NoticedFiles | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sweep the session's outputs into the thread, degrading on abort codes.

    ``notice_keys`` is the instance-level bounded registry owned by ``SlackApp``,
    keyed per thread for notices and per workspace for the warning log.
    ``noticed_files`` records the kept oversize outputs already announced, so
    a file over the limit gets one notice rather than one per sweep.
    """
    post = _build_poster(web_client, channel_id=channel_id, thread_ts=thread_ts)
    on_skip = _build_skip_notice(
        web_client,
        channel_id=channel_id,
        thread_ts=thread_ts,
        max_bytes=SLACK_MAX_BYTES_PER_FILE,
    )
    try:
        await sweep_session_outputs(
            anthropic_client,
            session_id=session_id,
            post=post,
            on_skip=on_skip,
            sleep=sleep,
            max_bytes=SLACK_MAX_BYTES_PER_FILE,
            noticed=noticed_files,
        )
    except OutputPostingUnavailable as exc:
        code = _abort_code(exc)
        notice = _ABORT_NOTICES.get(code)
        if notice is None:
            return
        log_key = f"log:{team_id}:{code}"
        if log_key not in notice_keys:
            log.warning(
                "slack.output_delivery.unavailable",
                session_id=session_id,
                team_id=team_id,
                code=code,
                needed_scope="files:write" if code == "missing_scope" else None,
            )
            notice_keys.add(log_key)
        notice_key = f"notice:{team_id}:{channel_id}:{thread_ts}:{code}"
        async with notice_keys.lock(notice_key):
            if notice_key in notice_keys:
                return
            await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel_id,
                thread_ts=thread_ts,
                attachments=notice_attachments(notice),
            )
            notice_keys.add(notice_key)


async def deliver_provider_output(
    file: DeliverableFile,
    web_client: AsyncWebClient,
    *,
    channel_id: str,
    thread_ts: str,
) -> bool:
    """Upload verified neutral bytes through Slack's existing three-request flow."""
    if (
        not file.content
        or file.size_bytes != len(file.content)
        or file.size_bytes > SLACK_MAX_BYTES_PER_FILE
    ):
        return False
    await _build_poster(web_client, channel_id=channel_id, thread_ts=thread_ts)(file)
    return True

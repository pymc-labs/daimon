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
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from anthropic import AsyncAnthropic
from daimon.core.media.filenames import display_filename_for
from daimon.core.output_delivery import (
    DeliverableFile,
    OutputPostingUnavailable,
    SkippedFile,
    render_oversize_notice,
    sweep_session_outputs,
)
from slack_sdk.errors import SlackApiError, SlackRequestError
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)

MAX_NOTICE_KEYS = 4096


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
    "missing_scope": (
        "I couldn't attach the generated file because this app is missing the "
        "`files:write` Slack scope. A workspace admin must add that scope and "
        "reinstall daimon from the install link before file delivery can work."
    ),
    "storage_limit_reached": (
        "I couldn't attach the generated file because this workspace has hit "
        "its Slack file-storage limit. A workspace admin must free up space or "
        "upgrade the plan before file delivery can work."
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


def _build_poster(
    web_client: AsyncWebClient, *, channel_id: str, thread_ts: str
) -> Callable[[DeliverableFile], Awaitable[None]]:
    async def post(file: DeliverableFile) -> None:
        display_name = display_filename_for(file.filename, file.mime_type)
        try:
            # No content_type kwarg: files_upload_v2 forwards it into the
            # files.completeUploadExternal query string, where it is a silent
            # no-op — Slack derives the preview from the filename extension.
            await web_client.files_upload_v2(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
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
    web_client: AsyncWebClient, *, channel_id: str, thread_ts: str
) -> Callable[[SkippedFile], Awaitable[None]]:
    async def on_skip(skipped: SkippedFile) -> None:
        await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id, thread_ts=thread_ts, text=render_oversize_notice(skipped)
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
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sweep the session's outputs into the thread, degrading on abort codes.

    ``notice_keys`` is the instance-level bounded registry owned by ``SlackApp``,
    keyed per thread for notices and per workspace for the warning log.
    """
    post = _build_poster(web_client, channel_id=channel_id, thread_ts=thread_ts)
    on_skip = _build_skip_notice(web_client, channel_id=channel_id, thread_ts=thread_ts)
    try:
        await sweep_session_outputs(
            anthropic_client, session_id=session_id, post=post, on_skip=on_skip, sleep=sleep
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
            )
            notice_keys.add(log_key)
        notice_key = f"notice:{team_id}:{channel_id}:{thread_ts}:{code}"
        async with notice_keys.lock(notice_key):
            if notice_key in notice_keys:
                return
            await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel_id,
                thread_ts=thread_ts,
                text=notice,
            )
            notice_keys.add(notice_key)

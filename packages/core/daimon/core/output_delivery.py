"""Post-then-delete sweep of a Managed Agents session's output files.

The sweep polls the session's Files API listing, downloads each downloadable
entry, hands it to an injected ``post`` callable, and deletes the listing
entry only after the post succeeded. Because a posted file leaves the listing,
the listing itself is the delivery ledger: it only ever holds undelivered
work, so delivery keeps no dedup state, no clock heuristic, and no age cutoff. A
poster whose delivery finishes later (a consent prompt) raises
``OutputDeliveryDeferred`` and deletes the entry itself when it is done.

Polling uses a settle floor rather than a naive "no new ids means done" rule.
MA's indexing lag is ~5s from the file WRITE, not from session idle — a file
written mid-turn is usually already indexed when the sweep starts, so a
first-poll listing that looks stable can still be missing a sibling written in
the turn's final seconds. The sweep therefore never concludes — neither
"settled with files" nor "give up empty" — before ``_MIN_SETTLE_S`` of
cumulative poll time has elapsed.

An output over the per-file delivery cap is never deleted: the sweep leaves it
listed, so the file stays downloadable and in the agent's workspace, and asks
the poster to say so once per file. That needs the one piece of dedup state,
``NoticedFiles``: the ids already noticed. Only a delivered file, or an empty
one, leaves the listing.

Two trade-offs are accepted by design: the MA-side copy of a file is destroyed
after a successful post, and a crash between the post and the delete
(including a SIGTERM that kills a detached sweep mid-flight) can double-post
that one file on the next sweep. The alternative — delete before posting —
silently loses files instead.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.core.checkpoint_prompt import HANDOFF_FILENAME_PREFIX
from daimon.core.errors import DaimonError
from daimon.core.media.filenames import sanitize_title
from daimon.core.output_ports_compat import (
    delete_output_record,
    list_output_records,
    read_output_record,
)
from mux.contracts.ids import Scope

_log = structlog.get_logger(__name__)

_MA_BETA = "managed-agents-2026-04-01"

_MIB = 1024 * 1024

# Public. The default per-file cap, for a caller that passes no ``max_bytes``:
# Teams, and what a transfer bundle must fit under (``checkpoint_prompt``). The
# Slack and Discord adapters pass their own limit instead, and the skip notice
# quotes whichever limit the sweep applied.
MAX_BYTES_PER_FILE = 20 * _MIB
# Public. No destination's cap goes above this: the sweep downloads each file
# whole into memory before the poster re-uploads it.
MAX_BYTES_CEILING = 200 * _MIB

_POLL_DELAYS_S = (0.0, 2.0, 4.0, 8.0)
_MIN_SETTLE_S = 6.0


@dataclass(frozen=True)
class DeliverableFile:
    """A downloadable session output, bytes in hand, ready for the poster."""

    file_id: str
    filename: str
    mime_type: str
    size_bytes: int
    content: bytes


@dataclass(frozen=True)
class SkippedFile:
    """An output skipped without downloading it (oversize). No ``content``.

    ``mime_type`` lets a caller that skips everything still pick some types
    to fetch on its own, such as an image to preview where nothing is saved.
    """

    file_id: str
    filename: str
    size_bytes: int
    mime_type: str = "application/octet-stream"


class OutputPostingUnavailable(DaimonError):
    """The poster cannot deliver at all (missing scope, workspace storage full).

    Aborts the sweep; nothing is deleted.
    """


class OutputDeliveryDeferred(DaimonError):
    """The poster handed the file to a later step (a consent prompt, say).

    The sweep leaves it listed and moves on; that step deletes it with
    :func:`delete_output_file` once the file is delivered or declined.
    """


def _mb(size_bytes: int) -> str:
    # Binary megabytes labelled MB, as Discord and Slack quote their own limits.
    return f"{size_bytes / _MIB:.1f}".removesuffix(".0")


def render_oversize_notice(skipped: SkippedFile, *, max_bytes: int = MAX_BYTES_PER_FILE) -> str:
    """The chat notice for an output over the per-file delivery cap.

    ``max_bytes`` is the limit the sweep applied; the notice quotes it.

    The file is kept, so the notice says so and names what the person can ask
    for instead; it never offers a download it cannot give.
    """
    limit = _mb(max_bytes)
    return (
        f"`{sanitize_title(skipped.filename)}` is {_mb(skipped.size_bytes)} MB, over the "
        f"{limit} MB upload limit here, so I couldn't attach it. It's still saved in my "
        "workspace: ask me for a smaller version, or to split it into parts under "
        f"{limit} MB, and I'll attach those."
    )


class NoticedFiles:
    """Bounded app-lifetime record of the kept oversize outputs already announced.

    A kept file is listed on every later sweep of its session; this stops each
    of those sweeps repeating the notice. Least recently seen ids fall out
    first, and a restart forgets them all, so a long-lived kept file can be
    announced once more after either.
    """

    def __init__(self, *, max_ids: int = 4096) -> None:
        if max_ids < 1:
            raise ValueError("max_ids must be positive")
        self._max_ids = max_ids
        self._ids: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, file_id: object) -> bool:
        if not isinstance(file_id, str) or file_id not in self._ids:
            return False
        self._ids.move_to_end(file_id)
        return True

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, file_id: str) -> None:
        self._ids[file_id] = None
        self._ids.move_to_end(file_id)
        while len(self._ids) > self._max_ids:
            self._ids.popitem(last=False)


async def _poll_until_settled(
    anthropic_client: AsyncAnthropic,
    *,
    session_id: str,
    sleep: Callable[[float], Awaitable[None]],
    scope: Scope | None = None,
) -> dict[str, FileMetadata]:
    """Poll the session listing until it settles; return downloadable entries by id.

    A poll "settles" the sweep only once cumulative elapsed time has reached
    ``_MIN_SETTLE_S`` AND that poll contributed no new downloadable file id.
    An exhausted schedule settles unconditionally.
    """
    seen: dict[str, FileMetadata] = {}
    elapsed = 0.0
    polls = 0
    for delay in _POLL_DELAYS_S:
        if delay > 0:
            await sleep(delay)
        elapsed += delay
        polls += 1
        # Retained oversize entries must not hide later pages.
        records = await list_output_records(
            anthropic_client,
            session_id,
            all_pages=True,
            scope=scope
            or Scope.legacy_host_authorized(call_site="output_delivery:_poll_until_settled"),
        )
        added_new_id = False
        for meta in records:
            if meta.downloadable is True and meta.id not in seen:
                seen[meta.id] = meta
                added_new_id = True
        if elapsed < _MIN_SETTLE_S:
            continue
        if not added_new_id:
            break
    _log.info(
        "output_delivery.settled",
        session_id=session_id,
        polls=polls,
        elapsed_s=elapsed,
        file_count=len(seen),
    )
    return seen


async def download_output_file(
    anthropic_client: AsyncAnthropic, file_id: str, *, scope: Scope | None = None
) -> bytes:
    """The bytes of one session output file."""
    return await read_output_record(
        anthropic_client,
        file_id,
        scope=scope
        or Scope.legacy_host_authorized(call_site="output_delivery:download_output_file"),
    )


async def delete_output_file(
    anthropic_client: AsyncAnthropic, *, session_id: str, file_id: str, scope: Scope | None = None
) -> None:
    """Delete a listing entry after its post won; failure to delete is logged, not raised."""
    try:
        await delete_output_record(
            anthropic_client,
            file_id,
            scope=scope
            or Scope.legacy_host_authorized(call_site="output_delivery:delete_output_file"),
        )
    except anthropic.NotFoundError:
        # An entry a concurrent sweep already removed is not an error.
        pass
    except anthropic.APIError as exc:
        _log.warning(
            "output_delivery.delete_failed",
            session_id=session_id,
            file_id=file_id,
            error=str(exc)[:300],
        )


async def sweep_session_outputs(
    anthropic_client: AsyncAnthropic,
    *,
    session_id: str,
    post: Callable[[DeliverableFile], Awaitable[None]],
    on_skip: Callable[[SkippedFile], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_bytes: int = MAX_BYTES_PER_FILE,
    exclude_filename_prefixes: tuple[str, ...] = (HANDOFF_FILENAME_PREFIX,),
    scope: Scope | None = None,
    noticed: NoticedFiles | None = None,
    discard_skipped_up_to: int = 0,
    order: Callable[[Sequence[str]], Sequence[int]] | None = None,
) -> int:
    """Deliver the session's output files via ``post``; return how many posted.

    ``order`` takes the listed filenames and returns the indexes to handle
    them in; without it they go in listing order.

    Raises :class:`OutputPostingUnavailable` when ``post`` signals that
    delivery is impossible for the whole workspace — nothing further is
    downloaded or deleted; files already posted-and-deleted stay deleted.

    ``exclude_filename_prefixes`` names outputs this sweep must not touch at
    all: neither posted nor deleted, so they stay in the listing for the
    machinery that owns them. The default excludes workspace-transfer
    bundles, which are written into the same outputs directory but belong to
    :mod:`daimon.core.workspace_transfer`, not to the user. The listing is
    basename-only (capability matrix P4.a), so a name prefix is the whole
    available key.

    An output over ``max_bytes`` is kept listed, never downloaded, and
    passed to ``on_skip`` unless ``noticed`` already holds its id; it is
    recorded there once ``on_skip`` returns, so a failed notice retries on
    the next sweep. Without ``noticed`` every sweep repeats the notice.
    ``discard_skipped_up_to`` is for a caller whose ``max_bytes`` means
    "cannot deliver anything here right now" (``max_bytes=0``): a skipped
    file no larger than it is deleted after its notice, as before, while a
    larger one, which no destination could take, is still kept.
    """
    settled = await _poll_until_settled(
        anthropic_client, session_id=session_id, sleep=sleep, scope=scope
    )

    listed = list(settled.values())
    if order is not None:
        listed = [listed[index] for index in order([meta.filename for meta in listed])]

    posted = 0
    for meta in listed:
        if exclude_filename_prefixes and meta.filename.startswith(exclude_filename_prefixes):
            # Left listed on purpose: the owner of this file deletes it.
            _log.info(
                "output_delivery.skipped_excluded",
                session_id=session_id,
                file_id=meta.id,
                filename=meta.filename,
            )
            continue

        if meta.size_bytes == 0:
            # Snapshot-at-first-write means a 0-byte entry can never gain
            # content; it is permanent noise unless deleted.
            _log.info("output_delivery.skipped_empty", session_id=session_id, file_id=meta.id)
            await delete_output_file(
                anthropic_client, session_id=session_id, file_id=meta.id, scope=scope
            )
            continue

        try:
            if meta.size_bytes > max_bytes:
                _log.info(
                    "output_delivery.skipped_oversize",
                    session_id=session_id,
                    file_id=meta.id,
                    size_bytes=meta.size_bytes,
                    max_bytes=max_bytes,
                )
                if on_skip is not None and (noticed is None or meta.id not in noticed):
                    # Notice before any delete: if the notice fails, the
                    # per-file boundary below keeps the entry listed and
                    # unrecorded, so the next sweep retries it.
                    await on_skip(
                        SkippedFile(
                            file_id=meta.id,
                            filename=meta.filename,
                            size_bytes=meta.size_bytes,
                            mime_type=meta.mime_type,
                        )
                    )
                    if noticed is not None:
                        noticed.add(meta.id)
                if meta.size_bytes <= discard_skipped_up_to:
                    await delete_output_file(
                        anthropic_client, session_id=session_id, file_id=meta.id, scope=scope
                    )
                # Otherwise kept: the person was told how to get it, and the
                # agent can still compress or split the file it wrote.
                continue

            await post(
                DeliverableFile(
                    file_id=meta.id,
                    filename=meta.filename,
                    mime_type=meta.mime_type,
                    size_bytes=meta.size_bytes,
                    content=await download_output_file(anthropic_client, meta.id, scope=scope),
                )
            )
        except OutputDeliveryDeferred:
            _log.info("output_delivery.deferred", session_id=session_id, file_id=meta.id)
            continue
        except OutputPostingUnavailable:
            _log.warning("output_delivery.aborted", session_id=session_id, file_id=meta.id)
            raise
        except Exception as exc:  # named boundary: per-file failures must not kill the sweep
            _log.warning(
                "output_delivery.file_failed",
                session_id=session_id,
                file_id=meta.id,
                error=str(exc)[:300],
            )
            continue

        await delete_output_file(
            anthropic_client, session_id=session_id, file_id=meta.id, scope=scope
        )
        posted += 1
        _log.info(
            "output_delivery.posted",
            session_id=session_id,
            file_id=meta.id,
            filename=meta.filename,
            size_bytes=meta.size_bytes,
        )

    return posted

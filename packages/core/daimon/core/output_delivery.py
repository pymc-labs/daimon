"""Post-then-delete sweep of a Managed Agents session's output files.

The sweep polls the session's Files API listing, downloads each downloadable
entry, hands it to an injected ``post`` callable, and deletes the listing
entry only after the post succeeded. Because a posted file leaves the listing,
the listing itself is the delivery ledger: it only ever holds undelivered
work, so there is no dedup state, no clock heuristic, and no age cutoff. A
poster whose delivery finishes later (a consent prompt) raises
``OutputDeliveryDeferred`` and deletes the entry itself when it is done.

Polling uses a settle floor rather than a naive "no new ids means done" rule.
MA's indexing lag is ~5s from the file WRITE, not from session idle — a file
written mid-turn is usually already indexed when the sweep starts, so a
first-poll listing that looks stable can still be missing a sibling written in
the turn's final seconds. The sweep therefore never concludes — neither
"settled with files" nor "give up empty" — before ``_MIN_SETTLE_S`` of
cumulative poll time has elapsed.

Two trade-offs are accepted by design: the MA-side copy of a file is destroyed
after a successful post, and a crash between the post and the delete
(including a SIGTERM that kills a detached sweep mid-flight) can double-post
that one file on the next sweep. The alternative — delete before posting —
silently loses files instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.core.checkpoint_prompt import HANDOFF_FILENAME_PREFIX
from daimon.core.errors import DaimonError
from daimon.core.media.filenames import sanitize_title

_log = structlog.get_logger(__name__)

_MA_BETA = "managed-agents-2026-04-01"

# Public: the skip notice quotes this limit to the user.
MAX_BYTES_PER_FILE = 20 * 1024 * 1024
_MIB = 1024 * 1024

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
    """An output skipped without downloading it (oversize). No ``content``."""

    file_id: str
    filename: str
    size_bytes: int


class OutputPostingUnavailable(DaimonError):
    """The poster cannot deliver at all (missing scope, workspace storage full).

    Aborts the sweep; nothing is deleted.
    """


class OutputDeliveryDeferred(DaimonError):
    """The poster handed the file to a later step (a consent prompt, say).

    The sweep leaves it listed and moves on; that step deletes it with
    :func:`delete_output_file` once the file is delivered or declined.
    """


def render_oversize_notice(skipped: SkippedFile) -> str:
    """The chat notice for an output over the per-file delivery cap."""
    return (
        f"I couldn't attach `{sanitize_title(skipped.filename)}` — it is "
        f"{skipped.size_bytes / _MIB:.1f} MiB, over the "
        f"{MAX_BYTES_PER_FILE // _MIB} MiB delivery limit."
    )


async def _poll_until_settled(
    anthropic_client: AsyncAnthropic,
    *,
    session_id: str,
    sleep: Callable[[float], Awaitable[None]],
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
        page = await anthropic_client.beta.files.list(
            scope_id=session_id, betas=[_MA_BETA], limit=1000
        )
        added_new_id = False
        for meta in page.data:
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


async def download_output_file(anthropic_client: AsyncAnthropic, file_id: str) -> bytes:
    """The bytes of one session output file."""
    response = await anthropic_client.beta.files.download(file_id, betas=[_MA_BETA])
    return await response.read()


async def delete_output_file(
    anthropic_client: AsyncAnthropic, *, session_id: str, file_id: str
) -> None:
    """Delete a listing entry after its post won; failure to delete is logged, not raised."""
    try:
        await anthropic_client.beta.files.delete(file_id, betas=[_MA_BETA])
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
    created_before: datetime | None = None,
) -> int:
    """Deliver the session's output files via ``post``; return how many posted.

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

    ``created_before`` limits the sweep to one turn's files. The listing is
    session-wide and the next turn may already be writing while this sweep
    settles, so an entry created at or after that moment is left listed,
    untouched, for the next turn's sweep.
    """
    settled = await _poll_until_settled(anthropic_client, session_id=session_id, sleep=sleep)

    posted = 0
    for meta in settled.values():
        if exclude_filename_prefixes and meta.filename.startswith(exclude_filename_prefixes):
            # Left listed on purpose: the owner of this file deletes it.
            _log.info(
                "output_delivery.skipped_excluded",
                session_id=session_id,
                file_id=meta.id,
                filename=meta.filename,
            )
            continue

        if created_before is not None and meta.created_at >= created_before:
            # Left listed on purpose: a later turn wrote it, and its sweep delivers it.
            _log.info(
                "output_delivery.left_for_next_turn",
                session_id=session_id,
                file_id=meta.id,
                filename=meta.filename,
            )
            continue

        if meta.size_bytes == 0:
            # Snapshot-at-first-write means a 0-byte entry can never gain
            # content; it is permanent noise unless deleted.
            _log.info("output_delivery.skipped_empty", session_id=session_id, file_id=meta.id)
            await delete_output_file(anthropic_client, session_id=session_id, file_id=meta.id)
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
                if on_skip is not None:
                    # Notice before delete: if the notice fails, the per-file
                    # boundary below keeps the entry listed so it retries.
                    await on_skip(
                        SkippedFile(
                            file_id=meta.id,
                            filename=meta.filename,
                            size_bytes=meta.size_bytes,
                        )
                    )
                await delete_output_file(anthropic_client, session_id=session_id, file_id=meta.id)
                continue

            await post(
                DeliverableFile(
                    file_id=meta.id,
                    filename=meta.filename,
                    mime_type=meta.mime_type,
                    size_bytes=meta.size_bytes,
                    content=await download_output_file(anthropic_client, meta.id),
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

        await delete_output_file(anthropic_client, session_id=session_id, file_id=meta.id)
        posted += 1
        _log.info(
            "output_delivery.posted",
            session_id=session_id,
            file_id=meta.id,
            filename=meta.filename,
            size_bytes=meta.size_bytes,
        )

    return posted

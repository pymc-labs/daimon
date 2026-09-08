"""Bytes-in routes: the publish archive and a reader-turn revised PDF.

Two separate `PUT` routes, one credential and one body format each — this is
what the prototype (`spikes/report-host/host/app.py`) got wrong, running
both through one handler with two token formats and two body formats. A
single handler that branches on what it received is the exact design this
replaces (T-21-17-F).

`PUT /publish/{capability_token}` (`application/gzip`) is the highest-risk
route in this module: it is authorised by a capability token signed with the
same secret set as the admin bearer, extracts one report PDF from an
untrusted archive without ever calling `TarFile.extract()`/`extractall()`,
persists the whole archive on the host's own volume (so the one re-push
after the seam's copy expires — SPEC D-03 — has something to send), and only
then hands it to the seam under the report's own token.

`PUT /upload/{turn_token}` (`application/pdf`) is the much smaller sibling:
a per-turn, single-use token minted by `routes.ask` authorises exactly one
revised-PDF upload for the thread that requested it.

Both tokens are burnt BEFORE the request body is read — a client that
disconnects mid-upload must not leave a token still usable (T-21-17-B). The
capability token's `jti` is burnt durably via `consumed_store` (mirroring
`notebook_host.consumed_store`, so a burn survives a host restart); the
per-turn upload token's burn is `reports_store.consume_upload_token`, an
atomic `DELETE ... RETURNING` — the row itself is the single-use record.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from report_host import reports_store, threads_store
from report_host.capability import verify_token
from report_host.config import Settings
from report_host.consumed_store import burn_jti
from report_host.mcp_client import SeamClient, SeamError, SeamUnauthorizedError

# One message per route, covering every failure reason that route has —
# unknown, tampered, expired, wrong-operation, already-used, and (for the
# publish route) a slug with no report all collapse into the same 404. A
# caller learns nothing about which reason applied (T-21-17-H).
_PUBLISH_INVALID_MESSAGE = "this publish link is invalid, expired, or already used"
_UPLOAD_INVALID_MESSAGE = "this upload link is invalid, expired, or already used"
_REPORT_UNAUTHORIZED_MESSAGE = (
    "this report's connection to daimon has expired; ask the publisher to re-publish it"
)

_REPORT_PDF_NAME = "report.pdf"
_BUNDLE_FILE_NAME = "bundle.tar.gz"

# In-memory threshold before a spooled upload spills to disk — neither route
# ever holds the full request body in memory beyond this, regardless of the
# eventual size cap enforced on top of it.
_SPOOL_MEMORY_BYTES = 1024 * 1024


class _ArchiveRejected(Exception):
    """A publish archive failed a structural check. Never escapes this module."""


class PublishLink(BaseModel):
    name: str
    link: str


class PublishUploadResponse(BaseModel):
    slug: str
    links: list[PublishLink]


class TurnUploadResponse(BaseModel):
    shown_as: str


async def _spool_body(
    request: Request, *, max_bytes: int
) -> tuple[tempfile.SpooledTemporaryFile[bytes], int]:
    """Stream the body into a spooled temp file, refusing mid-stream over ``max_bytes``.

    Never buffers the whole body before checking size — the running total is
    checked on every chunk, so an oversize upload is refused as soon as it
    crosses the cap rather than after it has all arrived (T-21-17-E).
    """
    # Not a `with` block: the caller owns this file's lifetime past this
    # function's return (it is read, rewound, and eventually `.close()`d by
    # the route handler once the upload is fully processed).
    spool: tempfile.SpooledTemporaryFile[bytes] = tempfile.SpooledTemporaryFile(  # noqa: SIM115
        max_size=_SPOOL_MEMORY_BYTES
    )
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            spool.close()
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE, detail="upload exceeds the size cap"
            )
        spool.write(chunk)
    spool.seek(0)
    return spool, total


def _check_gzip_magic(spool: IO[bytes]) -> None:
    spool.seek(0)
    magic = spool.read(2)
    spool.seek(0)
    if magic != b"\x1f\x8b":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="expected a gzip archive"
        )


def _check_pdf_magic(spool: IO[bytes]) -> None:
    spool.seek(0)
    magic = spool.read(4)
    spool.seek(0)
    if magic != b"%PDF":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="expected a PDF")


def _is_hostile_member(member: tarfile.TarInfo) -> bool:
    """Whether ``member`` could write, link, or resolve outside the report's directory.

    Checked against the member's raw, un-normalised name — the interpreter's
    own extraction filter (``tarfile.data_filter``, applied afterwards as a
    second layer) strips a leading ``/`` before deciding whether a name is
    absolute, so a raw absolute-path member survives it; these explicit
    checks are the real guard (T-21-17-C).
    """
    if member.name.startswith("/") or os.path.isabs(member.name):
        return True
    if ".." in Path(member.name).parts:
        return True
    if member.issym() or member.islnk():
        return True
    return bool(member.isdev())


def _extract_report_pdf(spool: IO[bytes], *, decompressed_ceiling: int) -> bytes:
    """Return the ``report.pdf`` member's bytes, extracted defensively.

    Never calls ``TarFile.extract()``/``extractall()`` — every member is
    validated (and, for a device/link/traversal member, rejected outright)
    before any single member is read, and only the one report-PDF member is
    ever decoded into memory. A malformed archive is a publisher bug worth
    surfacing, not a member to silently skip: the whole upload is refused.
    """
    spool.seek(0)
    try:
        with tarfile.open(fileobj=spool, mode="r:gz") as tar:
            return _read_report_member(tar, decompressed_ceiling=decompressed_ceiling)
    except tarfile.ReadError as err:
        raise _ArchiveRejected(f"not a valid gzip tar archive: {err}") from err


def _read_report_member(tar: tarfile.TarFile, *, decompressed_ceiling: int) -> bytes:
    """Validate every member, then read exactly the report-PDF member's bytes.

    Split out of `_extract_report_pdf` so the `tarfile.open` context manager
    can wrap this call directly (satisfying ruff's SIM115 without losing the
    `tarfile.ReadError` catch, which must wrap the `open()` call itself).
    """
    members = tar.getmembers()
    for member in members:
        if _is_hostile_member(member):
            raise _ArchiveRejected(f"archive contains an unsafe member: {member.name!r}")
        # The interpreter's own extraction filter (PEP 706), applied as a
        # second layer. Its behaviour is version-dependent (it silently
        # normalises a leading "/" rather than rejecting it, confirmed
        # against this interpreter — see `_is_hostile_member`'s
        # docstring), so the explicit checks above remain the real guard.
        try:
            tarfile.data_filter(member, "")
        except tarfile.FilterError as err:
            raise _ArchiveRejected(f"archive contains an unsafe member: {err}") from err

    report_member = next(
        (m for m in members if m.isfile() and m.name.removeprefix("./") == _REPORT_PDF_NAME),
        None,
    )
    if report_member is None:
        raise _ArchiveRejected(f"archive has no {_REPORT_PDF_NAME!r} at its root")
    if report_member.size > decompressed_ceiling:
        raise _ArchiveRejected(f"{_REPORT_PDF_NAME!r} exceeds the decompressed size ceiling")
    extracted = tar.extractfile(report_member)
    if extracted is None:
        raise _ArchiveRejected(f"{_REPORT_PDF_NAME!r} could not be read")
    # A compression bomb whose declared size lied would still be bounded
    # here: extractfile() never reads past the header's declared size,
    # so this loop can only ever read up to `report_member.size` bytes —
    # already checked above — regardless of the archive's compressed
    # size on disk.
    chunks: list[bytes] = []
    read_total = 0
    while True:
        chunk = extracted.read(65536)
        if not chunk:
            break
        read_total += len(chunk)
        if read_total > decompressed_ceiling:
            raise _ArchiveRejected(f"{_REPORT_PDF_NAME!r} exceeds the decompressed size ceiling")
        chunks.append(chunk)
    return b"".join(chunks)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via tmp + ``os.replace`` so a concurrent reader never sees a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def _persist_archive_for_publish(*, spool: IO[bytes], archive_path: Path) -> None:
    """Persist the spooled archive at ``archive_path``, tmp + fsync + ``os.replace``.

    ``bundle_ttl_days`` and ``recipient_link_ttl_days`` are both 90, so the
    seam's copy of a bundle expiring while a recipient link is still live is
    the normal case, not a rare one — this file is what the turn driver
    re-pushes when that happens (SPEC D-03). A crash mid-write must never
    leave a truncated archive standing at ``archive_path``: the write goes
    to a temp name in the same directory, is fsynced, and only then replaces
    the final name. Do not "clean this up" into a direct write — the fsync
    and the temp name are load-bearing, not redundant with the seam push
    that follows.
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = archive_path.with_name(f".{archive_path.name}.tmp")
    spool.seek(0)
    with open(tmp_path, "wb") as tmp_file:
        shutil.copyfileobj(spool, tmp_file)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, archive_path)
    spool.seek(0)


def _report_dir(*, data_dir: Path, slug: str) -> Path:
    """Compose and containment-check the report's directory.

    Composed only from ``data_dir`` and the slug the signed capability
    named — never from anything in the request path or the archive's own
    member names (T-21-17-I).
    """
    data_dir_resolved = data_dir.resolve()
    candidate = (data_dir_resolved / slug).resolve()
    if not candidate.is_relative_to(data_dir_resolved):
        raise _ArchiveRejected("resolved report directory escapes data_dir")
    return candidate


def _next_revision_name(conn: sqlite3.Connection, *, slug: str) -> str:
    existing = reports_store.list_revisions(conn, slug=slug)
    return f"v{len(existing) + 1}.pdf"


def build_uploads_router(
    *, settings: Settings, conn_factory: Callable[[], sqlite3.Connection], seam: SeamClient
) -> APIRouter:
    """Build the router carrying both upload routes.

    ``conn_factory`` is called exactly once, here, mirroring the reader and
    admin routers' single-shared-connection design.
    """
    conn = conn_factory()
    consumed_file = settings.data_dir / "consumed.json"
    router = APIRouter()

    @router.put("/publish/{capability_token}")
    async def publish_upload(capability_token: str, request: Request) -> PublishUploadResponse:  # pyright: ignore[reportUnusedFunction]
        claims = verify_token(settings.admin_secrets, capability_token, now=datetime.now(UTC))
        if claims is None or claims.op != "report":
            # Unknown, tampered, expired, and wrong-operation are the same
            # `verify_token` outcome (`None`) except wrong-operation, which
            # this explicit check catches for defence-in-depth even though
            # `CapabilityClaims.op`'s own type already excludes it — one 404
            # either way (T-21-17-A).
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_PUBLISH_INVALID_MESSAGE)

        # Burn before reading the body: a client that disconnects mid-upload
        # must not leave the capability still usable (T-21-17-B).
        burned = burn_jti(
            consumed_file,
            claims.jti,
            exp=claims.exp,
            now=int(datetime.now(UTC).timestamp()),
        )
        if not burned:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_PUBLISH_INVALID_MESSAGE)

        report = reports_store.load_report(conn, slug=claims.slug)
        if report is None:
            # The publisher's admin PUT always runs first, so a missing
            # report here is a genuine error, not a race — but the response
            # is identical to every other invalid-capability case
            # (T-21-17-H).
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_PUBLISH_INVALID_MESSAGE)

        ceiling = min(claims.max_bytes, settings.max_bundle_bytes)
        spool, size_bytes = await _spool_body(request, max_bytes=ceiling)
        try:
            _check_gzip_magic(spool)
            try:
                pdf_bytes = _extract_report_pdf(spool, decompressed_ceiling=settings.max_pdf_bytes)
            except _ArchiveRejected as err:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(err)) from err

            report_dir = _report_dir(data_dir=settings.data_dir, slug=report.slug)
            revision_name = _next_revision_name(conn, slug=report.slug)
            _atomic_write_bytes(report_dir / revision_name, pdf_bytes)
            reports_store.add_revision(
                conn,
                slug=report.slug,
                name=revision_name,
                by_thread=None,
                note="published",
                now=datetime.now(UTC),
            )
            reports_store.set_current_pdf(conn, slug=report.slug, name=revision_name)

            # Persist the archive itself, before touching the seam — this is
            # the file a later re-push reads (SPEC D-03). Written under the
            # same containment-checked directory the revision PDF just used.
            archive_path = report_dir / _BUNDLE_FILE_NAME
            _persist_archive_for_publish(spool=spool, archive_path=archive_path)

            # Read into bytes rather than handing httpx the still-open spooled
            # file: httpx's `AsyncClient` requires request content to be an
            # async byte stream, and a plain (sync) file-like object gets
            # wrapped as a *sync* iterator instead — raising at request time,
            # not at type-check time. The archive is already bounded by
            # `ceiling` above (at most `settings.max_bundle_bytes`), so
            # holding it in memory for this one push is the same order of
            # magnitude the cap already assumes.
            spool.seek(0)
            archive_bytes = spool.read()
            try:
                pushed = await seam.push_bundle(
                    token=report.seam_token, archive=archive_bytes, size_bytes=size_bytes
                )
            except SeamUnauthorizedError as err:
                reports_store.set_seam_status(conn, slug=report.slug, status="unauthorized")
                raise HTTPException(
                    status.HTTP_409_CONFLICT, detail=_REPORT_UNAUTHORIZED_MESSAGE
                ) from err
            except SeamError as err:
                # The archive stays on disk in this failure case too — a
                # re-publish is what replaces it, not this handler cleaning
                # up after itself.
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, detail="the seam refused the archive"
                ) from err

            reports_store.save_bundle_reference(
                conn,
                slug=report.slug,
                handle=pushed.handle,
                sha256=pushed.sha256,
                expires_at=datetime.fromisoformat(pushed.expires_at),
                archive_path=str(archive_path),
            )
        finally:
            spool.close()

        links = [
            PublishLink(
                name=recipient.name,
                link=f"{settings.public_url_base}r/{report.slug}?k={recipient.token}",
            )
            for recipient in reports_store.list_recipients(conn, slug=report.slug)
            if recipient.revoked_at is None
        ]
        return PublishUploadResponse(slug=report.slug, links=links)

    @router.put("/upload/{turn_token}")
    async def turn_upload(turn_token: str, request: Request) -> TurnUploadResponse:  # pyright: ignore[reportUnusedFunction]
        consumed = reports_store.consume_upload_token(conn, token=turn_token)
        if consumed is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_UPLOAD_INVALID_MESSAGE)
        thread = threads_store.load_thread_by_id(conn, thread_id=consumed.thread_id)
        if thread is None or thread.status != "running":
            # A token whose turn has already ended (thread back to idle, or
            # archived) is stale even though it was never spent — refused
            # with the same message as unknown-or-used.
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_UPLOAD_INVALID_MESSAGE)

        spool, _size_bytes = await _spool_body(request, max_bytes=settings.max_pdf_bytes)
        try:
            _check_pdf_magic(spool)
            spool.seek(0)
            pdf_bytes = spool.read()
        finally:
            spool.close()

        revision_name = _next_revision_name(conn, slug=consumed.slug)
        _atomic_write_bytes(settings.data_dir / consumed.slug / revision_name, pdf_bytes)
        reports_store.add_revision(
            conn,
            slug=consumed.slug,
            name=revision_name,
            by_thread=str(consumed.thread_id),
            note="revised by daimon",
            now=datetime.now(UTC),
        )
        reports_store.set_current_pdf(conn, slug=consumed.slug, name=revision_name)
        return TurnUploadResponse(shown_as=revision_name)

    return router

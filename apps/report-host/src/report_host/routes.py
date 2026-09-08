"""Reader-facing routes: the viewer, state and thread reads, and the files route.

The only part of the report host a browser ever reaches. Every route resolves
a per-recipient link token first, through the ``resolve_recipient`` dependency
built inside ``build_reader_router``, and every read below it is scoped to
that recipient (and, for threads, to the report's own slug) — the
store-level three-way scope ``threads_store.load_thread`` enforces. Unknown,
revoked and expired tokens are indistinguishable to a caller: one 403, one
message, so a guess learns nothing (T-21-15-A).

Admin, upload-token-consuming and publish routes are NOT here — they land in
later plans. This module owns only what a report's reader can reach.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from report_host import reports_store, threads_store
from report_host.config import Settings
from report_host.mcp_client import SeamClient
from report_host.reports_store import RecipientRow, SeamStatus
from report_host.threads_store import Role, ThreadStatus

_VIEWER_DIR = Path(__file__).parent / "viewer"

# A single literal for every reason a recipient dependency can fail: unknown
# token, revoked token, expired token. A reader whose link expired and a
# stranger guessing tokens get the same answer (T-21-15-A).
_RECIPIENT_INVALID_MESSAGE = "this link is not valid for this report"


class BudgetOut(BaseModel):
    cap_usd: Decimal
    spent_usd: Decimal
    reserve_usd: Decimal


class RevisionOut(BaseModel):
    name: str
    created_at: datetime
    note: str | None


class ThreadSummary(BaseModel):
    id: int
    title: str
    status: ThreadStatus
    created_at: datetime


class StateResponse(BaseModel):
    slug: str
    title: str
    current_pdf: str | None
    revisions: list[RevisionOut]
    budget: BudgetOut
    seam_status: SeamStatus
    recipient_name: str
    threads: list[ThreadSummary]


class MessageOut(BaseModel):
    id: int
    role: Role
    text: str
    created_at: datetime


class ThreadDetailResponse(BaseModel):
    id: int
    status: ThreadStatus
    elapsed_seconds: float | None
    messages: list[MessageOut]


def _validate_pdf_name(name: str) -> None:
    """Reject a traversal-shaped or non-PDF name before any path is built."""
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".pdf"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid file name")


def build_reader_router(
    *,
    settings: Settings,
    conn_factory: Callable[[], sqlite3.Connection],
    seam: SeamClient,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Build the reader-facing router.

    ``conn_factory`` is called exactly once, here, to obtain the single
    SQLite connection every route in this router shares — mirroring the
    one-connection design ``reports_store``/``threads_store`` are built
    around; production passes a factory that calls
    ``reports_store.connect(settings.data_dir)``, tests pass a lambda
    returning an already-open connection over a ``tmp_path`` file. ``now``
    is injected (never a global clock) so tests can control it. ``seam`` is
    unused by the routes in this file and consumed by ``ask``/``cancel``/
    ``close``, added in a later commit in this same plan.
    """
    conn = conn_factory()
    router = APIRouter()
    router.mount("/viewer", StaticFiles(directory=_VIEWER_DIR), name="viewer-assets")

    def resolve_recipient(slug: str, request: Request) -> RecipientRow:
        token = request.query_params.get("k") or request.cookies.get(f"rh_{slug}")
        recipient = (
            reports_store.load_recipient(conn, slug=slug, token=token, now=now()) if token else None
        )
        if recipient is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=_RECIPIENT_INVALID_MESSAGE)
        return recipient

    @router.get("/r/{slug}")
    def get_viewer(  # pyright: ignore[reportUnusedFunction]
        slug: str, recipient: Annotated[RecipientRow, Depends(resolve_recipient)]
    ) -> HTMLResponse:
        html = (_VIEWER_DIR / "index.html").read_text()
        response = HTMLResponse(html)
        response.set_cookie(
            f"rh_{slug}",
            recipient.token,
            httponly=True,
            samesite="lax",
            secure=True,
            max_age=settings.recipient_link_ttl_days * 86400,
        )
        return response

    @router.get("/api/{slug}/state")
    def get_state(  # pyright: ignore[reportUnusedFunction]
        slug: str, recipient: Annotated[RecipientRow, Depends(resolve_recipient)]
    ) -> StateResponse:
        report = reports_store.load_report(conn, slug=slug)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        revisions = reports_store.list_revisions(conn, slug=slug)
        threads = threads_store.list_threads(conn, slug=slug, recipient_token=recipient.token)
        return StateResponse(
            slug=slug,
            title=report.title,
            current_pdf=report.current_pdf,
            revisions=[
                RevisionOut(name=r.name, created_at=r.created_at, note=r.note) for r in revisions
            ],
            budget=BudgetOut(
                cap_usd=report.cap_usd,
                spent_usd=report.spent_usd,
                reserve_usd=settings.reserve_usd,
            ),
            seam_status=report.seam_status,
            recipient_name=recipient.name,
            threads=[
                ThreadSummary(id=t.id, title=t.title, status=t.status, created_at=t.created_at)
                for t in threads
            ],
        )

    @router.get("/api/{slug}/threads/{thread_id}")
    def get_thread(  # pyright: ignore[reportUnusedFunction]
        slug: str,
        thread_id: int,
        recipient: Annotated[RecipientRow, Depends(resolve_recipient)],
        after: int = 0,
    ) -> ThreadDetailResponse:
        thread = threads_store.load_thread(
            conn, thread_id=thread_id, slug=slug, recipient_token=recipient.token
        )
        if thread is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        elapsed_seconds: float | None = None
        if thread.status == "running" and thread.turn_started_at is not None:
            elapsed_seconds = (now() - thread.turn_started_at).total_seconds()
        messages = threads_store.list_messages(conn, thread_id=thread.id, after_id=after)
        return ThreadDetailResponse(
            id=thread.id,
            status=thread.status,
            elapsed_seconds=elapsed_seconds,
            messages=[
                MessageOut(id=m.id, role=m.role, text=m.text, created_at=m.created_at)
                for m in messages
            ],
        )

    @router.get("/files/{slug}/{name}")
    def get_file(  # pyright: ignore[reportUnusedFunction]
        slug: str, name: str, recipient: Annotated[RecipientRow, Depends(resolve_recipient)]
    ) -> FileResponse:
        _validate_pdf_name(name)
        path = settings.data_dir / slug / name
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return FileResponse(path, media_type="application/pdf")

    return router

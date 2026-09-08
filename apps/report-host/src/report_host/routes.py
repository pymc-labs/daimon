"""Reader-facing routes: the viewer, state and thread reads, ask, cancel, close, files.

The only part of the report host a browser ever reaches. Every route resolves
a per-recipient link token first, through the ``resolve_recipient`` dependency
built inside ``build_reader_router``, and every read and write below it is
scoped to that recipient (and, for threads, to the report's own slug) — the
store-level three-way scope ``threads_store.load_thread`` enforces. Unknown,
revoked and expired tokens are indistinguishable to a caller: one 403, one
message, so a guess learns nothing (T-21-15-A).

Admin, upload-token-consuming and publish routes are NOT here — they land in
later plans. This module owns only what a report's reader can reach.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from report_host import reports_store, threads_store, turns
from report_host.config import Settings
from report_host.mcp_client import SeamClient
from report_host.reports_store import RecipientRow, SeamStatus
from report_host.threads_store import Role, ThreadStatus

_VIEWER_DIR = Path(__file__).parent / "viewer"

# A single literal for every reason a recipient dependency can fail: unknown
# token, revoked token, expired token. A reader whose link expired and a
# stranger guessing tokens get the same answer (T-21-15-A).
_RECIPIENT_INVALID_MESSAGE = "this link is not valid for this report"
_REPORT_UNAUTHORIZED_MESSAGE = (
    "this report's connection to daimon has expired; ask the publisher to re-publish it"
)
_CAP_RUNNING_MESSAGE = "daimon is busy answering other questions on this report; try again shortly"
_CAP_OPEN_THREADS_MESSAGE = "close a conversation before starting another one"
_THREAD_BUSY_MESSAGE = "daimon is still answering the previous question in this thread"
_THREAD_NOT_RUNNING_MESSAGE = "this thread is not running"
_CANCEL_MESSAGE = "This question was stopped. It is still billed for what it consumed."


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


class AskRequest(BaseModel):
    message: str
    thread: int | None = None


class AskResponse(BaseModel):
    thread: int


class CancelResponse(BaseModel):
    status: str


class CloseResponse(BaseModel):
    status: str


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
    run_turn: Callable[..., Awaitable[None]] = turns.run_turn,
) -> APIRouter:
    """Build the reader-facing router.

    ``conn_factory`` is called exactly once, here, to obtain the single
    SQLite connection every route in this router shares — mirroring the
    one-connection design ``reports_store``/``threads_store`` are built
    around; production passes a factory that calls
    ``reports_store.connect(settings.data_dir)``, tests pass a lambda
    returning an already-open connection over a ``tmp_path`` file. ``now``
    and ``run_turn`` are injected (never a global clock, never a hardcoded
    turn driver) so tests can control both without driving a real seam turn
    end to end — ``ask`` schedules ``run_turn`` as a background task and
    never awaits it itself.
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

    # -- Task 2: ask, cancel, close ---------------------------------------

    @router.post("/api/{slug}/ask")
    async def ask(  # pyright: ignore[reportUnusedFunction]
        slug: str,
        body: AskRequest,
        recipient: Annotated[RecipientRow, Depends(resolve_recipient)],
        background_tasks: BackgroundTasks,
    ) -> AskResponse:
        report = reports_store.load_report(conn, slug=slug)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        if report.seam_status == "unauthorized":
            raise HTTPException(status.HTTP_409_CONFLICT, detail=_REPORT_UNAUTHORIZED_MESSAGE)

        # Both caps, before any spend, seam call, or run_turn schedule.
        if (
            threads_store.count_running_turns(conn, slug=slug)
            >= settings.max_running_turns_per_report
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=_CAP_RUNNING_MESSAGE)
        is_new_thread = body.thread is None
        if (
            is_new_thread
            and threads_store.count_open_threads(conn, slug=slug, recipient_token=recipient.token)
            >= settings.max_open_threads_per_recipient
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=_CAP_OPEN_THREADS_MESSAGE)

        if body.thread is not None:
            thread = threads_store.load_thread(
                conn, thread_id=body.thread, slug=slug, recipient_token=recipient.token
            )
            if thread is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND)
        else:
            thread = threads_store.create_thread(
                conn,
                slug=slug,
                recipient_token=recipient.token,
                title=body.message[:60],
                now=now(),
            )

        began = threads_store.begin_turn(
            conn,
            thread_id=thread.id,
            reserved_usd=settings.reserve_usd,
            deadline_at=now() + timedelta(seconds=settings.turn_timeout_seconds),
            now=now(),
        )
        if not began:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=_THREAD_BUSY_MESSAGE)
        thread = threads_store.load_thread(
            conn, thread_id=thread.id, slug=slug, recipient_token=recipient.token
        )
        assert thread is not None  # this exact row just transitioned to running, above

        upload_token = secrets.token_urlsafe(24)
        reports_store.create_upload_token(
            conn, token=upload_token, slug=slug, thread_id=thread.id, now=now()
        )
        upload_url = f"{settings.public_url_base}upload/{upload_token}"
        # The reader persona lives in the reader agent's own prompt and skill;
        # this preamble only orients the agent to the viewer and the upload
        # command, so the two never drift out of sync.
        if thread.handle is None:
            preamble = (
                f"You are answering {recipient.name}, who is looking at this report in a "
                "viewer beside this chat. If you produce a revised PDF this turn, upload it "
                f"with exactly: curl -sS -T <file.pdf> '{upload_url}'\n\n"
            )
        else:
            preamble = (
                "(If you produce a revised PDF this turn, upload it with: "
                f"curl -sS -T <file.pdf> '{upload_url}')\n\n"
            )

        threads_store.add_message(
            conn,
            thread_id=thread.id,
            role="user",
            text=body.message,
            now=now(),
            bundle_sha256=None,
            pdf_revision=None,
        )
        background_tasks.add_task(
            run_turn,
            conn=conn,
            seam=seam,
            settings=settings,
            thread=thread,
            message=preamble + body.message,
            now=now,
        )
        return AskResponse(thread=thread.id)

    @router.post("/api/{slug}/threads/{thread_id}/cancel")
    async def cancel(  # pyright: ignore[reportUnusedFunction]
        slug: str, thread_id: int, recipient: Annotated[RecipientRow, Depends(resolve_recipient)]
    ) -> CancelResponse:
        thread = threads_store.load_thread(
            conn, thread_id=thread_id, slug=slug, recipient_token=recipient.token
        )
        if thread is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        if thread.status != "running" or thread.handle is None:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=_THREAD_NOT_RUNNING_MESSAGE)
        report = reports_store.load_report(conn, slug=slug)
        if report is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        result = await seam.cancel_turn(token=report.seam_token, handle=thread.handle)
        threads_store.add_message(
            conn,
            thread_id=thread.id,
            role="system",
            text=_CANCEL_MESSAGE,
            now=now(),
            bundle_sha256=None,
            pdf_revision=None,
        )
        return CancelResponse(status=result.status)

    @router.post("/api/{slug}/threads/{thread_id}/close")
    async def close(  # pyright: ignore[reportUnusedFunction]
        slug: str, thread_id: int, recipient: Annotated[RecipientRow, Depends(resolve_recipient)]
    ) -> CloseResponse:
        thread = threads_store.load_thread(
            conn, thread_id=thread_id, slug=slug, recipient_token=recipient.token
        )
        if thread is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        if thread.archived_at is not None:
            return CloseResponse(status="archived")  # idempotent: already closed
        report = reports_store.load_report(conn, slug=slug)
        if report is not None and thread.handle is not None:
            await seam.archive_session(token=report.seam_token, handle=thread.handle)
        threads_store.archive_thread(conn, thread_id=thread.id, now=now())
        return CloseResponse(status="archived")

    return router

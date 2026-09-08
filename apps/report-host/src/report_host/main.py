"""FastAPI app factory for report-host.

``create_app(settings)`` wires:
  - the single SQLite connection (``reports_store.connect``, applying both
    ``reports_store``'s and ``threads_store``'s schema, once, at construction)
  - a ``SeamClient`` over one ``httpx.AsyncClient`` owned by the app
  - the reader router (which also mounts the viewer's static assets — see
    ``routes.build_reader_router``), the admin router and the uploads router
  - ``GET /health``, requiring no credential — the compose/reverse-proxy probe
  - a ``lifespan`` that, on entry, resumes every thread the host left running
    across a restart and starts one background task looping the deadline-cancel,
    idle-archive and recipient-prune sweeps; on exit, flips the shutting-down
    flag every ``run_turn`` call reads (so an in-flight turn leaves its thread
    ``running`` rather than telling the reader the answer was lost), cancels
    the sweep task, and closes the HTTP client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from report_host import reports_store, turns
from report_host.admin import build_admin_router
from report_host.config import Settings
from report_host.mcp_client import SeamClient
from report_host.routes import build_reader_router
from report_host.sweeps import (
    archive_idle_threads,
    cancel_expired_turns,
    prune_expired_recipients,
    resume_running_threads,
)
from report_host.threads_store import ThreadRow
from report_host.uploads import build_uploads_router

_log = logging.getLogger(__name__)

# Housekeeping cadence: idle threads and expired deadlines are hours-scale
# concerns, so a five-minute tick is frequent enough without hammering the
# seam or the local database on every request cycle.
_SWEEP_INTERVAL_SECONDS = 300.0


def create_app(settings: Settings) -> FastAPI:
    conn = reports_store.connect(settings.data_dir)
    http_client = httpx.AsyncClient()
    seam = SeamClient(
        mcp_url=str(settings.mcp_url),
        http_client=http_client,
        max_bundle_bytes=settings.max_bundle_bytes,
    )

    def now() -> datetime:
        return datetime.now(UTC)

    # A plain closure flag, not a module-level global (architecture rule 3):
    # `create_app` is the one place that owns it, and every `run_turn` call
    # this app schedules — from `ask`'s background task and from the resume
    # sweep alike — reads the exact same flag through `_run_turn` below. This
    # is what makes a turn interrupted by shutdown leave its thread `running`
    # (so the next boot's resume re-attaches to it) rather than being told,
    # incorrectly, that the answer was lost (T-21-18-A).
    shutting_down = False

    def is_shutting_down() -> bool:
        return shutting_down

    async def _run_turn(
        *,
        conn: sqlite3.Connection,
        seam: SeamClient,
        settings: Settings,
        thread: ThreadRow,
        message: str | None,
        now: Callable[[], datetime],
    ) -> None:
        await turns.run_turn(
            conn=conn,
            seam=seam,
            settings=settings,
            thread=thread,
            message=message,
            now=now,
            shutting_down=is_shutting_down,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
        nonlocal shutting_down
        resumed = await resume_running_threads(
            conn=conn,
            seam=seam,
            settings=settings,
            now=now,
            schedule=asyncio.create_task,
            run_turn=_run_turn,
        )
        if resumed:
            _log.info("resumed %d thread(s) still running on the seam", resumed)
        sweep_task = asyncio.create_task(
            _sweep_loop(conn=conn, seam=seam, settings=settings, now=now)
        )
        try:
            yield
        finally:
            shutting_down = True
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task
            await http_client.aclose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(
        build_reader_router(
            settings=settings, conn_factory=lambda: conn, seam=seam, now=now, run_turn=_run_turn
        )
    )
    app.include_router(build_admin_router(settings=settings, conn_factory=lambda: conn, now=now))
    app.include_router(
        build_uploads_router(settings=settings, conn_factory=lambda: conn, seam=seam)
    )

    @app.get("/health")
    def health() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": True}

    return app


async def _sweep_loop(
    *, conn: sqlite3.Connection, seam: SeamClient, settings: Settings, now: Callable[[], datetime]
) -> None:
    """Background task: every ``_SWEEP_INTERVAL_SECONDS``, cancel expired
    turns, archive idle threads, and prune expired recipients.

    One sweep failing must not stop the next tick (T-21-18-G) — the
    individual sweep functions already guard per item; this loop's own
    ``except Exception`` is the outer belt for a failure in the loop
    plumbing itself (e.g. a database file gone missing).
    """
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        try:
            await cancel_expired_turns(conn=conn, seam=seam, settings=settings, now=now)
            await archive_idle_threads(conn=conn, seam=seam, settings=settings, now=now)
            prune_expired_recipients(conn=conn, now=now)
        except Exception:
            _log.exception("sweep iteration failed; will retry next cycle")

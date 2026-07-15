"""FastAPI app factory for notebook-host.

`create_app(settings)` wires:
  - AdminState (injected settings + lifecycle.spawn_marimo as default spawner)
  - Admin router (PUT/DELETE/list/sweep/health)
  - Lifespan context that starts the background sweep task and kills all
    subprocesses on shutdown
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException

from notebook_host.admin import AdminState, create_admin_router
from notebook_host.blogs_store import load_blogs
from notebook_host.config import Settings
from notebook_host.lifecycle import (
    NotebookProcess,
    ValidationResult,
    allocate_port,
    has_inline_script_metadata,
    kill,
    should_reap,
    spawn_marimo,
    validate_notebook,
    wait_for_port,
)
from notebook_host.pids_store import reap_orphans
from notebook_host.proxy import create_proxy_router

_log = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    processes: dict[str, NotebookProcess] = {}

    # A notebook that declares PEP 723 deps is served from an isolated uv venv
    # (--sandbox); one without keeps the host's baked stack. Validation must use
    # the same mode as the spawn, so both read the on-disk source (already
    # written by the PUT handler) through the same detector.
    def _spawner(
        slug: str, file_path: Path, port: int, *, mode: Literal["edit", "run"] = "edit"
    ) -> subprocess.Popen[bytes]:
        return spawn_marimo(
            slug,
            file_path,
            port,
            mode=mode,
            sandbox=has_inline_script_metadata(file_path.read_text(encoding="utf-8")),
            rlimit_as_bytes=settings.marimo_rlimit_as_bytes or None,
            rlimit_cpu_seconds=settings.marimo_rlimit_cpu_seconds or None,
        )

    def _validator(slug: str, file_path: Path) -> ValidationResult:
        return validate_notebook(
            slug,
            file_path,
            timeout_s=settings.validation_timeout_seconds,
            sandbox=has_inline_script_metadata(file_path.read_text(encoding="utf-8")),
            rlimit_as_bytes=settings.marimo_rlimit_as_bytes or None,
            rlimit_cpu_seconds=settings.marimo_rlimit_cpu_seconds or None,
        )

    state = AdminState(
        settings=settings,
        processes=processes,
        spawner=_spawner,
        validator=_validator if settings.validate_on_publish else None,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        # Reap any marimo subprocesses left behind by a previous host crash
        # before we accept any PUTs. The previous host's pids.json is the
        # only record of what's still running with start_new_session=True.
        reaped = reap_orphans(settings.resolved_pids_file)
        if reaped:
            _log.warning(
                "reaped %d orphaned marimo subprocess(es) from previous host: %s",
                len(reaped),
                [r.slug for r in reaped],
            )
        respawned = await _respawn_registered_blogs(state)
        if respawned:
            _log.info("respawned %d persistent blog(s): %s", len(respawned), respawned)
        sweep_task = asyncio.create_task(_sweep_loop(state))
        try:
            yield
        finally:
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task
            for np in list(processes.values()):
                kill(np)
            processes.clear()
            state.snapshot_pids()

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_admin_router(state))
    app.include_router(create_proxy_router(state))
    app.state.admin_state = state
    return app


async def _spawn_blog_process(state: AdminState, slug: str) -> bool:
    """Spawn a registered blog in run mode and track it. Returns True on success.

    Source must already exist on the persistent volume at ``data_dir/<slug>.py``.
    Used by boot respawn and the sweep's self-heal. A failure (missing source,
    pool exhausted, spawn timeout) logs and returns False — callers must not let
    one bad blog abort their loop. The caller is responsible for popping any
    stale entry for ``slug`` before calling (so its port frees up for reuse).
    """
    path = state.settings.data_dir / f"{slug}.py"
    if not path.exists():
        _log.warning("blog %r has no source at %s; skipping respawn", slug, path)
        return False
    try:
        port = allocate_port(
            state.processes, state.settings.marimo_port_start, state.settings.marimo_port_end
        )
        proc = state.spawner(slug, path, port, mode="run")
    except HTTPException as err:
        _log.warning("blog %r respawn could not start: %s", slug, err.detail)
        return False
    np = state.make_process(slug, port, proc, mode="run")
    state.processes[slug] = np
    ready = await wait_for_port(port, slug, state.settings.spawn_timeout_seconds)
    if not ready:
        kill(np)
        state.processes.pop(slug, None)
        _log.warning("blog %r did not become ready on :%d; will retry next sweep", slug, port)
        return False
    return True


async def _respawn_registered_blogs(state: AdminState) -> list[str]:
    """At boot, respawn every blog in the registry. Returns the slugs respawned.

    Called from the lifespan after orphan reaping. One blog failing to respawn
    never aborts the others.
    """
    respawned: list[str] = []
    for slug in load_blogs(state.settings.resolved_blogs_file):
        if await _spawn_blog_process(state, slug):
            respawned.append(slug)
    if respawned:
        state.snapshot_pids()
    return respawned


async def _sweep_once(state: AdminState) -> bool:
    """One sweep pass. Returns True if it mutated state.processes.

    Blogs (run mode): never age-reaped; a dead one is respawned from disk
    (self-heal). Ephemeral notebooks (edit mode): reaped + source-deleted when
    should_reap is true.
    """
    mutated = False
    for slug in list(state.processes.keys()):
        np = state.processes[slug]
        if np.mode == "run":
            if not np.is_alive():
                _log.warning("blog %r kernel died; respawning from disk", slug)
                state.processes.pop(slug, None)
                await _spawn_blog_process(state, slug)
                mutated = True
            continue
        if not should_reap(np, state.settings.subprocess_ttl_seconds):
            continue
        kill(np)
        state.processes.pop(slug, None)
        (state.settings.data_dir / f"{slug}.py").unlink(missing_ok=True)
        mutated = True
    # Self-heal any registered blog that isn't currently running. This covers a
    # respawn that failed earlier (popped from state.processes but still in the
    # registry) — without this, such a blog would stay down until the next host
    # boot. Boot does the same via _respawn_registered_blogs; doing it every
    # sweep makes "retry next sweep" actually true.
    for slug in load_blogs(state.settings.resolved_blogs_file):
        if slug not in state.processes and await _spawn_blog_process(state, slug):
            mutated = True
    return mutated


async def _sweep_loop(state: AdminState) -> None:
    """Background task: sweep every sweep_interval_seconds (reap notebooks, heal blogs)."""
    while True:
        await asyncio.sleep(state.settings.sweep_interval_seconds)
        try:
            mutated = await _sweep_once(state)
            if mutated:
                state.snapshot_pids()
        except Exception:
            _log.exception("sweep iteration failed; will retry next cycle")

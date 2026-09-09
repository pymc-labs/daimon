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
from typing import Literal

from fastapi import FastAPI, HTTPException

from notebook_host.admin import AdminState, create_admin_router
from notebook_host.blogs_store import load_blogs
from notebook_host.config import Settings
from notebook_host.jail import (
    JailUnavailableError,
    SlugPaths,
    UidPoolExhaustedError,
    can_apply_jail,
    clear_slug_uv_cache,
    ensure_slug_jail,
    remove_slug_tree,
    resolve_jail_uid,
)
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
from notebook_host.migration import migrate_flat_layout
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
        slug: str,
        paths: SlugPaths,
        port: int,
        *,
        mode: Literal["edit", "run"] = "edit",
        jail_uid: int | None = None,
    ) -> subprocess.Popen[bytes]:
        return spawn_marimo(
            slug,
            paths,
            port,
            mode=mode,
            sandbox=has_inline_script_metadata(paths.notebook.read_text(encoding="utf-8")),
            rlimit_as_bytes=settings.marimo_rlimit_as_bytes or None,
            rlimit_cpu_seconds=settings.marimo_rlimit_cpu_seconds or None,
            jail_uid=jail_uid,
        )

    def _validator(slug: str, paths: SlugPaths, *, jail_uid: int | None = None) -> ValidationResult:
        return validate_notebook(
            slug,
            paths,
            timeout_s=settings.validation_timeout_seconds,
            sandbox=has_inline_script_metadata(paths.notebook.read_text(encoding="utf-8")),
            rlimit_as_bytes=settings.marimo_rlimit_as_bytes or None,
            rlimit_cpu_seconds=settings.marimo_rlimit_cpu_seconds or None,
            jail_uid=jail_uid,
        )

    state = AdminState(
        settings=settings,
        processes=processes,
        spawner=_spawner,
        validator=_validator if settings.validate_on_publish else None,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:  # pyright: ignore[reportUnusedFunction]
        # Fail-closed boot gate (D-05): a host that cannot apply the jail must
        # not come up at all. Refusing per-request instead would leave an
        # apparently healthy host answering nothing but 503s — a configuration
        # refusal that reads as an outage of unknown cause.
        if not can_apply_jail() and not settings.allow_unjailed_spawn:
            raise JailUnavailableError(
                "cannot apply the notebook jail on this host (not root, or this "
                "platform cannot change process identity); refusing to boot. Set "
                "DAIMON_NOTEBOOK__ALLOW_UNJAILED_SPAWN=true to explicitly opt out "
                "on a dev host."
            )
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        # Move any pre-jail flat layout onto the nested one before anything
        # else touches data_dir — reap_orphans and the blog respawn below
        # both assume data_dir/<slug>/notebook.py already exists. Not
        # wrapped in try/except: a migration failure must abort the boot
        # rather than come up serving a half-isolated data_dir.
        migrated = migrate_flat_layout(
            settings.data_dir,
            uids_file=settings.resolved_uids_file,
            uid_start=settings.jail_uid_start,
            uid_end=settings.jail_uid_end,
            allow_unjailed=settings.allow_unjailed_spawn,
            registry_files=(
                settings.resolved_blogs_file,
                settings.resolved_pids_file,
                settings.resolved_consumed_file,
                settings.resolved_uids_file,
            ),
        )
        if migrated:
            _log.info(
                "migrated %d legacy blog(s) to the nested layout: %s", len(migrated), migrated
            )
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

    Source must already exist on the persistent volume at
    ``data_dir/<slug>/notebook.py``. Used by boot respawn and the sweep's
    self-heal. A failure (missing source, pool exhausted, spawn timeout, or the
    jail being unavailable) logs and returns False — callers must not let one
    bad blog abort their loop. A blog that cannot be jailed stays down; it must
    not respawn unisolated. The caller is responsible for popping any stale
    entry for ``slug`` before calling (so its port frees up for reuse).
    """
    try:
        uid = resolve_jail_uid(
            state.settings.resolved_uids_file,
            slug,
            start=state.settings.jail_uid_start,
            end=state.settings.jail_uid_end,
            allow_unjailed=state.settings.allow_unjailed_spawn,
        )
    except (JailUnavailableError, UidPoolExhaustedError) as err:
        _log.error("blog %r respawn could not be jailed: %s", slug, err)
        return False
    paths = ensure_slug_jail(state.settings.data_dir, slug, uid=uid)
    if not paths.notebook.exists():
        _log.warning("blog %r has no source at %s; skipping respawn", slug, paths.notebook)
        return False
    try:
        port = allocate_port(
            state.processes, state.settings.marimo_port_start, state.settings.marimo_port_end
        )
        proc = state.spawner(slug, paths, port, mode="run", jail_uid=uid)
    except HTTPException as err:
        _log.warning("blog %r respawn could not start: %s", slug, err.detail)
        return False
    np = state.make_process(slug, port, proc, mode="run")
    state.processes[slug] = np
    ready = await wait_for_port(port, slug, state.settings.spawn_timeout_seconds)
    if not ready:
        kill(np)
        state.processes.pop(slug, None)
        clear_slug_uv_cache(state.settings.data_dir, slug)
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
    (self-heal). Ephemeral notebooks (edit mode): reaped + their whole slug
    tree (source, attachments, workspace, log) removed when should_reap is
    true — the background sweep and the two delete endpoints share identical
    cleanup semantics by construction.
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
        remove_slug_tree(state.settings.data_dir, slug, uids_file=state.settings.resolved_uids_file)
        mutated = True
    # Self-heal any registered blog that isn't currently running. This covers a
    # respawn that failed earlier (popped from state.processes but still in the
    # registry) — without this, such a blog would stay down until the next host
    # boot. Boot does the same via _respawn_registered_blogs; doing it every
    # sweep makes "retry next sweep" actually true.
    #
    # The outer load_blogs is a cheap candidate scan taken outside any lock, so
    # it can race delete_blog: a slug it names may already be mid-delete (or
    # finish deleting) before we get here. Re-reading load_blogs a second time
    # *inside* the same per-slug lock delete_blog holds is what closes that
    # window — a candidate that was unregistered under the lock is dropped
    # instead of respawned. The double read looks redundant; it is not.
    for slug in load_blogs(state.settings.resolved_blogs_file):
        async with state.lock_for(slug):
            if slug not in load_blogs(state.settings.resolved_blogs_file):
                continue
            if slug in state.processes:
                continue
            if await _spawn_blog_process(state, slug):
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

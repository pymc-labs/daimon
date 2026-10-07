"""Start registered notebooks on a visit, stop them when idle, delete them on expiry.

A read-only notebook can be kept for up to a year, but the host has one port
per running notebook and a few dozen ports. So a registered notebook (one in
``blogs_store``) does not hold a process for its whole lifetime: the proxy
calls ``ensure_running`` when a visit finds it stopped, and ``sweep_once``
stops it again once it has gone unvisited for ``warm_window_seconds``. Its
source, attachments and access token stay on disk, so its link works across
stops and host restarts until the record expires.

The editor is not registered: it starts at upload, is reaped with its files
after ``subprocess_ttl_seconds``, and is never restarted, because a link that
runs code for whoever holds it should not outlive the session it was made for.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import HTTPException, status

from notebook_host.blogs_store import is_expired, load_blogs, register_blog, unregister_blog
from notebook_host.jail import (
    JailUnavailableError,
    UidPoolExhaustedError,
    UidStillInUseError,
    ensure_slug_jail,
    kill_uid_processes,
    remove_slug_tree,
    resolve_jail_uid,
)
from notebook_host.lifecycle import NotebookProcess, allocate_port, kill, should_reap, wait_for_port

if TYPE_CHECKING:
    from notebook_host.admin import AdminState

_log = logging.getLogger(__name__)

# Seconds a browser is told to wait before retrying a notebook that could not
# be started (pool full of open sessions, or a slow start).
_RETRY_AFTER_SECONDS = 10


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail,
        headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
    )


def _pool_full(state: AdminState) -> bool:
    capacity = state.settings.marimo_port_end - state.settings.marimo_port_start + 1
    return len(state.processes) >= capacity


async def _make_room(state: AdminState) -> None:
    """Stop the least recently visited registered notebook nobody has open.

    The editor is never a candidate: it is not restarted on a visit, so
    stopping it would lose it. Raises 503 when every running notebook is an
    editor or has a websocket open.
    """
    candidates = [np for np in state.processes.values() if np.registered and np.open_sockets == 0]
    if not candidates:
        raise _unavailable("every notebook on this host is in use; try again shortly")
    victim = min(candidates, key=lambda np: np.last_active)
    _log.info("stopping idle notebook %r to start another", victim.slug)
    state.processes.pop(victim.slug, None)
    await asyncio.to_thread(kill, victim)


async def _start(state: AdminState, slug: str) -> NotebookProcess:
    """Start a registered notebook from disk and wait until it serves.

    The caller holds ``state.lock_for(slug)`` and has checked the record is
    live. Raises 503 when it cannot be jailed, has no source, or does not get
    ready in ``spawn_timeout_seconds``. A notebook that cannot be jailed stays
    down; it must not start unisolated.
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
        _log.error("notebook %r could not be jailed: %s", slug, err)
        raise _unavailable(f"notebook isolation unavailable: {err}") from err
    paths = ensure_slug_jail(state.settings.data_dir, slug, uid=uid)
    if not paths.notebook.exists():
        _log.warning("notebook %r has no source at %s; not starting it", slug, paths.notebook)
        raise _unavailable(f"notebook {slug!r} has no source on this host")
    if uid is not None:
        # Whatever the last run of this uid left behind (a cell's detached
        # child escapes kill's process group) must not live alongside the new
        # process and its token.
        try:
            kill_uid_processes(uid)
        except UidStillInUseError as err:
            _log.error("notebook %r uid still in use, not starting it: %s", slug, err)
            raise _unavailable(f"notebook isolation unavailable: {err}") from err
    access_token = state.access_token_for(slug, "run")
    record = load_blogs(state.settings.resolved_blogs_file).get(slug)
    if record is not None and record.access_token != access_token:
        # Registered before tokens existed: save the one it is about to be
        # served under, so its link survives the next stop.
        register_blog(
            state.settings.resolved_blogs_file,
            record.model_copy(update={"access_token": access_token}),
        )
    if _pool_full(state):
        await _make_room(state)
    port = allocate_port(
        state.processes, state.settings.marimo_port_start, state.settings.marimo_port_end
    )
    proc = state.spawner(slug, paths, port, access_token=access_token, mode="run", jail_uid=uid)
    np = state.make_process(
        slug, port, proc, access_token=access_token, mode="run", registered=True
    )
    state.processes[slug] = np
    state.snapshot_pids()
    ready = await wait_for_port(
        port, slug, state.settings.spawn_timeout_seconds, access_token=access_token
    )
    if not ready:
        state.processes.pop(slug, None)
        await asyncio.to_thread(kill, np)
        state.snapshot_pids()
        _log.warning("notebook %r did not become ready on :%d", slug, port)
        raise _unavailable(f"notebook {slug!r} is still starting; try again shortly")
    return np


async def ensure_running(state: AdminState, slug: str, *, now: float) -> NotebookProcess | None:
    """The slug's live process, starting it if it is registered and unexpired.

    None means there is nothing to serve: never published, deleted, expired,
    or an editor that has died. Raises 503 when a registered notebook could
    not be started.
    """
    np = state.processes.get(slug)
    if np is not None and np.is_alive():
        return np
    async with state.lock_for(slug):
        np = state.processes.get(slug)
        if np is not None and np.is_alive():
            return np
        record = load_blogs(state.settings.resolved_blogs_file).get(slug)
        if record is None or is_expired(record, now=now):
            return None
        if np is not None:
            state.processes.pop(slug, None)
        return await _start(state, slug)


@dataclass
class SweepResult:
    # Deleted with their files: expired notebooks and editors.
    reaped: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    # Process stopped, files and link kept for the next visit.
    stopped: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    @property
    def mutated(self) -> bool:
        return bool(self.reaped or self.stopped)


async def sweep_once(state: AdminState, *, now: float) -> SweepResult:
    """One pass: reap editors, stop idle or dead notebooks, delete expired ones.

    Shared by the background loop and ``POST /admin/sweep``, so the two never
    diverge. Every registered slug is handled under its lock, the same one
    the delete routes and ``ensure_running`` take.
    """
    result = SweepResult()
    warm_window = state.settings.warm_window_seconds
    for slug in list(state.processes):
        np = state.processes.get(slug)
        if np is None or np.registered:
            continue
        if not should_reap(np, state.settings.subprocess_ttl_seconds):
            continue
        reason = "ttl" if np.is_alive() else "dead"
        state.processes.pop(slug, None)
        await asyncio.to_thread(kill, np)
        remove_slug_tree(state.settings.data_dir, slug, uids_file=state.settings.resolved_uids_file)
        result.reaped.append({"slug": slug, "reason": reason})

    # The outer reads are a cheap candidate scan taken outside any lock, so a
    # slug they name may be deleted before its lock is free. Each candidate is
    # re-read under the lock, which is what keeps the sweep from acting on a
    # notebook a concurrent delete already removed.
    candidates = set(load_blogs(state.settings.resolved_blogs_file))
    candidates.update(slug for slug, np in state.processes.items() if np.registered)
    for slug in sorted(candidates):
        async with state.lock_for(slug):
            record = load_blogs(state.settings.resolved_blogs_file).get(slug)
            np = state.processes.get(slug)
            if record is not None and is_expired(record, now=now):
                if np is not None:
                    state.processes.pop(slug, None)
                    await asyncio.to_thread(kill, np)
                unregister_blog(state.settings.resolved_blogs_file, slug)
                remove_slug_tree(
                    state.settings.data_dir, slug, uids_file=state.settings.resolved_uids_file
                )
                result.reaped.append({"slug": slug, "reason": "expired"})
                continue
            if np is None or not np.registered:
                continue
            if not np.is_alive():
                state.processes.pop(slug, None)
                result.stopped.append({"slug": slug, "reason": "dead"})
            elif np.is_idle(warm_window, now=now):
                state.processes.pop(slug, None)
                await asyncio.to_thread(kill, np)
                result.stopped.append({"slug": slug, "reason": "idle"})
    return result

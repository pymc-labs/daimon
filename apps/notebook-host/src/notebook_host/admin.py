"""Admin + health routes for notebook-host."""

from __future__ import annotations

import asyncio
import errno
import hmac
import os
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from notebook_host.blogs_store import (
    BlogRecord,
    load_blogs,
    register_blog,
    unregister_blog,
)
from notebook_host.capability import CapabilityClaims, verify_token
from notebook_host.config import Settings
from notebook_host.consumed_store import burn_jti
from notebook_host.jail import (
    JailUnavailableError,
    SlugPaths,
    UidPoolExhaustedError,
    UidStillInUseError,
    ensure_slug_jail,
    get_slug_paths,
    kill_uid_processes,
    remove_path,
    remove_slug_tree,
    remove_uid_files,
    resolve_jail_uid,
    write_file_nofollow,
)
from notebook_host.lazy_spawn import sweep_once
from notebook_host.lifecycle import (
    NotebookProcess,
    ValidationResult,
    allocate_port,
    kill,
    new_access_token,
    origin_label_for,
    safe_attachment_name,
    safe_slug,
    wait_for_port,
)
from notebook_host.pids_store import record_from_process, save_pids


class Spawner(Protocol):
    def __call__(
        self,
        slug: str,
        paths: SlugPaths,
        port: int,
        *,
        access_token: str,
        mode: Literal["edit", "run"] = "edit",
        jail_uid: int | None = None,
    ) -> subprocess.Popen[bytes]: ...


class Validator(Protocol):
    def __call__(
        self, slug: str, paths: SlugPaths, *, jail_uid: int | None = None
    ) -> ValidationResult: ...


@dataclass
class AdminState:
    settings: Settings
    processes: dict[str, NotebookProcess]
    spawner: Spawner
    # Pre-publish execution check. None disables it (the source is served
    # without first confirming its cells run). Wired to a real validator in
    # `create_app` when `settings.validate_on_publish` is set.
    validator: Validator | None = None
    # Serialises concurrent PUTs to the same slug. Without it, two PUTs can
    # interleave kill/allocate/spawn and orphan the loser's subprocess.
    slug_locks: dict[str, asyncio.Lock] = field(default_factory=dict[str, asyncio.Lock])

    def make_process(
        self,
        slug: str,
        port: int,
        proc: subprocess.Popen[bytes],
        *,
        access_token: str,
        mode: Literal["edit", "run"] = "edit",
        registered: bool = False,
    ) -> NotebookProcess:
        public_url_base = self.settings.public_url_base
        if self.settings.origin_base is not None:
            label = origin_label_for(access_token)
            public_url_base = f"{self.settings.origin_scheme}://{label}.{self.settings.origin_base}"
        return NotebookProcess(
            slug=slug,
            port=port,
            process=proc,
            public_host=self.settings.public_host,
            host_port=self.settings.host_port,
            public_url_base=public_url_base,
            mode=mode,
            registered=registered,
            access_token=access_token,
        )

    def access_token_for(self, slug: str, mode: Literal["edit", "run"]) -> str:
        """The slug's existing token, so re-publishing keeps its link; else a new one.

        A token is only reused in the mode it was issued for: switching a slug
        between the read-only app and the editor mints a new one, so holders of
        a read-only link never become editors and an editor link stops working
        once the slug is read-only. A live process's token wins, then a
        registered notebook's persisted one (registered notebooks are always
        read-only). Deleting or reaping a slug drops both, so its next publish
        gets a fresh token and every old link stops working.
        """
        existing = self.processes.get(slug)
        if existing is not None:
            if existing.access_token and existing.mode == mode:
                return existing.access_token
            return new_access_token()
        record = load_blogs(self.settings.resolved_blogs_file).get(slug)
        if record is not None and record.access_token and mode == "run":
            return record.access_token
        return new_access_token()

    def lock_for(self, slug: str) -> asyncio.Lock:
        lock = self.slug_locks.get(slug)
        if lock is None:
            lock = asyncio.Lock()
            self.slug_locks[slug] = lock
        return lock

    def snapshot_pids(self) -> None:
        records = {
            slug: record_from_process(slug, np.process.pid, np.port, np.started_at)
            for slug, np in self.processes.items()
        }
        save_pids(self.settings.resolved_pids_file, records)


class WriteRequest(BaseModel):
    source: str
    # The marimo code editor instead of the read-only app. Refused unless the
    # host's ``allow_editable`` is on.
    editable: bool = False
    # How long to keep a read-only notebook, as the upload token's
    # ``notebook_ttl_seconds``. Ignored for the editor.
    ttl_seconds: int | None = None


def _register_read_only(
    state: AdminState, np: NotebookProcess, *, permanent: bool, ttl_seconds: int | None
) -> float | None:
    """Record a just-published read-only notebook; returns its ``expires_at``.

    ``ttl_seconds`` None or <= 0 means the host's ``subprocess_ttl_seconds``,
    and any value is clamped to ``max_notebook_ttl_seconds``. Re-publishing
    a slug restarts its lifetime from now, except that a blog stays a blog:
    re-uploading one without ``permanent`` must not quietly schedule it for
    deletion. Deleting it is the way back to a scratch notebook.
    """
    path = state.settings.resolved_blogs_file
    existing = load_blogs(path).get(np.slug)
    expires_at: float | None
    if permanent or (existing is not None and existing.expires_at is None):
        expires_at = None
    else:
        ttl = (
            ttl_seconds
            if ttl_seconds is not None and ttl_seconds > 0
            else state.settings.subprocess_ttl_seconds
        )
        # A host TTL <= 0 disables expiry, so with no lifetime asked for the
        # notebook is kept until it is deleted.
        expires_at = (
            np.started_at + min(ttl, state.settings.max_notebook_ttl_seconds) if ttl > 0 else None
        )
    register_blog(
        path,
        BlogRecord(
            slug=np.slug,
            created_at=existing.created_at if existing is not None else np.started_at,
            title=existing.title if existing is not None else None,
            access_token=np.access_token,
            expires_at=expires_at,
        ),
    )
    return expires_at


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat() if epoch is not None else None


def _read_only_response(
    state: AdminState, np: NotebookProcess, expires_at: float | None
) -> dict[str, object]:
    return {
        "slug": np.slug,
        "url": np.url,
        "port": np.port,
        "pid": np.process.pid,
        "size_bytes": get_slug_paths(state.settings.data_dir, np.slug).notebook.stat().st_size,
        "permanent": expires_at is None,
        "expires_at": _iso(expires_at),
    }


def _editor_response(state: AdminState, np: NotebookProcess) -> dict[str, object]:
    ttl = state.settings.subprocess_ttl_seconds
    # ttl <= 0 disables age-based reaping — the editor never expires, so there
    # is no expiry timestamp to report.
    return {
        "slug": np.slug,
        "url": np.url,
        "port": np.port,
        "pid": np.process.pid,
        "size_bytes": get_slug_paths(state.settings.data_dir, np.slug).notebook.stat().st_size,
        "subprocess_ttl_seconds": ttl,
        "expires_at": _iso(np.started_at + ttl if ttl > 0 else None),
    }


def _require_editor_allowed(settings: Settings) -> None:
    if not settings.allow_editable:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "the notebook editor is off on this host (DAIMON_NOTEBOOK__ALLOW_EDITABLE)",
        )


def _admit_tenant(settings: Settings, tenant: str | None) -> None:
    """On a shared-origin public host, accept uploads from listed tenants only.

    Without per-notebook origins (``origin_base``) every notebook shares one
    browser origin, so one tenant's notebook JavaScript could reach another's.
    The operator lists the tenants that may share it (``tenants``); any other
    tenant, or a token that names none, is refused. Local dev hosts and
    per-origin hosts skip this.
    """
    if settings.origin_base is not None or settings.is_local_dev:
        return
    if tenant is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "upload token names no tenant, so DAIMON_NOTEBOOK__TENANTS can't admit it; "
            "upgrade the bot, or set DAIMON_NOTEBOOK__ORIGIN_BASE for per-notebook origins",
        )
    try:
        listed = UUID(tenant) in settings.tenants
    except ValueError:
        listed = False
    if not listed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"tenant {tenant} is not in DAIMON_NOTEBOOK__TENANTS; notebooks on this host "
            "share one browser origin, so it serves only the tenants listed there. Add the "
            "id to share the origin with them, or set DAIMON_NOTEBOOK__ORIGIN_BASE for "
            "per-notebook origins",
        )


def _kill_uid_or_503(uid: int) -> None:
    try:
        kill_uid_processes(uid)
    except UidStillInUseError as err:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"notebook isolation unavailable: {err}"
        ) from err


def _bearer_dep(settings: Settings) -> Callable[[str | None], None]:
    def require(authorization: str | None = Header(default=None)) -> None:
        provided = authorization or ""
        # No short-circuit: comparing every entry avoids a timing leak of list position.
        matched = False
        for secret in settings.admin_secrets:
            expected = f"Bearer {secret.get_secret_value()}"
            if hmac.compare_digest(provided, expected):
                matched = True
        if not matched:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)

    return require


def _atomic_write_bytes(path: Path, content: bytes, *, owner_uid: int | None = None) -> None:
    """Write via tmp + ``os.replace`` so a concurrent reader never sees a torn file.

    When ``owner_uid`` is given, the tmp file is chowned to ``(owner_uid,
    owner_uid)`` before the replace, so the file is never visible at its final
    path with the wrong owner.
    """
    # The data dir is owned by the jail uid, so its code can plant a symlink
    # at the tmp or final name; write_file_nofollow never writes through one.
    write_file_nofollow(path, content, owner_uid=owner_uid)


def _read_published_source(path: Path, *, max_bytes: int) -> bytes | None:
    """Read only a single-link regular source, never a jail-planted link.

    The jail uid owns the file and can grow it, so the read is bounded: a
    source over ``max_bytes`` was never a valid upload and counts as none.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as err:
        if err.errno in (errno.ENOENT, errno.ELOOP):
            return None
        raise
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        content = source.read(max_bytes + 1)
        return content if len(content) <= max_bytes else None


async def _spawn_tracked(
    state: AdminState,
    slug: str,
    source_bytes: bytes,
    *,
    mode: Literal["edit", "run"],
) -> NotebookProcess:
    """Write source, validate, replace any existing process, spawn, wait ready.

    The caller must hold ``state.lock_for(slug)``. Returns the live
    ``NotebookProcess``. Raises ``HTTPException`` 422 (validation), 503 (port
    pool exhausted, or notebook isolation unavailable), or 504 (spawn
    timeout). Shared by the notebook and blog PUT handlers so the two never
    drift.

    A read-only process is registered (started on a visit, stopped when
    idle); the caller records it with ``_register_read_only``. Raises 409 for
    the editor on a blog: its readers hold a read-only link, and
    delete-then-republish is the way to turn it back into a scratch notebook.
    The editor on a read-only scratch notebook replaces it and drops its
    registry record, since the editor is never restarted on a visit.
    """
    record = load_blogs(state.settings.resolved_blogs_file).get(slug)
    if mode == "edit" and record is not None and record.expires_at is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{slug!r} is a published blog; delete it before publishing an editor there",
        )
    # This is the request boundary — the one place admin.py is allowed to
    # catch JailUnavailableError/UidPoolExhaustedError. Both mean the host is
    # refusing to serve rather than failing transiently, mirroring
    # allocate_port's existing 503 for pool exhaustion.
    try:
        uid = resolve_jail_uid(
            state.settings.resolved_uids_file,
            slug,
            start=state.settings.jail_uid_start,
            end=state.settings.jail_uid_end,
            allow_unjailed=state.settings.allow_unjailed_spawn,
        )
    except JailUnavailableError as err:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"notebook isolation unavailable: {err}"
        ) from err
    except UidPoolExhaustedError as err:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"uid pool exhausted: {err}"
        ) from err
    paths = ensure_slug_jail(state.settings.data_dir, slug, uid=uid)
    access_token = state.access_token_for(slug, mode)
    previous = state.processes.get(slug)
    if previous is not None and previous.mode != mode:
        # Switching between the editor and the read-only app: whatever the
        # editor's holder planted as this uid (site-packages in HOME, a
        # poisoned uv cache, files in the workspace or temp dirs) must not run
        # under the new mode, including in the validator below. So the old
        # process goes first, even though a failed validation then leaves
        # nothing serving. Attachments in data/ are content and are kept.
        state.processes.pop(slug, None)
        kill(previous)
        if uid is not None:
            _kill_uid_or_503(uid)
            remove_uid_files(uid)
        for d in (paths.home, paths.workspace, paths.tmp):
            remove_path(d)
        paths = ensure_slug_jail(state.settings.data_dir, slug, uid=uid)
    previous_source = (
        _read_published_source(paths.notebook, max_bytes=state.settings.max_source_bytes)
        if state.validator is not None
        else None
    )
    _atomic_write_bytes(paths.notebook, source_bytes, owner_uid=uid)

    def restore_previous_source() -> None:
        if previous_source is None:
            remove_path(paths.notebook)
        else:
            _atomic_write_bytes(paths.notebook, previous_source, owner_uid=uid)

    # Confirm the cells actually execute before we tear down a same-mode
    # notebook. Runs off the event loop (the marimo export is blocking).
    # On rejection, or if validation itself fails, restore the old source so
    # a later cold start uses it.
    if state.validator is not None:
        try:
            result = await asyncio.to_thread(state.validator, slug, paths, jail_uid=uid)
        except BaseException:
            restore_previous_source()
            raise
        if not result.ok:
            restore_previous_source()
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": "notebook failed validation — cells did not execute",
                    "cell_errors": result.errors,
                },
            )

    existing = state.processes.pop(slug, None)
    if existing is not None:
        kill(existing)
    if uid is not None:
        # Anything the previous run or the validator left behind as this
        # uid (a cell's detached child escapes kill's process group) must not
        # live alongside the new process and its token.
        _kill_uid_or_503(uid)

    port = allocate_port(
        state.processes, state.settings.marimo_port_start, state.settings.marimo_port_end
    )
    proc = state.spawner(slug, paths, port, access_token=access_token, mode=mode, jail_uid=uid)
    np = state.make_process(
        slug, port, proc, access_token=access_token, mode=mode, registered=mode == "run"
    )
    state.processes[slug] = np
    if mode == "edit" and record is not None:
        unregister_blog(state.settings.resolved_blogs_file, slug)

    state.snapshot_pids()
    ready = await wait_for_port(
        port, slug, state.settings.spawn_timeout_seconds, access_token=access_token
    )
    if not ready:
        kill(np)
        state.processes.pop(slug, None)
        state.snapshot_pids()
        timeout_s = state.settings.spawn_timeout_seconds
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            f"marimo subprocess on :{port} did not become ready within {timeout_s}s",
        )
    return np


def create_admin_router(state: AdminState) -> APIRouter:
    router = APIRouter()
    require_admin = _bearer_dep(state.settings)

    @router.get("/health")
    def health() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        alive = sum(1 for p in state.processes.values() if p.is_alive())
        return {
            "status": "ok",
            "data_dir": str(state.settings.data_dir),
            "active_notebooks": alive,
            "tracked_notebooks": len(state.processes),
            "port_pool": {
                "start": state.settings.marimo_port_start,
                "end": state.settings.marimo_port_end,
                "capacity": state.settings.marimo_port_end - state.settings.marimo_port_start + 1,
                "in_use": len(state.processes),
            },
            "subprocess_ttl_seconds": state.settings.subprocess_ttl_seconds,
            "warm_window_seconds": state.settings.warm_window_seconds,
            "registered_notebooks": len(load_blogs(state.settings.resolved_blogs_file)),
        }

    @router.put("/admin/notebooks/{slug}", dependencies=[Depends(require_admin)])
    async def put_notebook(slug: str, body: WriteRequest) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        slug = safe_slug(slug)
        source_bytes = body.source.encode("utf-8")
        if len(source_bytes) > state.settings.max_source_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"source exceeds max_source_bytes ({len(source_bytes)} > "
                f"{state.settings.max_source_bytes})",
            )
        async with state.lock_for(slug):
            if body.editable:
                _require_editor_allowed(state.settings)
            mode: Literal["edit", "run"] = "edit" if body.editable else "run"
            np = await _spawn_tracked(state, slug, source_bytes, mode=mode)
            if mode == "edit":
                return _editor_response(state, np)
            expires_at = _register_read_only(
                state, np, permanent=False, ttl_seconds=body.ttl_seconds
            )
            return _read_only_response(state, np, expires_at)

    @router.put(
        "/admin/notebooks/{slug}/data/{name}",
        dependencies=[Depends(require_admin)],
    )
    async def put_notebook_data(  # pyright: ignore[reportUnusedFunction]
        slug: str, name: str, request: Request
    ) -> dict[str, object]:
        slug = safe_slug(slug)
        name = safe_attachment_name(name)
        body = await request.body()
        if len(body) > state.settings.max_attachment_bytes_ceiling:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"attachment exceeds max_attachment_bytes_ceiling "
                f"({len(body)} > {state.settings.max_attachment_bytes_ceiling})",
            )
        async with state.lock_for(slug):
            # ensure the tree exists even when an attachment arrives before
            # the first publish, with the same 0700 mode a publish would set.
            try:
                uid = resolve_jail_uid(
                    state.settings.resolved_uids_file,
                    slug,
                    start=state.settings.jail_uid_start,
                    end=state.settings.jail_uid_end,
                    allow_unjailed=state.settings.allow_unjailed_spawn,
                )
            except JailUnavailableError as err:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, f"notebook isolation unavailable: {err}"
                ) from err
            except UidPoolExhaustedError as err:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, f"uid pool exhausted: {err}"
                ) from err
            paths = ensure_slug_jail(state.settings.data_dir, slug, uid=uid)
            final_path = paths.data / name
            _atomic_write_bytes(final_path, body, owner_uid=uid)
            return {
                "slug": slug,
                "name": name,
                "size_bytes": len(body),
                "path": f"data/{name}",
            }

    @router.delete("/admin/notebooks/{slug}", dependencies=[Depends(require_admin)])
    async def delete_notebook(slug: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """Delete either kind of notebook, and say whether one was there.

        Reports ``deleted`` rather than answering 204-for-everything. A caller
        that cannot tell "I removed it" from "there was nothing by that name"
        will report a typo'd or wrong-namespace slug as a successful delete —
        which is exactly how a broken delete stayed invisible in production.

        Unregisters any blog record under the same lock, so this one route
        handles run-mode blogs too: leaving the record behind would have the
        sweep's self-heal respawn the blog we just killed.
        """
        slug = safe_slug(slug)
        async with state.lock_for(slug):
            was_blog = unregister_blog(state.settings.resolved_blogs_file, slug)
            had_tree = get_slug_paths(state.settings.data_dir, slug).root.exists()
            np = state.processes.pop(slug, None)
            if np is not None:
                # kill blocks up to 5s (SIGTERM wait); must not block the
                # event loop now that the handler is async.
                await asyncio.to_thread(kill, np)
            remove_slug_tree(
                state.settings.data_dir, slug, uids_file=state.settings.resolved_uids_file
            )
            state.snapshot_pids()
            return {"slug": slug, "deleted": np is not None or was_blog or had_tree}

    @router.get("/admin/notebooks", dependencies=[Depends(require_admin)])
    def list_notebooks() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return {
            "notebooks": [
                {
                    "slug": np.slug,
                    "url": np.url,
                    "port": np.port,
                    "pid": np.process.pid,
                    "alive": np.is_alive(),
                    "age_s": round(np.age_s, 2),
                    "registered": np.registered,
                }
                for np in sorted(state.processes.values(), key=lambda p: p.slug)
            ]
        }

    @router.put("/admin/blogs/{slug}", dependencies=[Depends(require_admin)])
    async def put_blog(slug: str, body: WriteRequest) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        slug = safe_slug(slug)
        source_bytes = body.source.encode("utf-8")
        if len(source_bytes) > state.settings.max_source_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"source exceeds max_source_bytes ({len(source_bytes)} > "
                f"{state.settings.max_source_bytes})",
            )
        async with state.lock_for(slug):
            np = await _spawn_tracked(state, slug, source_bytes, mode="run")
            expires_at = _register_read_only(state, np, permanent=True, ttl_seconds=None)
            return _read_only_response(state, np, expires_at)

    @router.get("/admin/blogs", dependencies=[Depends(require_admin)])
    def list_blogs() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """Every registered read-only notebook, running or not.

        The route keeps its blog-era name so older bots keep working. Scratch
        notebooks are told apart by ``expires_at`` (None for a blog). ``url``
        is None while a notebook is stopped; its link still works and starts
        it.
        """
        records = load_blogs(state.settings.resolved_blogs_file)
        blogs: list[dict[str, object]] = []
        for slug, rec in sorted(records.items()):
            np = state.processes.get(slug)
            blogs.append(
                {
                    "slug": slug,
                    "created_at": rec.created_at,
                    "title": rec.title,
                    "expires_at": _iso(rec.expires_at),
                    "url": np.url if np is not None else None,
                    "port": np.port if np is not None else None,
                    "pid": np.process.pid if np is not None else None,
                    "alive": np.is_alive() if np is not None else False,
                }
            )
        return {"blogs": blogs}

    @router.delete(
        "/admin/blogs/{slug}",
        dependencies=[Depends(require_admin)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_blog(slug: str) -> None:  # pyright: ignore[reportUnusedFunction]
        slug = safe_slug(slug)
        async with state.lock_for(slug):
            # Unregister first, while the lock is held, so the registry stops
            # naming this slug before anything blocking (kill) starts. This is
            # what lets the sweep's self-heal, re-reading the registry under
            # the same lock, see the slug is already gone rather than racing
            # to respawn it.
            unregister_blog(state.settings.resolved_blogs_file, slug)
            np = state.processes.pop(slug, None)
            if np is not None:
                await asyncio.to_thread(kill, np)
            remove_slug_tree(
                state.settings.data_dir, slug, uids_file=state.settings.resolved_uids_file
            )
            state.snapshot_pids()

    @router.post("/admin/sweep", dependencies=[Depends(require_admin)])
    async def sweep() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        result = await sweep_once(state, now=time.time())
        if result.mutated:
            state.snapshot_pids()
        return {
            "reaped": result.reaped,
            "stopped": result.stopped,
            "subprocess_ttl_seconds": state.settings.subprocess_ttl_seconds,
            "warm_window_seconds": state.settings.warm_window_seconds,
        }

    @router.put("/upload/{token}")
    async def upload(token: str, request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        # Public route — authed by the capability token, NOT the admin bearer.
        secrets_list = [s.get_secret_value() for s in state.settings.admin_secrets]
        claims: CapabilityClaims = verify_token(secrets_list, token, now=datetime.now(UTC))
        if claims.op == "notebook_edit":
            # The bot gates this too; the host refuses on its own so a
            # capability minted elsewhere can't turn the editor on.
            _require_editor_allowed(state.settings)
        _admit_tenant(state.settings, claims.tenant)
        # Burn before reading the body: burn_jti's check-and-write is one call
        # with no await in between, so two concurrent replays of one token
        # cannot both observe it as unused. Burning here (rather than after a
        # successful write) means a token whose upload later fails a size
        # check is not reusable — that is what single-use means; the
        # alternative reopens the replay window this closes.
        burned = burn_jti(
            state.settings.resolved_consumed_file,
            claims.jti,
            exp=claims.exp,
            now=int(datetime.now(UTC).timestamp()),
        )
        if not burned:
            raise HTTPException(status.HTTP_409_CONFLICT, "capability token already used")
        body = await request.body()
        ceiling = (
            state.settings.max_attachment_bytes_ceiling
            if claims.op == "data"
            else state.settings.max_source_bytes
        )
        if len(body) > claims.max_bytes or len(body) > ceiling:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"upload body exceeds cap (size={len(body)}, token_max={claims.max_bytes}, "
                f"host_ceiling={ceiling})",
            )
        slug = safe_slug(claims.slug)

        if claims.op == "data":
            if claims.name is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "data upload token has no name in claims"
                )
            name = safe_attachment_name(claims.name)
            async with state.lock_for(slug):
                # ensure the tree exists even when an attachment arrives before
                # the first publish, with the same 0700 mode a publish would set.
                try:
                    uid = resolve_jail_uid(
                        state.settings.resolved_uids_file,
                        slug,
                        start=state.settings.jail_uid_start,
                        end=state.settings.jail_uid_end,
                        allow_unjailed=state.settings.allow_unjailed_spawn,
                    )
                except JailUnavailableError as err:
                    raise HTTPException(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        f"notebook isolation unavailable: {err}",
                    ) from err
                except UidPoolExhaustedError as err:
                    raise HTTPException(
                        status.HTTP_503_SERVICE_UNAVAILABLE, f"uid pool exhausted: {err}"
                    ) from err
                paths = ensure_slug_jail(state.settings.data_dir, slug, uid=uid)
                final_path = paths.data / name
                _atomic_write_bytes(final_path, body, owner_uid=uid)
                return {
                    "slug": slug,
                    "name": name,
                    "size_bytes": len(body),
                    "path": f"data/{name}",
                }

        # Only an explicit ``notebook_edit`` token gets the editor. A plain
        # scratch notebook is a read-only app like a blog, so a forwarded link
        # runs the notebook without handing out a code-executing editor.
        mode: Literal["edit", "run"] = "edit" if claims.op == "notebook_edit" else "run"
        async with state.lock_for(slug):
            np = await _spawn_tracked(state, slug, body, mode=mode)
            if mode == "edit":
                return _editor_response(state, np)
            expires_at = _register_read_only(
                state,
                np,
                permanent=claims.op == "blog",
                ttl_seconds=claims.notebook_ttl_seconds,
            )
            return _read_only_response(state, np, expires_at)

    return router

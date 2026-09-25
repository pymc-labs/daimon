"""Admin + health routes for notebook-host."""

from __future__ import annotations

import asyncio
import hmac
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

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
    ensure_slug_jail,
    get_slug_paths,
    remove_slug_tree,
    resolve_jail_uid,
)
from notebook_host.lifecycle import (
    NotebookProcess,
    ValidationResult,
    allocate_port,
    kill,
    safe_attachment_name,
    safe_slug,
    should_reap,
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
        mode: Literal["edit", "run"] = "edit",
    ) -> NotebookProcess:
        return NotebookProcess(
            slug=slug,
            port=port,
            process=proc,
            public_host=self.settings.public_host,
            host_port=self.settings.host_port,
            public_url_base=self.settings.public_url_base,
            mode=mode,
        )

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


def _disk_headroom_dep(settings: Settings) -> Callable[[], None]:
    """Refuse writes when the data volume lacks headroom, before any byte lands.

    A full volume otherwise surfaces as an OSError 500 from deep inside the
    first write (historically the jti burn), and on the capability route that
    burn would waste the single-use token. Runs as a route dependency so it
    precedes the handler body entirely. ``min_free_disk_bytes = 0`` disables.
    """

    def require() -> None:
        if settings.min_free_disk_bytes <= 0:
            return
        free = shutil.disk_usage(settings.data_dir).free
        if free < settings.min_free_disk_bytes:
            raise HTTPException(
                status.HTTP_507_INSUFFICIENT_STORAGE,
                f"notebook host is out of disk space (free={free}, "
                f"required={settings.min_free_disk_bytes})",
            )

    return require


def _atomic_write_bytes(path: Path, content: bytes, *, owner_uid: int | None = None) -> None:
    """Write via tmp + ``os.replace`` so a concurrent reader never sees a torn file.

    When ``owner_uid`` is given, the tmp file is chowned to ``(owner_uid,
    owner_uid)`` before the replace, so the file is never visible at its final
    path with the wrong owner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_bytes(content)
        if owner_uid is not None:
            os.chown(tmp, owner_uid, owner_uid)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


async def _spawn_tracked(
    state: AdminState, slug: str, source_bytes: bytes, *, mode: Literal["edit", "run"]
) -> NotebookProcess:
    """Write source, validate, replace any existing process, spawn, wait ready.

    The caller must hold ``state.lock_for(slug)``. Returns the live
    ``NotebookProcess``. Raises ``HTTPException`` 422 (validation), 503 (port
    pool exhausted, or notebook isolation unavailable), or 504 (spawn
    timeout). Shared by the notebook and blog PUT handlers so the two never
    drift.
    """
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
    _atomic_write_bytes(paths.notebook, source_bytes, owner_uid=uid)

    # Confirm the cells actually execute before we tear down any
    # existing notebook for this slug. Runs off the event loop (the
    # marimo export is blocking). A failure here leaves a previously
    # published notebook for this slug untouched and serving.
    if state.validator is not None:
        result = await asyncio.to_thread(state.validator, slug, paths, jail_uid=uid)
        if not result.ok:
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

    port = allocate_port(
        state.processes, state.settings.marimo_port_start, state.settings.marimo_port_end
    )
    proc = state.spawner(slug, paths, port, mode=mode, jail_uid=uid)
    np = state.make_process(slug, port, proc, mode=mode)
    state.processes[slug] = np

    state.snapshot_pids()
    ready = await wait_for_port(port, slug, state.settings.spawn_timeout_seconds)
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
    require_disk_headroom = _disk_headroom_dep(state.settings)

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
        }

    @router.put(
        "/admin/notebooks/{slug}",
        dependencies=[Depends(require_admin), Depends(require_disk_headroom)],
    )
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
            np = await _spawn_tracked(state, slug, source_bytes, mode="edit")
            ttl = state.settings.subprocess_ttl_seconds
            # ttl <= 0 disables age-based reaping — the notebook never expires,
            # so there is no expiry timestamp to report.
            expires_at = (
                datetime.fromtimestamp(np.started_at, tz=UTC) + timedelta(seconds=ttl)
                if ttl > 0
                else None
            )
            return {
                "slug": slug,
                "url": np.url,
                "port": np.port,
                "pid": np.process.pid,
                "size_bytes": get_slug_paths(state.settings.data_dir, slug).notebook.stat().st_size,
                "subprocess_ttl_seconds": ttl,
                "expires_at": expires_at.isoformat() if expires_at is not None else None,
            }

    @router.put(
        "/admin/notebooks/{slug}/data/{name}",
        dependencies=[Depends(require_admin), Depends(require_disk_headroom)],
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
                "size_bytes": final_path.stat().st_size,
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
                }
                for np in sorted(state.processes.values(), key=lambda p: p.slug)
            ]
        }

    @router.put(
        "/admin/blogs/{slug}",
        dependencies=[Depends(require_admin), Depends(require_disk_headroom)],
    )
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
            register_blog(
                state.settings.resolved_blogs_file,
                BlogRecord(slug=slug, created_at=np.started_at),
            )
            return {
                "slug": slug,
                "url": np.url,
                "port": np.port,
                "pid": np.process.pid,
                "size_bytes": get_slug_paths(state.settings.data_dir, slug).notebook.stat().st_size,
            }

    @router.get("/admin/blogs", dependencies=[Depends(require_admin)])
    def list_blogs() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        records = load_blogs(state.settings.resolved_blogs_file)
        blogs: list[dict[str, object]] = []
        for slug, rec in sorted(records.items()):
            np = state.processes.get(slug)
            blogs.append(
                {
                    "slug": slug,
                    "created_at": rec.created_at,
                    "title": rec.title,
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
    def sweep() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        reaped: list[dict[str, object]] = []
        for slug in list(state.processes.keys()):
            np = state.processes[slug]
            if not np.is_alive():
                reason = "dead"
            elif should_reap(np, state.settings.subprocess_ttl_seconds):
                reason = "ttl"
            else:
                continue
            kill(np)
            state.processes.pop(slug, None)
            remove_slug_tree(
                state.settings.data_dir, slug, uids_file=state.settings.resolved_uids_file
            )
            reaped.append({"slug": slug, "reason": reason, "age_s": round(np.age_s, 2)})
        if reaped:
            state.snapshot_pids()
        return {"reaped": reaped, "subprocess_ttl_seconds": state.settings.subprocess_ttl_seconds}

    @router.put("/upload/{token}", dependencies=[Depends(require_disk_headroom)])
    async def upload(token: str, request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        # Public route — authed by the capability token, NOT the admin bearer.
        secrets_list = [s.get_secret_value() for s in state.settings.admin_secrets]
        claims: CapabilityClaims = verify_token(secrets_list, token, now=datetime.now(UTC))
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
                    "size_bytes": final_path.stat().st_size,
                    "path": f"data/{name}",
                }

        mode: Literal["edit", "run"] = "run" if claims.op == "blog" else "edit"
        async with state.lock_for(slug):
            np = await _spawn_tracked(state, slug, body, mode=mode)
            size_bytes = get_slug_paths(state.settings.data_dir, slug).notebook.stat().st_size
            if claims.op == "blog":
                register_blog(
                    state.settings.resolved_blogs_file,
                    BlogRecord(slug=slug, created_at=np.started_at),
                )
                return {
                    "slug": slug,
                    "url": np.url,
                    "port": np.port,
                    "pid": np.process.pid,
                    "size_bytes": size_bytes,
                }
            ttl = state.settings.subprocess_ttl_seconds
            expires_at = (
                datetime.fromtimestamp(np.started_at, tz=UTC) + timedelta(seconds=ttl)
                if ttl > 0
                else None
            )
            return {
                "slug": slug,
                "url": np.url,
                "port": np.port,
                "pid": np.process.pid,
                "size_bytes": size_bytes,
                "subprocess_ttl_seconds": ttl,
                "expires_at": expires_at.isoformat() if expires_at is not None else None,
            }

    return router

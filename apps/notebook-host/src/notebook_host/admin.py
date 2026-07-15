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
        self, slug: str, file_path: Path, port: int, *, mode: Literal["edit", "run"] = "edit"
    ) -> subprocess.Popen[bytes]: ...


class Validator(Protocol):
    def __call__(self, slug: str, file_path: Path) -> ValidationResult: ...


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
    # Single-use capability-token jtis already burned. Process-local; bounded by
    # the token TTL (~300s) across restarts. Replay of a consumed token → 409.
    consumed: set[str] = field(default_factory=set[str])

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


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write via tmp + ``os.replace`` so a concurrent reader never sees a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_bytes(content)
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
    pool exhausted), or 504 (spawn timeout). Shared by the notebook and blog
    PUT handlers so the two never drift.
    """
    state.settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = state.settings.data_dir / f"{slug}.py"
    path.write_bytes(source_bytes)

    # Confirm the cells actually execute before we tear down any
    # existing notebook for this slug. Runs off the event loop (the
    # marimo export is blocking). A failure here leaves a previously
    # published notebook for this slug untouched and serving.
    if state.validator is not None:
        result = await asyncio.to_thread(state.validator, slug, path)
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
    proc = state.spawner(slug, path, port, mode=mode)
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
                "size_bytes": (state.settings.data_dir / f"{slug}.py").stat().st_size,
                "subprocess_ttl_seconds": ttl,
                "expires_at": expires_at.isoformat() if expires_at is not None else None,
            }

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
            data_dir = state.settings.data_dir / f"{slug}.data"
            final_path = data_dir / name
            _atomic_write_bytes(final_path, body)
            return {
                "slug": slug,
                "name": name,
                "size_bytes": final_path.stat().st_size,
                "path": f"data/{name}",
            }

    @router.delete(
        "/admin/notebooks/{slug}",
        dependencies=[Depends(require_admin)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_notebook(slug: str) -> None:  # pyright: ignore[reportUnusedFunction]
        slug = safe_slug(slug)
        np = state.processes.pop(slug, None)
        if np is not None:
            kill(np)
        (state.settings.data_dir / f"{slug}.py").unlink(missing_ok=True)
        # A crash between the .py unlink and these rmtrees leaks the dirs;
        # the slug is gone from state.processes so sweep won't re-discover them.
        shutil.rmtree(state.settings.data_dir / f"{slug}.data", ignore_errors=True)
        shutil.rmtree(state.settings.data_dir / f"{slug}_workspace", ignore_errors=True)
        state.snapshot_pids()

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
            register_blog(
                state.settings.resolved_blogs_file,
                BlogRecord(slug=slug, created_at=np.started_at),
            )
            return {
                "slug": slug,
                "url": np.url,
                "port": np.port,
                "pid": np.process.pid,
                "size_bytes": (state.settings.data_dir / f"{slug}.py").stat().st_size,
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
    def delete_blog(slug: str) -> None:  # pyright: ignore[reportUnusedFunction]
        slug = safe_slug(slug)
        np = state.processes.pop(slug, None)
        if np is not None:
            kill(np)
        unregister_blog(state.settings.resolved_blogs_file, slug)
        (state.settings.data_dir / f"{slug}.py").unlink(missing_ok=True)
        shutil.rmtree(state.settings.data_dir / f"{slug}.data", ignore_errors=True)
        shutil.rmtree(state.settings.data_dir / f"{slug}_workspace", ignore_errors=True)
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
            (state.settings.data_dir / f"{slug}.py").unlink(missing_ok=True)
            shutil.rmtree(state.settings.data_dir / f"{slug}.data", ignore_errors=True)
            shutil.rmtree(state.settings.data_dir / f"{slug}_workspace", ignore_errors=True)
            reaped.append({"slug": slug, "reason": reason, "age_s": round(np.age_s, 2)})
        if reaped:
            state.snapshot_pids()
        return {"reaped": reaped, "subprocess_ttl_seconds": state.settings.subprocess_ttl_seconds}

    @router.put("/upload/{token}")
    async def upload(token: str, request: Request) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        # Public route — authed by the capability token, NOT the admin bearer.
        secrets_list = [s.get_secret_value() for s in state.settings.admin_secrets]
        claims: CapabilityClaims = verify_token(secrets_list, token, now=datetime.now(UTC))
        if claims.jti in state.consumed:
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
        # Single-use is best-effort: the in-set check and this add straddle the
        # body read, so two concurrent replays of one token could both pass. Low
        # risk — both carry identical authorized bytes and the slug lock serialises
        # the writes. Process-local, TTL-bounded; a persistent store is day-2.
        state.consumed.add(claims.jti)
        slug = safe_slug(claims.slug)

        if claims.op == "data":
            if claims.name is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "data upload token has no name in claims"
                )
            name = safe_attachment_name(claims.name)
            async with state.lock_for(slug):
                final_path = state.settings.data_dir / f"{slug}.data" / name
                _atomic_write_bytes(final_path, body)
                return {
                    "slug": slug,
                    "name": name,
                    "size_bytes": final_path.stat().st_size,
                    "path": f"data/{name}",
                }

        mode: Literal["edit", "run"] = "run" if claims.op == "blog" else "edit"
        async with state.lock_for(slug):
            np = await _spawn_tracked(state, slug, body, mode=mode)
            size_bytes = (state.settings.data_dir / f"{slug}.py").stat().st_size
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

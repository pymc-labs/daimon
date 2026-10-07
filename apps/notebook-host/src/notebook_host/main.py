"""FastAPI app factory for notebook-host.

`create_app(settings)` wires:
  - AdminState (injected settings + lifecycle.spawn_marimo as default spawner)
  - Admin router (PUT/DELETE/list/sweep/health)
  - Proxy router, which starts a registered notebook on its first visit
  - Lifespan context that starts the background sweep task and kills all
    subprocesses on shutdown
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI

from notebook_host.admin import AdminState, create_admin_router
from notebook_host.config import Settings
from notebook_host.jail import JailUnavailableError, SlugPaths, can_apply_jail
from notebook_host.lazy_spawn import sweep_once
from notebook_host.lifecycle import (
    NotebookProcess,
    ValidationResult,
    has_inline_script_metadata,
    kill,
    spawn_marimo,
    validate_notebook,
)
from notebook_host.migration import migrate_flat_layout
from notebook_host.pids_store import reap_orphans
from notebook_host.proxy import create_proxy_router

_log = logging.getLogger(__name__)


def check_link_security(settings: Settings) -> None:
    """Refuse to serve tokenized links over plain http off localhost.

    Every link carries its notebook's access token, and marimo's session cookie
    is only as private as the transport. ``allow_http_links`` is the explicit
    opt-out for a trusted private network.
    """
    if settings.allow_http_links or settings.is_local_dev:
        return
    if settings.origin_base is not None:
        if settings.origin_scheme != "https":
            raise RuntimeError(
                "DAIMON_NOTEBOOK__ORIGIN_SCHEME must be https for a public ORIGIN_BASE; links "
                "carry each notebook's access token."
            )
        return
    base = settings.public_url_base
    if base is not None:
        if not base.startswith("https://"):
            raise RuntimeError(
                f"DAIMON_NOTEBOOK__PUBLIC_URL_BASE must be https:// (got {base!r}); links "
                "carry each notebook's access token. Set DAIMON_NOTEBOOK__ALLOW_HTTP_LINKS=true "
                "only on a trusted private network."
            )
        return
    raise RuntimeError(
        f"public_host {settings.public_host!r} would get plain-http links; set "
        "DAIMON_NOTEBOOK__PUBLIC_URL_BASE to its https:// origin, or "
        "DAIMON_NOTEBOOK__ALLOW_HTTP_LINKS=true on a trusted private network."
    )


def warn_shared_origin(settings: Settings) -> None:
    """Say at boot which tenants a shared-origin public host will serve."""
    if settings.is_local_dev:
        return
    if settings.origin_base is not None:
        if settings.tenants:
            _log.warning(
                "DAIMON_NOTEBOOK__TENANTS has no effect with DAIMON_NOTEBOOK__ORIGIN_BASE: "
                "every tenant gets per-notebook origins."
            )
        return
    if not settings.tenants:
        _log.warning(
            "notebooks share one browser origin on this host and DAIMON_NOTEBOOK__TENANTS "
            "is empty, so every upload is refused. List the tenant ids allowed to share the "
            "origin there (a JSON array of UUIDs), or set DAIMON_NOTEBOOK__ORIGIN_BASE "
            "(wildcard DNS + TLS) for per-notebook origins."
        )
        return
    _log.warning(
        "notebooks share one browser origin on this host: the %d tenant(s) in "
        "DAIMON_NOTEBOOK__TENANTS can reach each other's notebooks. Set "
        "DAIMON_NOTEBOOK__ORIGIN_BASE (wildcard DNS + TLS) for per-notebook origins.",
        len(settings.tenants),
    )


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
        access_token: str,
        mode: Literal["edit", "run"] = "edit",
        jail_uid: int | None = None,
    ) -> subprocess.Popen[bytes]:
        return spawn_marimo(
            slug,
            paths,
            port,
            access_token=access_token,
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
        check_link_security(settings)
        warn_shared_origin(settings)
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
        # Registered notebooks are not started here: each one starts on its
        # first visit (lazy_spawn.ensure_running), so a restart costs nothing
        # for the notebooks nobody opens.
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


async def _sweep_loop(state: AdminState) -> None:
    """Background task: sweep every sweep_interval_seconds (see ``lazy_spawn.sweep_once``)."""
    while True:
        await asyncio.sleep(state.settings.sweep_interval_seconds)
        try:
            result = await sweep_once(state, now=time.time())
            if result.mutated:
                state.snapshot_pids()
        except Exception:
            _log.exception("sweep iteration failed; will retry next cycle")

"""Subprocess lifecycle: spawn, port allocation, ready-wait, kill."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from fastapi import HTTPException, status

_log = logging.getLogger(__name__)

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SLUG_MAX_LEN = 64
_ATTACHMENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
# PEP 723 inline script metadata opener. marimo's --sandbox reads the
# `# /// script` block's `dependencies` and builds a per-notebook uv venv from
# them. We match the spec's opening line so our detection agrees with marimo's
# own parser (https://peps.python.org/pep-0723/).
_INLINE_SCRIPT_METADATA = re.compile(r"^# /// script$", re.MULTILINE)


def has_inline_script_metadata(source: str) -> bool:
    """True when the notebook declares PEP 723 inline dependencies.

    Used to decide whether to spawn marimo with ``--sandbox``. Sandbox installs
    the block's ``dependencies`` into an isolated uv venv; *without* a block it
    would hand the notebook only marimo — stripping the host's baked
    pandas/numpy/pymc stack. So the host sandboxes a notebook only when it opted
    in by declaring its own deps; headerless notebooks keep the baked env.
    """
    return _INLINE_SCRIPT_METADATA.search(source) is not None


def safe_attachment_name(name: str) -> str:
    # No path separators; leading char rules out hidden files and argv flags.
    if not _ATTACHMENT_NAME_PATTERN.fullmatch(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid attachment name: {name!r}")
    return name


@dataclass
class NotebookProcess:
    slug: str
    port: int
    process: subprocess.Popen[bytes]
    public_host: str
    host_port: int
    public_url_base: str | None = None
    started_at: float = field(default_factory=time.time)
    mode: Literal["edit", "run"] = "edit"

    @property
    def url(self) -> str:
        if self.public_url_base is not None:
            return f"{self.public_url_base.rstrip('/')}/n/{self.slug}/"
        return f"http://{self.public_host}:{self.host_port}/n/{self.slug}/"

    @property
    def internal_url(self) -> str:
        return f"http://localhost:{self.port}/n/{self.slug}/"

    @property
    def age_s(self) -> float:
        return time.time() - self.started_at

    def is_alive(self) -> bool:
        return self.process.poll() is None


def should_reap(np: NotebookProcess, ttl_seconds: int) -> bool:
    """Whether the sweeper should reclaim this subprocess.

    Run-mode processes are blogs: permanent, never killed-and-deleted by age or
    death here (their liveness/respawn is the sweep's separate concern). For an
    edit-mode notebook, a dead subprocess is always reaped; an alive one is
    reaped only when a *positive* TTL is configured and it has outlived it.
    ``ttl_seconds <= 0`` disables age-based reaping entirely — the notebook lives
    until its kernel dies or it is explicitly deleted. Shared by the background
    sweep loop and the ``/admin/sweep`` endpoint so the two never diverge.
    """
    if np.mode == "run":
        return False
    if not np.is_alive():
        return True
    return ttl_seconds > 0 and np.age_s > ttl_seconds


def allocate_port(processes: dict[str, NotebookProcess], start: int, end: int) -> int:
    used = {p.port for p in processes.values()}
    for port in range(start, end + 1):
        if port not in used:
            return port
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"port pool exhausted ({start}-{end}, {len(used)} in use)",
    )


def safe_slug(slug: str) -> str:
    # Leading "-" would let the slug be parsed as an argv flag to `marimo edit`.
    if not slug or len(slug) > _SLUG_MAX_LEN or slug.startswith("-"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid slug: {slug!r}")
    if not _SLUG_PATTERN.fullmatch(slug):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid slug: {slug!r}")
    return slug


def scrub_env(parent_env: dict[str, str]) -> dict[str, str]:
    # Notebook code is untrusted Python; the admin bearer must not leak via os.environ.
    drop_prefixes = ("DAIMON_NOTEBOOK__", "DAIMON_")
    return {k: v for k, v in parent_env.items() if not k.startswith(drop_prefixes)}


def _make_preexec(
    rlimit_as_bytes: int | None, rlimit_cpu_seconds: int | None
) -> Callable[[], None] | None:
    """Return a preexec_fn that applies RLIMIT_AS / RLIMIT_CPU in the child.

    Linux only. On macOS dev hosts, RLIMIT_AS is unreliable (some kernels
    reject any value), so we no-op there and rely on prod (Fly = Linux) for
    enforcement. Returns None when limits are unset or platform is non-Linux.
    """
    if sys.platform != "linux":
        return None
    if not rlimit_as_bytes and not rlimit_cpu_seconds:
        return None

    import resource  # Linux-only stdlib; guarded above.

    def _apply() -> None:  # runs in the forked child, before launching marimo
        if rlimit_as_bytes:
            resource.setrlimit(resource.RLIMIT_AS, (rlimit_as_bytes, rlimit_as_bytes))
        if rlimit_cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (rlimit_cpu_seconds, rlimit_cpu_seconds))

    return _apply


def _prepare_workspace(file_path: Path, slug: str) -> Path:
    """Create ``<slug>_workspace/`` next to ``file_path`` and wire its symlinks.

    Layout (under ``file_path.parent``):
        <slug>.py                    — source file (caller-owned)
        <slug>.data/                 — data dir for attachments
        <slug>_workspace/            — marimo cwd
            data        -> ../<slug>.data
            <slug>.py   -> ../<slug>.py

    Idempotent: stale links/files are replaced so a mid-spawn crash doesn't
    wedge the next attempt.
    """
    data_dir_parent = file_path.parent
    slug_data_dir = data_dir_parent / f"{slug}.data"
    slug_data_dir.mkdir(parents=True, exist_ok=True)

    workspace = data_dir_parent / f"{slug}_workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Relative symlinks so the workspace dir is location-independent.
    data_link = workspace / "data"
    source_link = workspace / file_path.name  # e.g. "<slug>.py"
    targets: tuple[tuple[Path, Path], ...] = (
        (data_link, Path("..") / f"{slug}.data"),
        (source_link, Path("..") / file_path.name),
    )
    for link, target in targets:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    return workspace


def spawn_marimo(
    slug: str,
    file_path: Path,
    port: int,
    *,
    mode: Literal["edit", "run"] = "edit",
    sandbox: bool = False,
    rlimit_as_bytes: int | None = None,
    rlimit_cpu_seconds: int | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn ``marimo <mode> <basename>`` on ``port`` from a per-slug workspace.

    cwd is the workspace dir, so the basename arg resolves through the source
    symlink. ``--base-url /n/<slug>`` keeps the proxy a straight passthrough.
    ``start_new_session`` isolates the child's process group; env is scrubbed
    so notebook code can't read the admin bearer; RLIMIT_AS/CPU (Linux) cap
    runaway notebooks (inherited by the sandbox's re-exec'd descendants).

    ``sandbox`` adds ``--sandbox``, which makes marimo install the notebook's
    PEP 723 ``dependencies`` into an isolated uv venv — the only way a notebook
    can use a library outside the host's baked set. Pass it only for notebooks
    that declare inline metadata (``has_inline_script_metadata``); a headerless
    notebook under ``--sandbox`` loses the baked pandas/numpy/pymc stack.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv not on PATH")
    workspace = _prepare_workspace(file_path, slug)
    cmd = [uv, "run", "--with", "marimo", "marimo", mode]
    if sandbox:
        cmd.append("--sandbox")
    cmd += [
        file_path.name,
        "--no-token",
        "--headless",
        "--host",
        "0.0.0.0",
        "-p",
        str(port),
        "--base-url",
        f"/n/{slug}",
    ]
    log_path = file_path.parent / f"{slug}.marimo.log"
    log_fh = open(log_path, "ab")  # noqa: SIM115 — owned by subprocess
    preexec = _make_preexec(rlimit_as_bytes, rlimit_cpu_seconds)
    if preexec is None and sys.platform != "linux" and (rlimit_as_bytes or rlimit_cpu_seconds):
        _log.warning(
            "rlimit configured but platform=%s is not Linux; "
            "subprocess will run without resource caps",
            sys.platform,
        )
    try:
        return subprocess.Popen(
            cmd,
            cwd=str(workspace),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=scrub_env(dict(os.environ)),
            preexec_fn=preexec,
        )
    finally:
        log_fh.close()


async def wait_for_port(port: int, slug: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    url = f"http://localhost:{port}/n/{slug}/"
    async with httpx.AsyncClient(timeout=2.0) as c:
        while time.monotonic() < deadline:
            try:
                r = await c.get(url)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    return False


# marimo emits one of these when a cell fails to build or execute during an
# export. `MultipleDefinitionError` is the silent loop-variable collision that
# `publish_notebook` would otherwise ship as a notebook that errors on load.
_CELL_ERROR_MARKERS = (
    "cells failed to execute",
    "MultipleDefinitionError",
    "MarimoExceptionRaisedError",
    "SyntaxError",
    "Traceback (most recent call last)",
)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list[str])
    timed_out: bool = False


def _extract_cell_errors(output: str) -> list[str]:
    seen: set[str] = set()
    hits: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line and any(marker in line for marker in _CELL_ERROR_MARKERS) and line not in seen:
            seen.add(line)
            hits.append(line)
    # Cap so a pathological notebook can't return a megabyte of repeated
    # tracebacks back through the MCP tool to the agent.
    return hits[:20]


def validate_notebook(
    slug: str,
    file_path: Path,
    *,
    timeout_s: float,
    sandbox: bool = False,
    rlimit_as_bytes: int | None = None,
    rlimit_cpu_seconds: int | None = None,
) -> ValidationResult:
    """Run ``marimo export html`` to confirm the notebook's cells actually run.

    ``publish_notebook`` accepting a source says nothing about whether it
    executes — marimo silently refuses to run cells that violate its dataflow
    rules. This executes the notebook headlessly in the *same* command, env, and
    workspace ``spawn_marimo`` uses (so import availability and the ``data/``
    attachments match the served notebook), and reports any cell errors.

    ``sandbox`` must mirror the value passed to ``spawn_marimo`` for the same
    notebook — otherwise validation runs in a different env than what's served
    (validate without the PEP 723 deps, then serve with them, or vice versa).
    The caller derives both from ``has_inline_script_metadata`` so they agree. A
    cold sandbox install here also warms uv's shared cache, so the subsequent
    spawn starts fast.

    Blocking (uses ``subprocess.run``) — call via ``asyncio.to_thread`` from the
    async request handler. On timeout, returns ``ok=True`` with
    ``timed_out=True``: a slow-but-possibly-valid notebook is published rather
    than false-rejected, while the fast structural errors this targets
    (collisions, import failures) surface near-instantly at graph-build time.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv not on PATH")
    workspace = _prepare_workspace(file_path, slug)
    preexec = _make_preexec(rlimit_as_bytes, rlimit_cpu_seconds)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [uv, "run", "--with", "marimo", "marimo", "export", "html"]
        if sandbox:
            cmd.append("--sandbox")
        cmd += [file_path.name, "-o", str(Path(tmp) / "check.html")]
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                cwd=str(workspace),
                env=scrub_env(dict(os.environ)),
                preexec_fn=preexec,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(ok=True, timed_out=True)
    errors = _extract_cell_errors(f"{proc.stdout}\n{proc.stderr}")
    if errors:
        return ValidationResult(ok=False, errors=errors)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        return ValidationResult(
            ok=False, errors=[f"marimo export failed (exit {proc.returncode}): {detail[:200]}"]
        )
    return ValidationResult(ok=True)


def kill(np: NotebookProcess) -> None:
    """SIGTERM → 5s wait → SIGKILL, sent to the whole process group."""
    if not np.is_alive():
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(np.process.pid), signal.SIGTERM)
    try:
        np.process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(np.process.pid), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            np.process.wait(timeout=2)

"""Per-slug on-disk layout and directory-tree lifecycle.

This module owns three things: the shape of a slug's directory tree
(``SlugPaths``), creating/removing that tree on disk, and the privilege-drop
mechanism a spawned/validated notebook runs under. ``data_dir/<slug>/`` is the
isolation boundary a jailed marimo process is confined to; every host-owned
registry file (``blogs.json``, ``pids.json``, ``uids.json``) deliberately sits
outside it, as a sibling of every slug root rather than inside any of them.

Nothing here imports ``Settings`` — callers read config and pass paths / ints
in explicitly, so this module has no dependency on ``notebook_host.config``.

Pure functions only for path computation; the tree create/remove pair and the
uid-resolution/privilege-drop functions are the only filesystem/process I/O
(mkdir, chmod, chown, rmtree, and dropping into another uid — no clock, no
network).

``data_dir`` itself must be traversable (mode 0711, ``DATA_DIR_MODE`` below) so
a dropped uid can resolve an absolute path down into its own subtree — Linux
requires the ``x`` bit on every path component, including ones the caller
doesn't otherwise need to read. It must not be listable, so a jailed process
can reach a path it already knows but cannot enumerate sibling slugs. This is
strictly weaker than the per-slug boundary (``SLUG_TREE_MODE``, still 0700)
and does not change it: the parent grants traversal only, ownership of every
subtree stays exclusive to that slug's uid.

Two documented limitations of the uid split, deliberately not worked around:

1. Allocated uids have no ``/etc/passwd`` entry. ``uv``/``marimo`` do not need
   one (verified — neither does an NSS lookup once ``HOME`` is set explicitly),
   but notebook code that itself calls ``Path.home()``,
   ``os.path.expanduser("~")`` or ``getpass.getuser()`` will raise
   ``KeyError: getpwuid()``. This is a correctness footnote for tenant
   notebook authors, not an isolation gap.
2. Because ``HOME`` is per-slug, each slug's ``uv`` cache is private. Repeated
   ``--sandbox`` publishes across *different* slugs each pay their own PEP 723
   dependency download instead of sharing one warm cache — a direct,
   unavoidable consequence of per-uid isolation, not a bug.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

SLUG_TREE_MODE = 0o700
"""Mode for each slug's uid-owned directories (data, workspace, home, tmp).

This is the real isolation boundary: only the slug's own uid (chowned by
``ensure_slug_jail``) can read, write, or traverse it.
"""

SLUG_ROOT_MODE = 0o711
"""Mode for the slug root itself, owned by the host (root), not the jail uid.

The host writes ``notebook.py`` and ``marimo.log`` there and chowns and wipes
the subdirectories as root. If the jail uid owned the root, a cell could
rename ``home`` away and plant ``home -> /etc``, and the host's next chown
would hand ``/etc`` to the jail uid. Owned by the host, its entries can't be
swapped. 0711 lets the uid traverse into its own subdirectories; the files
the host keeps here are 0600.
"""

DATA_DIR_MODE = 0o711
"""Mode for ``data_dir`` itself — traversable by any uid, listable by none.

Corrected 2026-07-31: a live container rehearsal (plan 05) proved ``0700``
denies a dropped uid traversal into its OWN subtree, since resolving an
absolute path requires the ``x`` bit on every path component. ``0711`` fixes
that while keeping ``data_dir`` unlistable (no ``r`` bit), so a jailed
process can reach a path it already knows but cannot enumerate other slugs.
``migration.py`` must use this exact value — call ``lock_data_dir_root``
rather than chmod'ing ``data_dir`` with a locally-defined mode, so the two
can never drift apart.
"""

_REGISTRY_MODE = 0o600
"""Mode for host-owned registry files (uids.json here; blogs.json/pids.json
in their own modules). Explicit because under ``DATA_DIR_MODE`` the parent no
longer hides them via its own unlistability, and the file's default-umask
mode (0644) would otherwise leave it world-readable. ``blogs.json`` in
particular holds every blog's slug and access token — reading it hands out
every blog's link."""


def lock_data_dir_root(data_dir: Path) -> None:
    """Chmod ``data_dir`` itself to ``DATA_DIR_MODE``. Idempotent; call on every boot.

    The single place this mode is ever applied — callers (``migration.py``)
    must go through this function instead of chmod'ing ``data_dir`` directly,
    so the parent's mode can't silently drift from the slug tree's.
    """
    os.chmod(data_dir, DATA_DIR_MODE)


def lock_registry_file(path: Path) -> None:
    """Chmod an existing host-owned registry file to ``_REGISTRY_MODE``. No-op if absent.

    The stores lock their tmp file before the atomic rename, so a file this
    host has written is already correct. This exists for the files a host
    *inherits* — a registry created by a release that predates the jail keeps
    its old default-umask mode (0644) until something happens to rewrite it,
    which on a quiet host may be never. Since the same boot that inherits them
    also makes ``data_dir`` traversable, leaving them is what turns an
    unlistable-parent assumption into a readable file.
    """
    if path.exists():
        os.chmod(path, _REGISTRY_MODE)


@dataclass(frozen=True)
class SlugPaths:
    """Every on-disk path a slug owns. The single source of truth for the layout.

    ``notebook``'s basename is always ``notebook.py`` regardless of slug —
    deliberate, because ``spawn_marimo`` passes ``file_path.name`` as marimo's
    positional argument, so a fixed basename keeps that argv slug-independent.
    """

    root: Path
    notebook: Path
    data: Path
    workspace: Path
    home: Path
    log: Path
    # TMPDIR for the slug's processes, so nothing it writes lands in the
    # host-wide /tmp another notebook's uid can list.
    tmp: Path


def get_slug_paths(data_dir: Path, slug: str) -> SlugPaths:
    """Compute every path a slug owns. Pure — no filesystem access.

    ``slug`` must already have passed ``lifecycle.safe_slug``; this function
    does not re-validate it (importing ``lifecycle`` from here would create a
    cycle, since plan-wiring makes ``lifecycle`` import ``jail``).
    """
    root = data_dir / slug
    return SlugPaths(
        root=root,
        notebook=root / "notebook.py",
        data=root / "data",
        workspace=root / "workspace",
        home=root / "home",
        log=root / "marimo.log",
        tmp=root / "tmp",
    )


def ensure_slug_jail(data_dir: Path, slug: str, *, uid: int | None = None) -> SlugPaths:
    """Create (or repair) a slug's tree without ever following a symlink.

    ``root`` is owned by the host at ``SLUG_ROOT_MODE`` (0711); ``data``,
    ``workspace``, ``home`` and ``tmp`` are 0700 and, when ``uid`` is given,
    owned by it. Every directory is opened ``O_NOFOLLOW | O_DIRECTORY`` and
    changed through the fd, so a symlink planted where a directory should be
    (possible in trees from before the root was host-owned) is removed, never
    chowned through. A symlinked ``notebook.py`` or ``marimo.log`` is unlinked
    for the same reason.

    Idempotent: safe to call repeatedly on an existing, already-owned tree.
    Does NOT touch anything inside the subdirectories — files the jailed
    marimo process creates are already owned correctly by construction.

    ``slug`` must already have passed ``lifecycle.safe_slug``.
    """
    paths = get_slug_paths(data_dir, slug)
    data_dir.mkdir(parents=True, exist_ok=True)
    _secure_dir(paths.root, SLUG_ROOT_MODE, owner=(os.geteuid(), os.getegid()))
    for f in (paths.notebook, paths.log):
        if f.is_symlink():
            f.unlink()
    for d in (paths.data, paths.workspace, paths.home, paths.tmp):
        _secure_dir(d, SLUG_TREE_MODE, owner=(uid, uid) if uid is not None else None)
    return paths


def _secure_dir(path: Path, mode: int, *, owner: tuple[int, int] | None) -> None:
    """Make ``path`` a real directory with ``mode`` and ``owner``, symlinks refused."""
    remove_path(path, keep_real_dir=True)
    with contextlib.suppress(FileExistsError):
        path.mkdir()
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if owner is not None:
            os.fchown(fd, *owner)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def remove_path(path: Path, *, keep_real_dir: bool = False) -> None:
    """Remove ``path`` without following it: unlink links and files, rmtree dirs.

    ``shutil.rmtree`` refuses a symlink (and ``ignore_errors`` would leave it
    in place), so a link is always unlinked. ``keep_real_dir`` leaves a real
    directory alone and removes anything else in its place.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        if not keep_real_dir:
            shutil.rmtree(path)
        return
    path.unlink()


def write_file_nofollow(
    path: Path, content: bytes, *, owner_uid: int | None, mode: int = 0o600
) -> None:
    """Atomically write ``path`` as root without following any link an attacker planted.

    The tmp file is created ``O_CREAT | O_EXCL | O_NOFOLLOW`` next to ``path``
    (a stale one is unlinked first, never opened), written and chowned through
    its fd, then renamed over ``path``; a rename replaces a symlink at the
    destination rather than writing through it.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    remove_path(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        try:
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view) :]
            if owner_uid is not None:
                os.fchown(fd, owner_uid, owner_uid)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def open_log_nofollow(path: Path) -> int:
    """Open the slug's host-owned log for append, refusing a symlink. Returns an fd."""
    return os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )


def remove_slug_tree(data_dir: Path, slug: str, *, uids_file: Path | None = None) -> None:
    """Remove a slug's whole directory tree in one call.

    Replaces the unlink-plus-two-rmtrees pattern the flat layout required
    across three call sites — exactly the duplication that let the
    background sweep delete only one of the three on-disk pieces. A no-op
    (does not raise) if the tree doesn't exist.

    When ``uids_file`` is given, the slug's uid is also released from the
    registry (via ``release_slug_uid``) after the tree is gone — keeping uid
    release on the same single code path every delete site already calls,
    rather than adding a fourth thing each caller must remember.

    Before the uid is released, every process still running as it is killed
    (``kill_uid_processes``) and every file it owns in the shared temp dirs is
    deleted (``remove_uid_files``). ``lifecycle.kill`` only signals marimo's
    process group, and a cell can start a detached child that outlives it; that
    child would otherwise sit under whichever slug is handed the uid next. If
    anything survives the kill, the uid is quarantined instead of released.

    ``slug`` must already have passed ``lifecycle.safe_slug``.
    """
    uid = load_uid_registry(uids_file).get(slug) if uids_file is not None else None
    if uids_file is not None and uid is not None:
        try:
            kill_uid_processes(uid)
        except UidStillInUseError:
            _log.error("uid %d of slug %r still has processes; quarantining it", uid, slug)
            quarantine_slug_uid(uids_file, slug)
            shutil.rmtree(get_slug_paths(data_dir, slug).root, ignore_errors=True)
            return
        remove_uid_files(uid)
    shutil.rmtree(get_slug_paths(data_dir, slug).root, ignore_errors=True)
    if uids_file is not None:
        release_slug_uid(uids_file, slug)


class UidStillInUseError(RuntimeError):
    """Raised when a jail uid still has processes after ``kill_uid_processes``."""


def _process_uids(status_file: Path) -> tuple[int, ...]:
    """The real, effective, saved and filesystem uids from ``/proc/<pid>/status``."""
    try:
        text = status_file.read_text()
    except OSError:
        return ()
    for line in text.splitlines():
        if line.startswith("Uid:"):
            return tuple(int(field) for field in line.split()[1:])
    return ()


# Run as the jail uid: kill(-1) then reaches every process of that uid, and
# only those, in one kernel walk (Linux never signals the caller itself).
_KILL_ALL_AS_UID = """
import os, signal
try:
    os.kill(-1, signal.SIGKILL)
except ProcessLookupError:
    pass
"""


def _signal_all_as(uid: int) -> None:
    """SIGKILL every process of ``uid`` by becoming it and calling ``kill(-1)``.

    Unlike signalling pids from a ``/proc`` scan, a process can't fork its way
    out of the walk. Needs root; elsewhere (and if the interpreter can't be
    exec'd as ``uid``) it does nothing and the caller's scan falls back to
    per-pid kills.
    """
    if os.geteuid() != 0:
        return

    def _drop() -> None:
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [sys.executable, "-I", "-S", "-c", _KILL_ALL_AS_UID],
            preexec_fn=_drop,
            env={},
            cwd="/",
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def kill_uid_processes(
    uid: int,
    *,
    proc_root: Path = Path("/proc"),
    signal_all_as: Callable[[int], None] = _signal_all_as,
    deadline_s: float = 5.0,
) -> None:
    """Kill every process any of whose uids is ``uid``; raise if one survives.

    Each pass kills the whole uid from the kernel's side (``signal_all_as``),
    then scans ``proc_root`` and SIGKILLs any pid still showing the uid. It
    loops until a scan finds none, and raises ``UidStillInUseError`` if
    ``deadline_s`` passes first, so callers fail closed rather than hand the
    uid to another slug. ``uid`` is always a jail uid, never the host's own.
    ``RLIMIT_NPROC`` (``build_jailed_preexec``) bounds how fast a fork loop can
    refill the uid.
    """
    deadline = time.monotonic() + deadline_s
    while True:
        signal_all_as(uid)
        survivors = [
            int(entry.name)
            for entry in proc_root.iterdir()
            if entry.name.isdigit() and uid in _process_uids(entry / "status")
        ]
        if not survivors:
            return
        for pid in survivors:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if time.monotonic() >= deadline:
            raise UidStillInUseError(f"uid {uid} still has {len(survivors)} process(es)")
        time.sleep(0.05)


SHARED_TEMP_DIRS: tuple[Path, ...] = (Path("/tmp"), Path("/dev/shm"))


def remove_uid_files(uid: int, *, roots: tuple[Path, ...] = SHARED_TEMP_DIRS) -> None:
    """Delete everything ``uid`` owns under the host-wide temp dirs.

    A notebook's TMPDIR is inside its own slug tree, but code can still write
    to ``/tmp`` or ``/dev/shm`` directly, and those files would be readable by
    whichever slug gets the uid next. Symlinks are unlinked, never followed.
    Only root can delete another uid's files, so elsewhere this does nothing.
    """
    if os.geteuid() not in (0, uid):
        return
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in [*dirnames, *filenames]:
                path = Path(dirpath) / name
                try:
                    st = path.lstat()
                except OSError:
                    continue
                if st.st_uid != uid:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(path, ignore_errors=True)
                    if name in dirnames:
                        dirnames.remove(name)
                else:
                    with contextlib.suppress(OSError):
                        path.unlink()


# ─── uid pool ────────────────────────────────────────────────────────────────
#
# A persisted slug -> uid registry, not a deterministic hash of the slug.
# Blogs are permanent and accumulate for the life of the deployment, so a
# hash into any convenient-sized range carries real birthday-collision risk
# — and a collision silently defeats isolation for both colliding slugs. The
# registry lives at ``data_dir / "uids.json"``, alongside ``blogs.json`` and
# ``pids.json``, outside every slug root. A plain ``dict[str, int]`` is
# enough here — unlike ``PidRecord``/``BlogRecord``, a uid row carries no
# other fields, so a Pydantic model would buy nothing.


class UidPoolExhaustedError(RuntimeError):
    """Raised when every uid in the configured range is already allocated."""


def load_uid_registry(path: Path) -> dict[str, int]:
    """Read the uid registry. Returns an empty dict if missing or malformed.

    Same posture as ``pids_store.load_pids``: a malformed file means a
    previous instance died mid-write, so we forget it rather than trust
    partial state. Entries whose key is not a ``str`` or whose value is not
    a genuine ``int`` (``bool`` is an ``int`` subclass and is rejected too)
    are skipped rather than raising.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(key, str):
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        out[key] = value
    return out


def save_uid_registry(path: Path, records: dict[str, int]) -> None:
    """Atomically rewrite the uid registry (tmp + rename), same idiom as save_pids.

    The tmp file is locked to ``_REGISTRY_MODE`` (0600) before the rename, not
    after — so there is never a window where a freshly written or rewritten
    registry is briefly world-readable under the default umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(records, indent=2))
    os.chmod(tmp, _REGISTRY_MODE)
    os.replace(tmp, path)


def allocate_uid(
    registry: dict[str, int], slug: str, *, start: int, end: int, after: int | None = None
) -> int:
    """Return the uid for ``slug``, allocating a new one if needed. Pure.

    If ``slug`` is already in ``registry``, its existing uid is returned
    unchanged — idempotency the boot migration depends on. Otherwise the
    first value in ``range(start, end + 1)`` not present in
    ``registry.values()`` is returned, searching upward from ``after + 1``
    and wrapping to ``start`` (from ``start`` when ``after`` is None). Does
    not touch disk and does not mutate ``registry``. Raises
    ``UidPoolExhaustedError`` if every value in the range is already taken.
    """
    if slug in registry:
        return registry[slug]
    used = set(registry.values())
    first = start if after is None or not start <= after < end else after + 1
    for uid in [*range(first, end + 1), *range(start, first)]:
        if uid not in used:
            return uid
    raise UidPoolExhaustedError(f"uid pool exhausted ({start}-{end}, {len(registry)} allocated)")


def get_or_create_slug_uid(path: Path, slug: str, *, start: int, end: int) -> int:
    """Load the registry, allocate (or reuse) a uid for ``slug``, save if changed.

    No write on the hit path — an already-registered slug returns without
    rewriting the file.

    Allocation walks the pool round-robin from the last uid handed out
    (recorded in ``<registry>.cursor``), so a just-released uid is the last
    one reused, not the first.
    """
    registry = load_uid_registry(path)
    if slug in registry:
        return registry[slug]
    cursor = _uid_cursor_path(path)
    try:
        after: int | None = int(cursor.read_text())
    except (OSError, ValueError):
        after = None
    uid = allocate_uid(registry, slug, start=start, end=end, after=after)
    registry[slug] = uid
    save_uid_registry(path, registry)
    tmp = cursor.with_suffix(".tmp")
    tmp.write_text(str(uid))
    os.chmod(tmp, _REGISTRY_MODE)
    os.replace(tmp, cursor)
    return uid


def quarantine_slug_uid(path: Path, slug: str) -> None:
    """Drop ``slug`` but keep its uid reserved forever, under a non-slug key.

    For a uid that still had processes when its slug was deleted. ``#`` can't
    appear in a slug (``lifecycle.safe_slug``), so the key never collides, and
    ``allocate_uid`` treats every registry value as taken.
    """
    registry = load_uid_registry(path)
    uid = registry.pop(slug, None)
    if uid is None:
        return
    registry[f"#quarantine-{uid}"] = uid
    save_uid_registry(path, registry)


def _uid_cursor_path(registry_path: Path) -> Path:
    return registry_path.with_name(registry_path.name + ".cursor")


def release_slug_uid(path: Path, slug: str) -> None:
    """Drop a slug's uid from the registry, making it immediately reusable.

    A no-op for a slug that isn't registered — does not raise and does not
    create the file if it didn't already exist.

    The caller MUST have already killed the slug's process before calling
    this (``remove_slug_tree`` does, via ``kill_uid_processes``): the uid
    becomes reusable the instant this returns, and a
    surviving process still holding the old uid would otherwise be able to
    reach whichever slug gets allocated it next. Releasing is required
    rather than optional — edit-mode notebooks are TTL-reaped every
    ``subprocess_ttl_seconds`` (default 24h), so a never-releasing pool
    would burn through its whole range on ordinary churn.
    """
    registry = load_uid_registry(path)
    if registry.pop(slug, None) is not None:
        save_uid_registry(path, registry)


# ─── privilege drop ──────────────────────────────────────────────────────────
#
# The jail's failure policy is the opposite of the rlimit-only preexec in
# lifecycle.py: rlimits warn and degrade (fine for a resource cap), the jail
# fails closed (required for a security control, D-05). Keeping the drop here
# rather than folding it into lifecycle._make_preexec is deliberate — merging
# the two failure policies into one function is exactly how the isolation
# would get silently lost.


class JailUnavailableError(RuntimeError):
    """Raised when the jail cannot be applied and no explicit opt-out is set."""


def can_apply_jail() -> bool:
    """Whether this process can actually drop privilege into another uid.

    The ``os.geteuid() == 0`` check is the operative one, not merely whether
    the platform exposes the privilege-changing syscalls used below: a
    non-root process cannot change its uid to another value regardless of
    platform, so this gate is POSIX-and-root, not Linux-only like the rlimit
    gate in ``lifecycle._make_preexec`` (which only cares about ``RLIMIT_AS``
    reliability, not privilege). The ``hasattr`` checks are evaluated first so
    a non-POSIX platform short-circuits before reaching ``os.geteuid()``,
    which doesn't exist there.
    """
    return hasattr(os, "setuid") and hasattr(os, "setgroups") and os.geteuid() == 0


def resolve_jail_uid(
    uids_file: Path, slug: str, *, start: int, end: int, allow_unjailed: bool
) -> int | None:
    """Resolve the uid a spawn/validate call for ``slug`` should drop into.

    Fail-closed per D-05: when the jail cannot be applied (not root, or this
    platform cannot change process identity), this raises
    ``JailUnavailableError`` unless the caller has explicitly set
    ``allow_unjailed=True`` (the deliberate dev-host opt-out, wired to the
    ``allow_unjailed_spawn`` setting) — in which case it returns ``None`` and
    the caller spawns unjailed rather than refusing.

    ``UidPoolExhaustedError`` from the underlying allocation is allowed to
    propagate unchanged, even when ``allow_unjailed=True``: a full pool must be
    loud, never a silent fall-through to an unjailed spawn.
    """
    if can_apply_jail():
        return get_or_create_slug_uid(uids_file, slug, start=start, end=end)
    if allow_unjailed:
        return None
    raise JailUnavailableError(
        "cannot apply the notebook jail on this host (not root, or this "
        "platform cannot change process identity); refusing to spawn "
        "unjailed. Set allow_unjailed_spawn=True to explicitly opt out on a "
        "dev host."
    )


JAIL_RLIMIT_NPROC = 512
"""Processes (threads count) one jail uid may hold. Caps a fork loop, so
``kill_uid_processes`` can always drain the uid, while leaving room for marimo,
its kernel, uv and a threaded numeric stack."""

_PR_SET_NO_NEW_PRIVS = 38
_prctl: Callable[..., int] | None = None


def _load_prctl() -> None:
    """Resolve libc's ``prctl`` in the parent: dlopen after fork can deadlock."""
    global _prctl
    if _prctl is None:
        _prctl = ctypes.CDLL(None, use_errno=True).prctl


def _set_no_new_privs() -> None:
    """``PR_SET_NO_NEW_PRIVS``: no setuid/file-capability binary can raise privilege."""
    if _prctl is None or _prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")


def build_jailed_preexec(
    uid: int,
    *,
    rlimit_as_bytes: int | None,
    rlimit_cpu_seconds: int | None,
    rlimit_nproc: int = JAIL_RLIMIT_NPROC,
) -> Callable[[], None]:
    """Build the preexec_fn that drops the forked child into ``uid``.

    Always returns a callable — never ``None``. This is a deliberate
    divergence from ``lifecycle._make_preexec``, which returns ``None`` on
    non-Linux or when no rlimits are configured: a jail that silently does not
    apply is exactly the failure mode this plan exists to remove, so there is
    no code path here that can produce a no-op preexec.

    The gid always equals the uid — each slug gets its own primary group of
    the same number, so no separate gid allocation is needed.

    Import ``resource`` here (POSIX-only stdlib), matching ``_make_preexec``'s
    existing guarded-import style.
    """
    import resource  # POSIX-only stdlib; this whole mechanism requires POSIX.

    _load_prctl()

    def _apply() -> None:  # runs in the forked child, before launching marimo
        os.setgroups([])  # MUST precede setgid/setuid — omitting this leaves
        # the child in every supplementary group the host process (root)
        # belongs to, even after setuid/setgid drop the primary identity.
        os.setgid(uid)  # MUST precede setuid — a process cannot change gid
        # once its uid is no longer 0.
        resource.setrlimit(resource.RLIMIT_NPROC, (rlimit_nproc, rlimit_nproc))
        _set_no_new_privs()
        os.setuid(uid)
        if rlimit_as_bytes:
            resource.setrlimit(resource.RLIMIT_AS, (rlimit_as_bytes, rlimit_as_bytes))
        if rlimit_cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (rlimit_cpu_seconds, rlimit_cpu_seconds))

    return _apply

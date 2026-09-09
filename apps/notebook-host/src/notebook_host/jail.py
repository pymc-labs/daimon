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

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SLUG_TREE_MODE = 0o700
"""Mode for each slug's four owned directories (root, data, workspace, home).

This is the real isolation boundary: only the slug's own uid (chowned by
``ensure_slug_jail``) can read, write, or traverse it.
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
particular lists every slug, and per D-04 the slug is the only access control
on the unauthenticated ``/n/{slug}/*`` proxy — reading it hands out every
other notebook's URL."""


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
    )


def ensure_slug_jail(data_dir: Path, slug: str, *, uid: int | None = None) -> SlugPaths:
    """Create (or repair) a slug's whole directory tree at mode 0700.

    Creates ``root``, ``data``, ``workspace``, ``home`` and chmods each to
    0700 unconditionally — ``mkdir``'s ``mode`` argument is masked by umask,
    so an explicit ``chmod`` is required. When ``uid`` is given, each of those
    four directories is also chowned to ``(uid, uid)``.

    Idempotent: safe to call repeatedly on an existing, already-owned tree.
    Does NOT touch ``notebook.py``, ``marimo.log``, or anything already inside
    ``data/`` — file ownership is the write site's job, and files created by
    the jailed marimo process are already owned correctly by construction.

    ``slug`` must already have passed ``lifecycle.safe_slug``.
    """
    paths = get_slug_paths(data_dir, slug)
    dirs = (paths.root, paths.data, paths.workspace, paths.home)
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, SLUG_TREE_MODE)
        if uid is not None:
            os.chown(d, uid, uid)
    return paths


def remove_slug_tree(data_dir: Path, slug: str, *, uids_file: Path | None = None) -> None:
    """Remove a slug's whole directory tree in one call.

    Replaces the unlink-plus-two-rmtrees pattern the flat layout required
    across three call sites — exactly the duplication that let the
    background sweep delete only one of the three on-disk pieces. A no-op
    (does not raise) if the tree doesn't exist.

    When ``uids_file`` is given, the slug's uid is also released from the
    registry (via ``release_slug_uid``) after the tree is gone — keeping uid
    release on the same single code path every delete site already calls,
    rather than adding a fourth thing each caller must remember. Omitted by
    default so this plan changes no caller's behaviour.

    ``slug`` must already have passed ``lifecycle.safe_slug``.
    """
    shutil.rmtree(get_slug_paths(data_dir, slug).root, ignore_errors=True)
    if uids_file is not None:
        release_slug_uid(uids_file, slug)


def clear_slug_uv_cache(data_dir: Path, slug: str) -> None:
    """Remove a slug's ``home/.cache/uv`` without touching the rest of the tree.

    Every failed sandboxed spawn leaves a fresh ephemeral uv environment in
    the slug's cache, so a blog stuck in a spawn/kill/retry loop grows it
    without bound until the data volume fills. Clearing on failure bounds the
    leak at the cost of a cold install on the next attempt; a successful
    spawn keeps its warm cache. A no-op (does not raise) if the cache doesn't
    exist. ``slug`` must already have passed ``lifecycle.safe_slug``.
    """
    shutil.rmtree(get_slug_paths(data_dir, slug).home / ".cache" / "uv", ignore_errors=True)


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


def allocate_uid(registry: dict[str, int], slug: str, *, start: int, end: int) -> int:
    """Return the uid for ``slug``, allocating a new one if needed. Pure.

    If ``slug`` is already in ``registry``, its existing uid is returned
    unchanged — idempotency the boot migration depends on. Otherwise the
    lowest value in ``range(start, end + 1)`` not present in
    ``registry.values()`` is returned. Does not touch disk and does not
    mutate ``registry``. Raises ``UidPoolExhaustedError`` if every value in
    the range is already taken.
    """
    if slug in registry:
        return registry[slug]
    used = set(registry.values())
    for uid in range(start, end + 1):
        if uid not in used:
            return uid
    raise UidPoolExhaustedError(f"uid pool exhausted ({start}-{end}, {len(registry)} allocated)")


def get_or_create_slug_uid(path: Path, slug: str, *, start: int, end: int) -> int:
    """Load the registry, allocate (or reuse) a uid for ``slug``, save if changed.

    No write on the hit path — an already-registered slug returns without
    rewriting the file.
    """
    registry = load_uid_registry(path)
    if slug in registry:
        return registry[slug]
    uid = allocate_uid(registry, slug, start=start, end=end)
    registry[slug] = uid
    save_uid_registry(path, registry)
    return uid


def release_slug_uid(path: Path, slug: str) -> None:
    """Drop a slug's uid from the registry, making it immediately reusable.

    A no-op for a slug that isn't registered — does not raise and does not
    create the file if it didn't already exist.

    The caller MUST have already killed the slug's process before calling
    this: the uid becomes reusable the instant this returns, and a
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


def build_jailed_preexec(
    uid: int, *, rlimit_as_bytes: int | None, rlimit_cpu_seconds: int | None
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

    def _apply() -> None:  # runs in the forked child, before launching marimo
        os.setgroups([])  # MUST precede setgid/setuid — omitting this leaves
        # the child in every supplementary group the host process (root)
        # belongs to, even after setuid/setgid drop the primary identity.
        os.setgid(uid)  # MUST precede setuid — a process cannot change gid
        # once its uid is no longer 0.
        os.setuid(uid)
        if rlimit_as_bytes:
            resource.setrlimit(resource.RLIMIT_AS, (rlimit_as_bytes, rlimit_as_bytes))
        if rlimit_cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (rlimit_cpu_seconds, rlimit_cpu_seconds))

    return _apply

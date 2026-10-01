"""Crash-safe, idempotent migration from the flat on-disk layout to the nested one.

Every release before the per-slug jail wrote a flat layout directly under
``data_dir``: ``<slug>.py``, ``<slug>.data/`` (attachments), ``<slug>_workspace/``
(disposable), and ``<slug>.marimo.log``, siblings of ``blogs.json``/``pids.json``.
The jail needs each slug isolated in its own ``0700``-owned subtree
(``data_dir/<slug>/{notebook.py, data/, workspace/, home/, marimo.log}``), so an
existing deployment's ``data_dir`` has to be moved onto that shape before
anything reads or spawns from it.

The host can be SIGKILLed at any point — a deploy replaces the container out
from under it — so this has to converge to the fully-migrated state no matter
which step a previous run died at, and running it again once already migrated
must be a no-op. The mechanism: build each slug's new tree in a staging
directory (``data_dir/.migrating-<slug>``), moving the notebook source in
*last* so its presence in staging is a single reliable marker meaning
"everything else already moved in", then ``os.rename`` (atomic on the same
filesystem, same guarantee ``pids_store.save_pids`` already relies on) the
staging directory onto its final name.

Nothing here imports ``Settings``; the caller reads config and passes values,
matching ``jail.py``'s posture.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from fastapi import HTTPException

from notebook_host.jail import (
    SLUG_TREE_MODE,
    lock_data_dir_root,
    lock_registry_file,
    resolve_jail_uid,
)
from notebook_host.lifecycle import safe_slug

_log = logging.getLogger(__name__)

_STAGING_PREFIX = ".migrating-"


def migrate_flat_layout(
    data_dir: Path,
    *,
    uids_file: Path,
    uid_start: int,
    uid_end: int,
    allow_unjailed: bool,
    registry_files: Sequence[Path] = (),
) -> list[str]:
    """Move every flat legacy slug under ``data_dir`` onto the nested layout.

    Returns the slugs migrated by this call (empty once the tree is already
    fully migrated). Must be called before anything else touches ``data_dir``
    — nothing here is safe to run concurrently with a spawn or a request.

    ``data_dir`` itself is locked to ``jail.DATA_DIR_MODE`` (0711 — traversable,
    not listable) as the very first step, before any per-slug directory is
    created or chowned, so there is never a window where a freshly jailed
    slug sits inside a still-unlocked parent alongside not-yet-migrated flat
    siblings.

    ``registry_files`` are host-owned registry paths (``blogs.json``,
    ``pids.json``, ...) that this host may have INHERITED from a release
    predating the jail. They are locked in the same first step as ``data_dir``,
    because the boot that makes ``data_dir`` traversable is exactly the boot
    that must stop relying on an unlistable parent to hide them. Files this
    host writes itself are created 0600 by their store (``files.write_private_file``).

    ``UidPoolExhaustedError`` and ``JailUnavailableError`` (from
    ``resolve_jail_uid``) are allowed to propagate uncaught: a migration that
    cannot isolate every legacy slug must abort the boot rather than leave
    some of them unjailed.
    """
    lock_data_dir_root(data_dir)
    for registry in registry_files:
        lock_registry_file(registry)

    migrated: list[str] = []

    # Pass 1 — resume any staging directory left by an interrupted run.
    for staging in sorted(data_dir.glob(f"{_STAGING_PREFIX}*")):
        slug = staging.name.removeprefix(_STAGING_PREFIX)
        final = data_dir / slug
        if final.exists():
            # The final tree already won the race; the staging copy is a
            # leftover from a run that got all the way to promote and then
            # died before it could clean up, or from external tampering.
            shutil.rmtree(staging)
            continue
        if (staging / "notebook.py").exists():
            # Killed after the source moved in but before the promote. The
            # tree is otherwise complete (attachments, workspace, home all
            # moved in before the source, by construction of pass 2 below),
            # so finish exactly like pass 2 would: resolve the uid, chown,
            # promote.
            _finish_staging(staging, final, slug, uids_file, uid_start, uid_end, allow_unjailed)
            migrated.append(slug)
        # else: leave the staging directory exactly where it is. It may
        # already hold attachments moved in from the flat layout while the
        # flat `<slug>.py` still exists — pass 2 will complete this same
        # staging directory rather than starting over.

    # Pass 2 — migrate remaining flat sources.
    for source in sorted(data_dir.glob("*.py")):
        slug = source.stem
        try:
            safe_slug(slug)
        except HTTPException:
            _log.warning("skipping %s: %r is not a valid slug, leaving it in place", source, slug)
            continue

        final = data_dir / slug
        if final.exists():
            _log.warning(
                "both a flat source %s and a nested directory %s exist; "
                "leaving the flat file for an operator to inspect",
                source,
                final,
            )
            continue

        staging = data_dir / f"{_STAGING_PREFIX}{slug}"
        staging.mkdir(exist_ok=True)
        os.chmod(staging, SLUG_TREE_MODE)

        for name in ("workspace", "home"):
            d = staging / name
            d.mkdir(exist_ok=True)
            os.chmod(d, SLUG_TREE_MODE)

        data = staging / "data"
        legacy_data = data_dir / f"{slug}.data"
        if not data.exists() and legacy_data.exists():
            os.rename(legacy_data, data)
        elif not data.exists():
            data.mkdir()
        os.chmod(data, SLUG_TREE_MODE)

        # Last: the source move is what marks staging as "complete" for pass
        # 1's resume predicate above, so it must happen only after every
        # other piece of the tree is already in place.
        os.rename(source, staging / "notebook.py")

        _finish_staging(staging, final, slug, uids_file, uid_start, uid_end, allow_unjailed)
        migrated.append(slug)

    # Pass 3 — drop the disposable flat leftovers. Both are regenerated on
    # the next spawn, and neither glob can match the nested equivalents
    # (`data_dir/<slug>/workspace`, `data_dir/<slug>/marimo.log`), so this is
    # idempotent by construction.
    for workspace_dir in data_dir.glob("*_workspace"):
        shutil.rmtree(workspace_dir, ignore_errors=True)
    for log_file in data_dir.glob("*.marimo.log"):
        log_file.unlink(missing_ok=True)

    if migrated:
        _log.info("migrated %d legacy slug(s) to the nested layout: %s", len(migrated), migrated)
    return migrated


def _finish_staging(
    staging: Path,
    final: Path,
    slug: str,
    uids_file: Path,
    uid_start: int,
    uid_end: int,
    allow_unjailed: bool,
) -> None:
    """Resolve the slug's uid, chown the staged tree, and promote it into place.

    The legacy `.data/` contents are host-owned files the jailed process must
    be able to read, so a recursive chown is required here — the one place it
    is: `ensure_slug_jail` deliberately only owns the four directories, since
    everywhere else a slug's files are created by the already-uid-owned
    process itself.
    """
    uid = resolve_jail_uid(
        uids_file, slug, start=uid_start, end=uid_end, allow_unjailed=allow_unjailed
    )
    if uid is not None:
        _chown_recursive(staging, uid)
    os.rename(staging, final)
    _log.info("migrated legacy slug %r to the nested layout", slug)


def _chown_recursive(root: Path, uid: int) -> None:
    for path in root.rglob("*"):
        os.chown(path, uid, uid, follow_symlinks=False)
    os.chown(root, uid, uid)

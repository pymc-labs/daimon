"""Private, atomic, no-follow file writes for everything the host persists."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path


def write_private_file(
    path: Path, content: bytes, *, owner_uid: int | None = None, mode: int = 0o600
) -> None:
    """Atomically replace ``path`` with ``content``, private from the first byte.

    The tmp file is created ``O_CREAT | O_EXCL | O_NOFOLLOW`` with ``mode``
    already applied, so there is no moment, under any umask, when another uid
    can open it (``data_dir`` is traversable and the tmp names are
    predictable). A stale tmp is removed first, never opened; a symlink an
    attacker planted there or at ``path`` is never written through, because the
    content goes into the new fd and a rename replaces a link rather than
    following it. Ownership (``owner_uid``) is set through the fd before the
    rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    _remove_stale(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        try:
            os.fchmod(fd, mode)
            if owner_uid is not None:
                os.fchown(fd, owner_uid, owner_uid)
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _remove_stale(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()

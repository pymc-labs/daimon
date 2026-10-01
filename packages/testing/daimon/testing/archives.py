"""Tarball builder for GitHub-archive fakes (skill sync, repo bundling)."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Mapping


def make_tarball(files: Mapping[str, bytes], *, mtime: int | None = None) -> bytes:
    """A gzip tarball holding `files` as {tar-internal-path: content}.

    Entries are written in mapping order. `mtime` (seconds since the epoch)
    stamps every entry when given; otherwise entries keep tarfile's zero
    default, so two calls with the same input are byte-identical.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            if mtime is not None:
                info.mtime = mtime
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()

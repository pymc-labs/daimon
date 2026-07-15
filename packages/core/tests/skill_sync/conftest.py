"""Shared fixtures for skill_sync tests."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable

import pytest


@pytest.fixture
def make_tarball() -> Callable[[dict[str, bytes]], bytes]:
    """Factory: dict[path, bytes] -> tar.gz bytes (uncompressed mtime stable)."""

    def _make(files: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for path, content in files.items():
                info = tarfile.TarInfo(name=path)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    return _make

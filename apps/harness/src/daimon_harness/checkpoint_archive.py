"""Bounded, link-free workspace archives, also run under the session tool UID."""

from __future__ import annotations

import os
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

MAX_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 50_000


class LimitedReader:
    """Bound decoded tar bytes, including metadata consumed inside tarfile itself."""

    def __init__(self, source: BinaryIO, limit: int) -> None:
        self.source, self.remaining = source, limit

    def read(self, size: int = -1) -> bytes:
        size = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        data = self.source.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError("checkpoint exceeds decoded archive limit")
        return data


def export_work(root: Path, output: BinaryIO) -> None:
    size = count = 0

    def visit(archive: tarfile.TarFile, directory: int, prefix: str) -> None:
        nonlocal size, count
        for name in sorted(os.listdir(directory)):
            count += 1
            if count > MAX_ENTRIES:
                raise ValueError("workspace checkpoint has too many entries")
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            path = prefix + name
            member = tarfile.TarInfo(path)
            member.mode = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
            member.mtime = int(info.st_mtime)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    member.type = tarfile.DIRTYPE
                    archive.addfile(member)
                    visit(archive, child, path + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                # The UID boundary and descriptor-relative opens prevent a tool
                # swapping an ancestor to a service-readable symlink during export.
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as source:
                    opened = os.fstat(source.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise ValueError("workspace file changed type")
                    size += opened.st_size
                    if size > MAX_BYTES:
                        raise ValueError("workspace checkpoint exceeds 512 MiB")
                    member.size = opened.st_size
                    archive.addfile(member, source)
            else:
                raise ValueError("workspace checkpoints require regular files and directories")

    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with tarfile.open(fileobj=output, mode="w|") as archive:
            visit(archive, directory, "")
    finally:
        os.close(directory)


def restore_work(source: BinaryIO, target: Path) -> None:
    """Extract only bounded regular files and directories into a private empty dir."""
    size = 0
    seen: set[str] = set()
    target.mkdir(mode=0o700)
    bounded = cast(BinaryIO, LimitedReader(source, MAX_BYTES + 64 * 1024 * 1024))
    with tarfile.open(fileobj=bounded, mode="r|") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if (
                not parts
                or member.name.startswith("/")
                or ".." in parts
                or "\\" in member.name
                or member.name in seen
                or not (member.isfile() or member.isdir())
                or member.size < 0
                or (member.isdir() and member.size != 0)
            ):
                raise ValueError("unsafe workspace checkpoint entry")
            seen.add(member.name)
            size += member.size
            if size > MAX_BYTES or len(seen) > MAX_ENTRIES:
                raise ValueError("workspace checkpoint exceeds limits")
            path = target.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if member.isdir():
                path.mkdir(exist_ok=True, mode=0o700)
            else:
                content = archive.extractfile(member)
                if content is None:
                    raise ValueError("checkpoint file is missing")
                with path.open("xb") as output:
                    while chunk := content.read(1024 * 1024):
                        output.write(chunk)
                path.chmod(0o600)


if __name__ == "__main__":
    export_work(Path(sys.argv[1]), sys.stdout.buffer)

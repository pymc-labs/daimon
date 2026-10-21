"""Bounded, link-free workspace archives, also run under the session tool UID."""

from __future__ import annotations

import json
import os
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

MAX_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 50_000
MAX_DEPTH = 32
MAX_REPORT_BYTES = 32 * 1024


def valid_name(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and not name.startswith("/") and ".." not in parts and "\\" not in name


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


def _export_work(root: Path, output: BinaryIO, staged: BinaryIO) -> dict[str, object]:
    size = count = files = skipped_count = 0
    skipped: list[dict[str, str]] = []

    def skip(path: str, reason: str) -> None:
        nonlocal skipped_count
        skipped_count += 1
        sample = {"path": path[:512], "reason": reason}
        if (
            len(skipped) < 100
            and len(json.dumps(skipped + [sample]).encode("utf-8")) < MAX_REPORT_BYTES
        ):
            skipped.append(sample)

    def visit(archive: tarfile.TarFile, directory: int, prefix: str, depth: int) -> None:
        nonlocal size, count, files
        if depth > MAX_DEPTH:
            skip(prefix, "depth_cap")
            return
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ENTRIES:
                    skip(prefix, "entry_cap")
                    return
                name, path = entry.name, prefix + entry.name
                if not valid_name(path):
                    skip(path, "unsupported_name")
                    continue
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    member = tarfile.TarInfo(path)
                    member.mode = (
                        0o700 if stat.S_ISDIR(info.st_mode) else 0o600 | (info.st_mode & 0o100)
                    )
                    member.mtime = int(info.st_mtime)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(
                            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                        )
                        try:
                            member.type = tarfile.DIRTYPE
                            archive.addfile(member)
                            visit(archive, child, path + "/", depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_size + size > MAX_BYTES:
                            skip(path, "size_cap")
                            continue
                        fd = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                        )
                        with os.fdopen(fd, "rb") as source:
                            staged.seek(0)
                            staged.truncate()
                            opened = os.fstat(source.fileno())
                            if not stat.S_ISREG(opened.st_mode):
                                skip(path, "changed_type")
                                continue
                            if opened.st_size + size > MAX_BYTES:
                                skip(path, "size_cap")
                                continue
                            # Stage each file before its tar header. An unreadable
                            # or shrinking file cannot corrupt the remaining archive.
                            remaining = opened.st_size
                            while remaining:
                                chunk = source.read(min(remaining, 1024 * 1024))
                                if not chunk:
                                    break
                                staged.write(chunk)
                                remaining -= len(chunk)
                            if remaining:
                                skip(path, "changed_during_export")
                                continue
                            staged.seek(0)
                            member.size = opened.st_size
                            archive.addfile(member, staged)
                            size += opened.st_size
                            files += 1
                    else:
                        skip(path, "symlink" if stat.S_ISLNK(info.st_mode) else "unsupported_type")
                except OSError:
                    skip(path, "unreadable")

    with tarfile.open(fileobj=output, mode="w|") as archive:
        try:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            skip(".", "unreadable")
        else:
            try:
                visit(archive, directory, "", 0)
            finally:
                os.close(directory)
    return {
        "degraded": bool(skipped_count),
        "skipped": skipped,
        "skipped_count": skipped_count,
        "included_bytes": size,
        "included_files": files,
    }


def export_work(root: Path, output: BinaryIO, staged: BinaryIO | None = None) -> dict[str, object]:
    if staged is not None:
        return _export_work(root, output, staged)
    # Direct callers own this parent. Production passes a pre-opened descriptor
    # inside the service-owned snapshot directory on the workspace filesystem.
    with tempfile.TemporaryFile(dir=root.parent) as scratch:
        return _export_work(root, output, scratch)


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
                not valid_name(member.name)
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
                path.chmod(0o600 | (member.mode & 0o100))


if __name__ == "__main__":
    if len(sys.argv) > 2:
        with os.fdopen(int(sys.argv[2]), "w+b") as staged:
            report = export_work(Path(sys.argv[1]), sys.stdout.buffer, staged)
    else:
        report = export_work(Path(sys.argv[1]), sys.stdout.buffer)
    sys.stderr.write(json.dumps(report))

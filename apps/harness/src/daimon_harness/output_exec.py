"""List, read and delete session output files, run only as the tool identity.

Every step opens by descriptor with O_NOFOLLOW from the session home, so a
symlink, hard link or mount inside `work/outputs` never reaches another file.
"""

from __future__ import annotations

import base64
import json
import os
import resource
import stat
import sys
from contextlib import suppress
from pathlib import Path

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MAX_ENTRIES = 2048


class Refused(Exception):
    """A safe failure code; paths and contents never leave this process."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def version(info: os.stat_result) -> str:
    return f"{info.st_mtime_ns}-{info.st_size}-{info.st_ino}"


def deliverable(info: os.stat_result, device: int) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_dev == device


def open_outputs(home: Path) -> int | None:
    descriptor = os.open(home, DIRECTORY)
    try:
        for name in ("work", "outputs"):
            try:
                child = os.open(name, DIRECTORY, dir_fd=descriptor)
            except FileNotFoundError:
                return None
            except OSError:
                raise Refused("refused") from None
            os.close(descriptor)
            descriptor = child
        result, descriptor = descriptor, -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def walk(root: int, limit: int) -> tuple[list[dict[str, object]], bool]:
    device = os.fstat(root).st_dev
    found: list[dict[str, object]] = []
    pending: list[tuple[int, str]] = [(os.dup(root), "")]
    seen = 0
    truncated = False
    try:
        while pending:
            descriptor, prefix = pending.pop()
            try:
                for name in sorted(os.listdir(descriptor)):
                    seen += 1
                    if seen > MAX_ENTRIES or len(found) >= limit:
                        truncated = True
                        return found, truncated
                    if "\x00" in name or "\\" in name or name in {".", ".."}:
                        continue
                    with suppress(FileNotFoundError):
                        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        path = f"{prefix}{name}"
                        if stat.S_ISDIR(info.st_mode) and info.st_dev == device:
                            with suppress(OSError):
                                pending.append(
                                    (os.open(name, DIRECTORY, dir_fd=descriptor), f"{path}/")
                                )
                        elif deliverable(info, device):
                            found.append(
                                {
                                    "path": path,
                                    "size_bytes": info.st_size,
                                    "modified_ns": info.st_mtime_ns,
                                    "version": version(info),
                                }
                            )
            finally:
                os.close(descriptor)
        return found, truncated
    finally:
        for descriptor, _ in pending:
            os.close(descriptor)


def open_parent(root: int, path: str) -> tuple[int, str]:
    parts = path.split("/")
    if not path or any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise Refused("refused")
    descriptor = os.dup(root)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, DIRECTORY, dir_fd=descriptor)
            except FileNotFoundError:
                raise Refused("not_found") from None
            except OSError:
                raise Refused("refused") from None
            os.close(descriptor)
            descriptor = child
        result, descriptor = descriptor, -1
        return result, parts[-1]
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read(root: int, path: str, expected: str, max_bytes: int) -> dict[str, object]:
    device = os.fstat(root).st_dev
    parent, name = open_parent(root, path)
    try:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            raise Refused("not_found") from None
        except OSError:
            raise Refused("refused") from None
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not deliverable(info, device):
                raise Refused("refused")
            if version(info) != expected:
                raise Refused("changed")
            if info.st_size > max_bytes:
                raise Refused("too_large")
            content = file.read(info.st_size + 1)
        if len(content) != info.st_size:
            raise Refused("changed")
        return {"size_bytes": len(content), "content_b64": base64.b64encode(content).decode()}
    finally:
        os.close(parent)


def delete(root: int, path: str, expected: str) -> dict[str, object]:
    device = os.fstat(root).st_dev
    parent, name = open_parent(root, path)
    try:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            raise Refused("not_found") from None
        except OSError:
            raise Refused("refused") from None
        try:
            info = os.fstat(descriptor)
            if not deliverable(info, device):
                raise Refused("refused")
            if version(info) != expected:
                raise Refused("changed")
            # Linux has no unlink by descriptor: re-check the name still holds the
            # pinned file just before unlinking. A rename swap between this check and
            # the unlink can still remove another file in the same outputs tree.
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                raise Refused("not_found") from None
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise Refused("changed")
            os.unlink(name, dir_fd=parent)
        finally:
            os.close(descriptor)
        return {"deleted": True}
    finally:
        os.close(parent)


def operate(home: Path, operation: str, path: str, version: str, limit: int) -> dict[str, object]:
    try:
        root = open_outputs(home.absolute())
    except Refused:
        # A linked or replaced outputs directory delivers nothing.
        if operation != "list":
            raise
        root = None
    if root is None:
        if operation == "list":
            return {"outputs": [], "truncated": False}
        raise Refused("not_found")
    try:
        if operation == "list":
            found, truncated = walk(root, limit)
            return {"outputs": found, "truncated": truncated}
        if operation == "read":
            return read(root, path, version, limit)
        if operation == "delete":
            return delete(root, path, version)
        raise Refused("refused")
    finally:
        os.close(root)


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    try:
        payload = json.load(sys.stdin)
        print(json.dumps({"result": operate(Path(sys.argv[1]), **payload)}))
    except Refused as exc:
        print(json.dumps({"error": exc.code}))
    except Exception:
        print(json.dumps({"error": "refused"}))


if __name__ == "__main__":
    main()

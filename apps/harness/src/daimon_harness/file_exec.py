"""Bounded file operations, executed only after dropping to the tool identity."""

from __future__ import annotations

import json
import os
import resource
import stat
import sys
from contextlib import suppress
from pathlib import Path


def open_parent(root: Path, parts: tuple[str, ...], *, create: bool) -> tuple[int, str]:
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid file path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def operate(home: Path, operation: str, path: str, content: str, old: str, new: str) -> str:
    home = home.absolute()
    work = home / "work"
    target = Path(os.path.abspath(work / path))
    roots = (work, home / "skills") if operation == "read" else (work,)
    root = next((root for root in roots if target.is_relative_to(root)), None)
    if root is None:
        raise ValueError("path is outside workspace and skills")
    parent, name = open_parent(root, target.relative_to(root).parts, create=operation == "write")
    descriptor = -1
    try:
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_RDONLY if operation == "read" else os.O_RDWR
        if operation == "write":
            flags |= os.O_CREAT
        descriptor = os.open(name, flags, 0o600, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("file tools require a regular file")
        if operation != "read":
            os.fchown(descriptor, os.geteuid(), os.getegid())
        with os.fdopen(descriptor, "r" if operation == "read" else "r+", encoding="utf-8") as file:
            descriptor = -1
            if operation == "read":
                return file.read(100_000)
            if operation == "edit":
                if info.st_size > 1_048_576:
                    raise ValueError("edit file exceeds 1 MiB")
                original = file.read(1_048_577)
                if not old or original.count(old) != 1:
                    raise ValueError("old text must occur exactly once")
                content = original.replace(old, new, 1)
            elif operation != "write":
                raise ValueError("unknown file operation")
            file.seek(0)
            file.write(content)
            file.truncate()
        return "edited" if operation == "edit" else "written"
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    try:
        payload = json.load(sys.stdin)
        output = operate(Path(sys.argv[1]), **payload)
        print(json.dumps({"output": output}))
    except Exception:
        # Paths and file contents can carry secrets; callers need only a safe failure.
        print(json.dumps({"error": "File operation refused or failed"}))
        sys.exit(1)


if __name__ == "__main__":
    main()

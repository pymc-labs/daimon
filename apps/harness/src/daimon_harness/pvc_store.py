"""Atomic, fsync-before-ack object storage for controller-owned checkpoints."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import httpx

from .checkpoints import MAX_ARCHIVE_BYTES, sync_directory


class PVCStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, name: str) -> Path:
        parts = PurePosixPath(name).parts
        if not parts or any(p in {"..", "."} for p in parts) or name.startswith("/"):
            raise ValueError("invalid object name")
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("invalid object path")
        return path

    def list(self, prefix: str) -> list[str]:
        path = self.path(prefix)
        # Checkpoints always have sessions/<id>/ prefixes. Never scan other sessions.
        parts = Path(prefix).parts
        base = self.root.joinpath(*parts[:2]) if len(parts) >= 2 else path
        return sorted(
            str(p.relative_to(self.root))
            for p in base.rglob("*")
            if p.is_file()
            and not p.name.startswith(".pending-")
            and str(p.relative_to(self.root)).startswith(prefix)
        )

    def upload(self, name: str, source: BinaryIO) -> None:
        target = self.path(name)
        missing: list[Path] = []
        parent = target.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            sync_directory(directory.parent)
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("object too large")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            sync_directory(target.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def download(self, name: str, target: BinaryIO) -> None:
        with self.path(name).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)

    def delete(self, name: str) -> None:
        target = self.path(name)
        target.unlink(missing_ok=True)
        if target.parent.exists():
            sync_directory(target.parent)

    def close(self) -> None:
        pass


class ControllerStore:
    """A pod capability is captured before service UID drop and env scrubbing."""

    def __init__(self, url: str, token: str):
        import ssl

        if not url.startswith("https://"):
            raise ValueError("controller checkpoints require HTTPS")
        context = ssl.create_default_context(cafile=os.environ.get("HARNESS_CONTROLLER_CA"))
        self.client = httpx.Client(
            verify=context,
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            trust_env=False,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        source: BinaryIO | None = None,
    ) -> httpx.Response:
        import time

        offset = source.tell() if source is not None else None
        for attempt in range(3):
            if source is not None and offset is not None:
                source.seek(offset)
            response = self.client.request(method, path, params=params, content=source)
            if response.status_code != 429 or attempt == 2:
                return response
            response.close()
            time.sleep(1)
        raise RuntimeError("unreachable")

    def list(self, prefix: str) -> list[str]:
        response = self.request("GET", "/internal/checkpoints", params={"prefix": prefix})
        response.raise_for_status()
        return response.json()

    def upload(self, name: str, source: BinaryIO) -> None:
        response = self.request("PUT", "/internal/checkpoints/" + name, source=source)
        response.raise_for_status()

    def download(self, name: str, target: BinaryIO) -> None:
        import time

        for attempt in range(3):
            with self.client.stream("GET", "/internal/checkpoints/" + name) as response:
                if response.status_code == 429 and attempt < 2:
                    time.sleep(1)
                    continue
                response.raise_for_status()
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("object too large")
                    target.write(chunk)
                return

    def delete(self, name: str) -> None:
        response = self.request("DELETE", "/internal/checkpoints/" + name)
        response.raise_for_status()

    def close(self) -> None:
        self.client.close()

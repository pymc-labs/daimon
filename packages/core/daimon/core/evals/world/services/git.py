"""Git over smart HTTP: `git http-backend` over a bare copy of the case's repo bundle, one copy
per item, so a push lands in that item's copy only.

The local copy is a cache: after a push its new state is bundled and kept in the item's
overlay, so a restart or another replica rebuilds the same state. `forget` drops an item's
copies once it ends.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import uuid
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from daimon.core.evals.format import BlobRef, Effect, Repo
from daimon.core.evals.world.model import HttpCall, ItemRef

_SUFFIX: Final = re.compile(r"(/info/refs|/git-upload-pack|/git-receive-pack)$")
_COMMAND: Final = re.compile(rb"([0-9a-f]{40}) ([0-9a-f]{40}) (refs/[^\x00\n]+)")
_OK: Final = re.compile(rb"ok (refs/[^\x00\n]+)")
_MARKER: Final = "daimon-bundle"  # the sha256 of the bundle a local copy holds
_COMMANDS_MAX: Final = 1 << 20  # push commands come first; the pack after them is not read

#: Loads a blob of the item's tenant by sha256.
LoadBlob = Callable[[ItemRef, str], Awaitable[bytes]]


@dataclass(frozen=True)
class GitReply:
    status: int
    headers: dict[str, str]
    body: bytes
    effects: tuple[Effect, ...]
    snapshot: bytes | None = None  # the repo as a bundle after a push changed it


def _repo_id(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/").removesuffix(".git")
    return f"{(parts.hostname or '').lower()}{path}"


def find_repo(item: ItemRef, call: HttpCall) -> tuple[int, Repo, str] | None:
    """The case repo a smart-HTTP call is for (its index and itself), and the git path."""
    found = _SUFFIX.search(call.path)
    if found is None:
        return None
    wanted = _repo_id(f"//{call.host}{call.path[: found.start()]}")
    repos = item.case.world.repos or []
    index = next((i for i, r in enumerate(repos) if _repo_id(r.url) == wanted), None)
    return (index, repos[index], found[1]) if index is not None else None


def pushes(service_path: str, call: HttpCall) -> bool:
    return service_path == "/git-receive-pack" or "service=git-receive-pack" in urlsplit(
        call.url
    ).query.split("&")


def _pkt_payload(body: bytes, encoding: str | None) -> bytes:
    if encoding != "gzip":
        return body[:_COMMANDS_MAX]
    try:
        return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body, _COMMANDS_MAX)
    except zlib.error:
        return b""


def effects_of(
    repo: Repo, service_path: str, call: HttpCall, reply: bytes = b""
) -> tuple[Effect, ...]:
    """What the call did; a push's refs count only when the server reported them ok."""
    target = f"git:{_repo_id(repo.url)}"
    if call.method != "POST":
        return ()
    payload = _pkt_payload(call.body, call.header("content-encoding"))
    if service_path == "/git-receive-pack":
        accepted = set(_OK.findall(reply))
        return tuple(
            Effect(
                verb="git.repo.push",
                target=target,
                args={"ref": ref.decode(), "old": old.decode(), "new": new.decode()},
            )  # fmt: skip
            for old, new, ref in _COMMAND.findall(payload)
            if ref in accepted
        )
    if service_path == "/git-upload-pack" and b"want " in payload:
        verb = "git.repo.fetch" if b"have " in payload else "git.repo.clone"
        return (Effect(verb=verb, target=target),)
    return ()


async def _run(*args: str, cwd: Path | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{args[0]} {args[1]} failed: {err.decode(errors='replace')[:500]}")


@dataclass
class GitService:
    """Per-item bare repos under `root/<item>/<index>.git`, made from bundles on first use."""

    root: Path
    load_blob: LoadBlob
    _locks: dict[Path, asyncio.Lock] = field(default_factory=dict[Path, asyncio.Lock])

    def path(self, item: ItemRef, index: int) -> Path:
        base = (self.root / str(item.id)).resolve()
        path = (base / f"{index}.git").resolve()
        if not path.is_relative_to(base):
            raise ValueError("a repo copy outside its item's folder")
        return path

    def forget(self, item_id: uuid.UUID) -> None:
        """Drop an ended item's local copies."""
        base = self.root / str(item_id)
        for path in [p for p in self._locks if p.parent == base.resolve()]:
            del self._locks[path]
        shutil.rmtree(base, ignore_errors=True)

    async def _ensure(self, item: ItemRef, path: Path, repo: Repo, bundle: BlobRef) -> None:
        marker = path / _MARKER
        if marker.exists() and marker.read_text() == bundle.sha256:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        content = await self.load_blob(item, bundle.sha256)
        with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
            source = Path(scratch) / "repo.bundle"
            source.write_bytes(content)
            staged = Path(scratch) / "repo.git"
            await _run("git", "clone", "--quiet", "--mirror", str(source), str(staged))
            await _run("git", "config", "http.receivepack", "true", cwd=staged)
            head = f"refs/heads/{repo.default_branch}"
            await _run("git", "symbolic-ref", "HEAD", head, cwd=staged)
            (staged / _MARKER).write_text(bundle.sha256)
            shutil.rmtree(path, ignore_errors=True)
            staged.rename(path)

    async def _snapshot(self, path: Path) -> bytes:
        with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
            out = Path(scratch) / "repo.bundle"
            await _run("git", "bundle", "create", "--quiet", str(out), "--all", cwd=path)
            return out.read_bytes()

    async def serve(
        self, item: ItemRef, index: int, repo: Repo, bundle: BlobRef, service_path: str,
        call: HttpCall,
    ) -> GitReply:  # fmt: skip
        """Answer from the copy of the state in `bundle`; a push's new state comes back as
        `snapshot`, which the caller keeps."""
        path = self.path(item, index)
        async with self._locks.setdefault(path, asyncio.Lock()):
            await self._ensure(item, path, repo, bundle)
            reply = await self._backend(path, service_path, call)
            effects = (
                effects_of(repo, service_path, call, reply.body) if reply.status == 200 else ()
            )
            snapshot = None
            if any(e.verb == "git.repo.push" for e in effects):
                snapshot = await self._snapshot(path)
                (path / _MARKER).write_text(hashlib.sha256(snapshot).hexdigest())
        return GitReply(reply.status, reply.headers, reply.body, effects, snapshot)

    @staticmethod
    async def _backend(path: Path, service_path: str, call: HttpCall) -> GitReply:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": str(path.parent),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": f"/{path.name}{service_path}",
            "REQUEST_METHOD": call.method,
            "QUERY_STRING": urlsplit(call.url).query,
            "CONTENT_TYPE": call.header("content-type") or "",
            "CONTENT_LENGTH": str(len(call.body)),
            "REMOTE_USER": "eval",
            "REMOTE_ADDR": "127.0.0.1",
        }
        for name in ("content-encoding", "git-protocol"):
            if (value := call.header(name)) is not None:
                env[f"HTTP_{name.upper().replace('-', '_')}"] = value
        process = await asyncio.create_subprocess_exec(
            "git", "http-backend", env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        out, _ = await process.communicate(call.body)
        head, _, body = out.partition(b"\r\n\r\n")
        status, headers = 200, dict[str, str]()
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            elif name:
                headers[name.strip()] = value.strip()
        return GitReply(status, headers, body, ())

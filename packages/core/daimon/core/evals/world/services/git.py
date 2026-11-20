"""Git over smart HTTP: `git http-backend` over a bare copy of the case's repo bundle, one copy
per item, so a push lands in that item's copy only."""

from __future__ import annotations

import asyncio
import gzip
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from daimon.core.evals.format import Effect, Repo
from daimon.core.evals.world.model import HttpCall, ItemRef

_SUFFIX: Final = re.compile(r"(/info/refs|/git-upload-pack|/git-receive-pack)$")
_COMMAND: Final = re.compile(rb"([0-9a-f]{40}) ([0-9a-f]{40}) (refs/[^\x00\n]+)")

#: Loads a blob of the item's tenant by sha256.
LoadBlob = Callable[[ItemRef, str], Awaitable[bytes]]


@dataclass(frozen=True)
class GitReply:
    status: int
    headers: dict[str, str]
    body: bytes
    effects: tuple[Effect, ...]


def _repo_id(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/").removesuffix(".git")
    return f"{(parts.hostname or '').lower()}{path}"


def find_repo(item: ItemRef, call: HttpCall) -> tuple[Repo, str] | None:
    """The case repo a smart-HTTP call is for, and the git path under it."""
    found = _SUFFIX.search(call.path)
    if found is None:
        return None
    wanted = _repo_id(f"//{call.host}{call.path[: found.start()]}")
    repo = next((r for r in item.case.world.repos or () if _repo_id(r.url) == wanted), None)
    return (repo, found[1]) if repo else None


def _pkt_payload(body: bytes, encoding: str | None) -> bytes:
    return gzip.decompress(body) if encoding == "gzip" else body


def effects_of(repo: Repo, service_path: str, call: HttpCall) -> tuple[Effect, ...]:
    target = f"git:{_repo_id(repo.url)}"
    if call.method != "POST":
        return ()
    payload = _pkt_payload(call.body, call.header("content-encoding"))
    if service_path == "/git-receive-pack":
        return tuple(
            Effect(
                verb="git.repo.push",
                target=target,
                args={"ref": ref.decode(), "old": old.decode(), "new": new.decode()},
            )  # fmt: skip
            for old, new, ref in _COMMAND.findall(payload)
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


@dataclass(frozen=True)
class GitService:
    """Per-item bare repos under `root`, made from the case bundles on first use."""

    root: Path
    load_blob: LoadBlob

    def path(self, item: ItemRef, repo: Repo) -> Path:
        return self.root / str(item.id) / f"{_repo_id(repo.url)}.git"

    async def _ensure(self, item: ItemRef, repo: Repo) -> Path:
        path = self.path(item, repo)
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        bundle = await self.load_blob(item, repo.bundle.sha256)
        with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
            source = Path(scratch) / "repo.bundle"
            source.write_bytes(bundle)
            staged = Path(scratch) / "repo.git"
            await _run("git", "clone", "--quiet", "--mirror", str(source), str(staged))
            await _run("git", "config", "http.receivepack", "true", cwd=staged)
            head = f"refs/heads/{repo.default_branch}"
            await _run("git", "symbolic-ref", "HEAD", head, cwd=staged)
            staged.rename(path)
        return path

    async def serve(self, item: ItemRef, repo: Repo, service_path: str, call: HttpCall) -> GitReply:
        path = await self._ensure(item, repo)
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
        effects = effects_of(repo, service_path, call) if status == 200 else ()
        return GitReply(status, headers, body, effects)

"""GitHub markdown: the text files of one repository, one record each.

A mirror of a repository's markdown and text files at the head of a branch, readable by the
whole of the scope the source is created with. A run compares the head commit with the last
one read, reads only the files whose blob changed, and removes those that are gone. The
source's `redaction_set` rules are applied as the files are stored.

Credential `{"token": "..."}`: a fine-grained token with read access to the repository's
contents. Configuration (`sources.config`): `repo` (`owner/name`), `folders` (files or folders
to read; empty: every text file), `branch` (default: the repository's default).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncGenerator
from typing import ClassVar, Final

import httpx
from daimon.data.sync.connector import (
    Checkpoint,
    Connector,
    Field,
    ItemRecord,
    Json,
    Record,
    Source,
    SyncState,
)
from daimon.data.sync.connectors.chat import names, obj
from daimon.data.sync.connectors.github_api import (
    Api,
    blob,
    client_of,
    head,
    tree,
    wants,
)
from daimon.data.sync.http import Upstream

REPO: Final = re.compile(r"[\w.-]+/[\w.-]+")
HEADING: Final = re.compile(r"^#\s+(.+)$", re.M)
TITLE: Final = re.compile(r"^title:\s*[\"']?(.+?)[\"']?\s*$", re.M)
CHECK_EVERY: Final = 25


def repo_of(config: Json) -> str:
    repo = str(config.get("repo") or "").strip().strip("/")
    if not REPO.fullmatch(repo):
        raise ValueError("config.repo must be owner/name")
    return repo


def title_of(path: str, text: str) -> str:
    front = text[:600] if text.startswith("---") else ""
    m = TITLE.search(front) or HEADING.search(text[:2000])
    return m.group(1).strip()[:200] if m else path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


class GitHubMarkdownConnector(Connector):
    kind: ClassVar[str] = "github_markdown"
    name: ClassVar[str] = "GitHub markdown"
    category: ClassVar[str] = "knowledge"
    default_scope: ClassVar[str | None] = "internal"
    fields: ClassVar[tuple[Field, ...]] = (
        Field("token", "Fine-grained token", "secret"),
        Field("repo", "Repository", placeholder="your-org/notes"),
        Field("folders", "Folders or files", "list", required=False, placeholder="docs, notes"),
        Field("branch", "Branch", required=False, placeholder="main"),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            repo = repo_of(source.config)
            async with client_of(source, self.transport) as client:
                got = await head(Api(client, source), repo, str(source.config.get("branch") or ""))
        except (ValueError, Upstream, httpx.HTTPError) as e:
            return False, str(e)
        return (True, f"{repo} at {got[0][:7]}") if got else (False, f"{repo} has no commits")

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        repo = repo_of(source.config)
        folders = names(source.config, "folders")
        branch = str(source.config.get("branch") or "")
        basis = hashlib.sha256(f"{repo}|{branch}|{sorted(folders)}".encode()).hexdigest()[:16]
        old = source.state if source.state.get("basis") == basis else {}
        known = {str(k): str(v) for k, v in obj(old.get("blobs")).items()}
        async with client_of(source, self.transport) as client:
            api = Api(client, source)
            got = await head(api, repo, branch)
            if got is None:
                raise Upstream(f"{repo} has no commits; nothing was removed")
            sha, date = got
            if old.get("head") == sha:
                yield SyncState(old)
                return
            files = await tree(api, repo, sha, wants(folders))
            state: Json = {"v": 1, "basis": basis, "head": None, "blobs": dict(known)}
            n = 0
            for path in sorted(files):
                if known.get(path) == files[path]:
                    continue
                text = await blob(api, repo, files[path])
                yield ItemRecord(
                    f"file:{path}",
                    "markdown",
                    title_of(path, text),
                    text,
                    date,
                    container=path.rsplit("/", 1)[0] if "/" in path else "/",
                    container_kind="folder",
                    url=f"https://github.com/{repo}/blob/{sha}/{path}",
                    payload={"repo": repo, "path": path, "sha": sha},
                )
                state["blobs"][path] = files[path]
                n += 1
                if n % CHECK_EVERY == 0:
                    yield Checkpoint(state)
        yield SyncState(
            {"v": 1, "basis": basis, "head": sha, "blobs": files},
            {f"file:{p}" for p in files},
        )

"""What the GitHub connectors share: calls inside the source's budget, and the repository files
they mirror."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

import httpx
from daimon.data.sync.connector import Json, Source
from daimon.data.sync.connectors.chat import obj, rows
from daimon.data.sync.http import GuardedTransport, Upstream, send

API: Final = "https://api.github.com"
PER_PAGE: Final = 100
PAGES: Final = 500
"""Pages read of one list at most: a list that never ends is a fault."""
FILE_MAX: Final = 1_000_000
"""Bytes of one file read; a larger one is skipped."""
TEXT: Final = (".md", ".markdown", ".txt", ".rst")


def client_of(source: Source, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    token = source.credentials.get("token", "")
    if not token:
        raise ValueError("the credential needs the token")
    return httpx.AsyncClient(
        base_url=API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=60,
        transport=transport or GuardedTransport(source.private_hosts),
    )


def when(s: object) -> datetime:
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class Refused(Upstream):
    """GitHub answered a call with a status the caller did not accept."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"GitHub answered HTTP {status} for {path.rsplit('/', 1)[-1] or path}")
        self.status = status


class Api:
    """Read-only calls (GET, and GraphQL queries) inside the source's request budget."""

    def __init__(self, client: httpx.AsyncClient, source: Source) -> None:
        self.client, self.source = client, source

    async def get(
        self,
        path: str,
        params: Mapping[str, str | int] | None = None,
        *,
        ok: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        r = await send(self.client, self.source.budget, path, params, deadline=self.source.deadline)
        if r.status_code in ok:
            return r
        limited = r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"
        if limited:
            raise Upstream("GitHub rate limit reached")
        raise Refused(r.status_code, path)

    async def pages(
        self,
        path: str,
        params: Mapping[str, str | int] | None = None,
        *,
        ok: tuple[int, ...] = (200,),
    ) -> AsyncIterator[Json]:
        """Every item of a paged list, following its `next` link. A status in `ok` other than
        200 (a feature that is off) ends it empty."""
        url: str | None = path
        query: dict[str, str | int] = {**(params or {}), "per_page": PER_PAGE}
        for _ in range(PAGES):
            r = await self.get(url, query if url == path else None, ok=ok)
            if r.status_code != 200:
                return
            for item in rows(r.json()):
                yield item
            url = obj(r.links.get("next")).get("url")
            if not url:
                return
            nxt, base = httpx.URL(url), self.client.base_url
            if (nxt.scheme, nxt.host, nxt.port) != (base.scheme, base.host, base.port):
                raise Upstream("GitHub paged to a host other than its API")
        raise Upstream(f"GitHub returned more than {PAGES} pages of {path}")

    async def graphql(self, query: str, **variables: object) -> Json:
        await self.source.budget.take()
        try:
            r = await self.client.post("/graphql", json={"query": query, "variables": variables})
        except httpx.TransportError:
            raise Upstream("GitHub GraphQL is unreachable") from None
        if r.status_code != 200:
            raise Upstream(f"GitHub GraphQL answered HTTP {r.status_code}")
        body = obj(r.json())
        if body.get("errors"):
            raise Upstream("GitHub GraphQL refused the query")
        return obj(body.get("data"))


async def repositories(api: Api, org: str) -> list[Json]:
    """The organization's repositories the token sees, archived ones included, by id."""
    found = [r async for r in api.pages(f"/orgs/{quote(org)}/repos", {"type": "all"})]
    return sorted(found, key=lambda r: int(r["id"]))


def wants(prefixes: list[str]) -> Callable[[str], bool]:
    """Text files at or under any of `prefixes` (a folder or a file; empty: anywhere)."""
    folders = [p.strip("/") for p in prefixes]

    def under(path: str) -> bool:
        return path.lower().endswith(TEXT) and (
            not folders or any(path == p or path.startswith(p + "/") for p in folders if p)
        )

    return under


async def head(api: Api, repo: str, branch: str | None) -> tuple[str, datetime] | None:
    """The branch's head commit (sha, date); None for an empty repository."""
    r = await api.get(
        f"/repos/{repo}/commits/{quote(branch or 'HEAD', safe='')}", ok=(200, 404, 409)
    )
    if r.status_code != 200:
        return None
    j = obj(r.json())
    return str(j["sha"]), when(obj(obj(j.get("commit")).get("committer")).get("date"))


async def tree(api: Api, repo: str, sha: str, want: Callable[[str], bool]) -> dict[str, str]:
    """Path to blob sha of the wanted files at a commit."""
    j = obj((await api.get(f"/repos/{repo}/git/trees/{sha}", {"recursive": "1"})).json())
    if j.get("truncated"):
        raise Upstream(f"{repo}: the file tree is too large to list")
    return {
        str(e["path"]): str(e["sha"])
        for e in rows(j.get("tree"))
        if e.get("type") == "blob" and int(e.get("size") or 0) <= FILE_MAX and want(str(e["path"]))
    }


async def blob(api: Api, repo: str, sha: str) -> str:
    j = obj((await api.get(f"/repos/{repo}/git/blobs/{sha}")).json())
    return base64.b64decode(str(j.get("content") or "")).decode("utf-8", "replace")

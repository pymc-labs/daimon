"""What the GitHub connector shares: calls inside the source's budget, the token Daimon's GitHub
App mints for a workspace, and the repository paths it lists."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

import httpx
import jwt
from daimon.data.sync.connector import Json, Source
from daimon.data.sync.connectors.chat import obj, rows
from daimon.data.sync.http import GuardedTransport, Upstream, send
from psycopg import AsyncConnection
from psycopg.rows import DictRow

API: Final = "https://api.github.com"
PER_PAGE: Final = 100
PAGES: Final = 500
"""Pages read of one list at most: a list that never ends is a fault."""
APP_READS: Final = ("metadata", "contents", "issues", "pull_requests", "discussions", "members")
"""What the connector asks of Daimon's GitHub App, read only, of those the installation grants."""
APP_TOKEN_TTL: Final = 50 * 60.0
"""Seconds a minted token is reused (GitHub's last an hour)."""
_minted: dict[tuple[int, tuple[int, ...]], tuple[float, str]] = {}


def client_of(source: Source, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    token = source.credentials.get("token", "")
    if not token:
        why = source.credentials.get("mint_error")
        raise ValueError(why or "the credential needs a token, or Daimon's GitHub App connected")
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


async def app_token(
    conn: AsyncConnection[DictRow],
    app: tuple[str, str],
    source: Source,
    transport: httpx.AsyncBaseTransport,
) -> str | None:
    """A read token from Daimon's GitHub App (`app`: its id and private key) for the repositories
    the source's workspace connected in the source's organization; None: it connected none."""
    cur = await conn.execute(
        b"select r.installation_id, array_agg(r.repo_id order by r.repo_id) ids,"
        b" max(i.permissions::text) granted from tenant_github_repos r"
        b" left join github_app_installations i on i.installation_id = r.installation_id"
        b" where r.tenant_id = %s and r.status = 'active'"
        b" and lower(split_part(r.repo_full_name, '/', 1)) = lower(%s)"
        b" group by r.installation_id order by count(*) desc limit 1",
        (source.tenant_id, str(source.config.get("org") or "")),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    key = (int(row["installation_id"]), tuple(int(i) for i in row["ids"][:500]))
    hit = _minted.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    granted = obj(json.loads(row["granted"] or "{}"))
    now = int(time.time())
    signed = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app[0]}, app[1], "RS256")
    async with httpx.AsyncClient(base_url=API, transport=transport, timeout=30) as client:
        r = await client.post(
            f"/app/installations/{key[0]}/access_tokens",
            headers={"Authorization": f"Bearer {signed}", "Accept": "application/vnd.github+json"},
            json={
                "repository_ids": list(key[1]),
                "permissions": {k: "read" for k in APP_READS if k in granted or not granted},
            },
        )
    token = str(obj(r.json()).get("token") or "") if r.status_code == 201 else ""
    if not token:
        raise Upstream(f"GitHub refused the App a token (HTTP {r.status_code})")
    _minted[key] = (time.monotonic() + APP_TOKEN_TTL, token)
    return token


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
    """Files at or under any of `prefixes` (a folder or a file; empty: anywhere)."""
    folders = [p.strip("/") for p in prefixes if p.strip("/")]
    return lambda path: not folders or any(path == p or path.startswith(p + "/") for p in folders)


async def head(api: Api, repo: str, branch: str | None) -> tuple[str, datetime] | None:
    """The branch's head commit (sha, date); None for an empty repository."""
    r = await api.get(
        f"/repos/{repo}/commits/{quote(branch or 'HEAD', safe='')}", ok=(200, 404, 409)
    )
    if r.status_code != 200:
        return None
    j = obj(r.json())
    return str(j["sha"]), when(obj(obj(j.get("commit")).get("committer")).get("date"))


async def tree(api: Api, repo: str, sha: str, want: Callable[[str], bool]) -> list[str]:
    """The wanted file paths at a commit."""
    j = obj((await api.get(f"/repos/{repo}/git/trees/{sha}", {"recursive": "1"})).json())
    if j.get("truncated"):
        raise Upstream(f"{repo}: the file tree is too large to list")
    return sorted(
        str(e["path"])
        for e in rows(j.get("tree"))
        if e.get("type") == "blob" and want(str(e["path"]))
    )

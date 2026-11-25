"""LinkedIn: an organization's published, public posts, one record per post, readable by the
whole tenant (a source is created with the `public` scope).

Every run reads every post, so a post deleted on LinkedIn, or no longer public, disappears. Drafts,
connection-only, sponsored (dark) and unfeatured posts are never stored. A run that finds no
posts for an organization that had some, or posts that no longer say whether they are public,
fails and removes nothing.

Credential `{"token": "..."}`: an access token that may read the organization's posts
(`r_organization_social`). Configuration (`sources.config`): `organization`, the organization's
id or URN (`urn:li:organization:12345`).
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar, Final

import httpx
from daimon.data.sync.connector import (
    Connector,
    Field,
    ItemRecord,
    Json,
    Record,
    Source,
    SyncState,
)
from daimon.data.sync.connectors.chat import obj, rows
from daimon.data.sync.http import GuardedTransport, Upstream, send

API: Final = "https://api.linkedin.com/rest"
VERSION: Final = "202601"
PAGE: Final = 100
PAGES: Final = 500
"""Pages read per run at most: a stream that never ends is a fault."""
LITTLE: Final = re.compile(r"\\([|{}@\[\]()<>#\\*_~])")
"""LinkedIn escapes its reserved characters in post text."""
URN: Final = re.compile(r"urn:li:(organization|person):[\w-]+")


def urn_of(config: Json) -> str:
    v = str(config.get("organization") or "").strip()
    v = f"urn:li:organization:{v}" if v.isdecimal() else v
    if not URN.fullmatch(v):
        raise ValueError(
            "config.organization must be an id or a URN such as urn:li:organization:12345"
        )
    return v


def plain(t: object) -> str:
    s = re.sub(r"@\[([^\]]*)\]\(urn:[^)]*\)", r"@\1", str(t or ""))
    s = re.sub(r"\{hashtag\|\\?#\|([^}]*)\}", r"#\1", s)
    return LITTLE.sub(r"\1", s).strip()


def stamp(ms: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(str(ms)) / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def record(p: Json, urn: str) -> ItemRecord | None:
    """The post as a record, or None when it is not a public, published post with text."""
    dist = obj(p.get("distribution"))
    if (
        p.get("lifecycleState", "PUBLISHED") != "PUBLISHED"
        or p.get("visibility") != "PUBLIC"
        or p.get("isDsc")
        or dist.get("feedDistribution") == "NONE"
        or not p.get("id")
    ):
        return None
    when = stamp(p.get("publishedAt") or p.get("createdAt"))
    article = obj(obj(p.get("content")).get("article")).get("title")
    body = "\n".join(
        x for x in (plain(p.get("commentary")), article and f"[article: {article}]") if x
    )
    if not body or not when:
        return None
    return ItemRecord(
        f"post:{p['id']}",
        "post",
        f"LinkedIn post {when:%Y-%m-%d}: {body.split(chr(10))[0][:60]}",
        body,
        when,
        container="Posts",
        container_kind="page",
        url=f"https://www.linkedin.com/feed/update/{p['id']}/",
        payload={"urn": p["id"], "organization": urn},
        facets={"platform": "linkedin"},
    )


class LinkedInConnector(Connector):
    kind: ClassVar[str] = "linkedin"
    name: ClassVar[str] = "LinkedIn"
    category: ClassVar[str] = "chat"
    default_scope: ClassVar[str | None] = "public"
    fields: ClassVar[tuple[Field, ...]] = (
        Field("token", "Access token", "secret"),
        Field("organization", "Organization", placeholder="urn:li:organization:12345"),
    )

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport

    def _client(self, source: Source) -> httpx.AsyncClient:
        token = source.credentials.get("token", "")
        if not token:
            raise ValueError("the credential needs an access token")
        return httpx.AsyncClient(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Linkedin-Version": VERSION,
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=30,
            transport=self.transport or GuardedTransport(source.private_hosts),
        )

    async def _posts(
        self, client: httpx.AsyncClient, source: Source, urn: str
    ) -> AsyncIterator[Json]:
        """Every post, paged until the stream is empty or past its total: a short page proves
        nothing, the API filters some."""
        start = 0
        for _ in range(PAGES):
            r = await send(
                client,
                source.budget,
                "/posts",
                {"q": "author", "author": urn, "count": PAGE, "start": start},
                deadline=source.deadline,
            )
            if r.status_code != 200:
                raise Upstream(f"LinkedIn answered HTTP {r.status_code}")
            j = obj(r.json())
            els = rows(j.get("elements"))
            for p in els:
                yield p
            start += len(els)
            total = obj(j.get("paging")).get("total")
            if not els or (isinstance(total, int) and start >= total):
                return
        raise Upstream(f"LinkedIn returned more than {PAGES} pages of posts; nothing was removed")

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            urn = urn_of(source.config)
            async with self._client(source) as client:
                n = sum([1 async for p in self._posts(client, source, urn) if record(p, urn)])
        except (ValueError, Upstream, httpx.HTTPError) as e:
            return False, str(e)
        return (True, f"{n} posts to read") if n else (False, "no published posts with text found")

    async def fetch(self, source: Source) -> AsyncGenerator[Record]:
        urn = urn_of(source.config)
        ids: set[str] = set()
        seen = typed = 0
        async with self._client(source) as client:
            async for p in self._posts(client, source, urn):
                seen += 1
                typed += "visibility" in p
                if rec := record(p, urn):
                    ids.add(rec.external_id)
                    yield rec
        if seen and not typed:  # the API changed, not the posts
            raise Upstream(
                "LinkedIn posts no longer say whether they are public; nothing was removed"
            )
        yield SyncState({"v": 1}, ids)

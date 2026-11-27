"""Linear, read live: issues found at question time with the source's own API key, nothing
stored. Read-only, and scope-gated: a caller whose scopes include the source's default scope
reads every issue the key sees; there is no per-issue access list. See
`docs/data-connectors-apps.md`.

Credential `{"token": "..."}`: a personal API key (sent as it is, without `Bearer`).
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Final

import httpx
from daimon.data.sync.connector import Connector, Field, LiveHit, Source
from daimon.data.sync.connectors.chat import obj, rows
from daimon.data.sync.connectors.live_http import call, client_of
from daimon.data.sync.http import Upstream

API: Final = "https://api.linear.app"
TEXT_MAX: Final = 1500
ISSUE: Final = "id identifier title description url updatedAt team { key } creator { name }"
SEARCH: Final = (
    "query($term: String!, $first: Int) { searchIssues(term: $term, first: $first) {"
    f" nodes {{ {ISSUE} }} }} }}"
)


def when(s: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def hit(n: dict[str, object]) -> LiveHit:
    return LiveHit(
        str(n["identifier"]),
        f"{n['identifier']} {n.get('title') or ''}".strip(),
        str(n.get("description") or "")[:TEXT_MAX],
        "issue",
        str(n["url"]) if n.get("url") else None,
        str(obj(n.get("creator")).get("name") or "") or None,
        when(n.get("updatedAt")),
    )


class LinearConnector(Connector):
    kind: ClassVar[str] = "linear"
    name: ClassVar[str] = "Linear"
    category: ClassVar[str] = "work"
    live: ClassVar[bool] = True
    fields: ClassVar[tuple[Field, ...]] = (Field("token", "API key", "secret"),)

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """`transport` replaces the guarded one (tests)."""
        self.transport = transport

    async def _graphql(self, source: Source, query: str, **variables: object) -> dict[str, object]:
        async with client_of(source, API, self.transport, bearer=False) as client:
            body = await call(
                client, source, "Linear", "/graphql", {"query": query, "variables": variables}
            )
        if body.get("errors"):
            raise Upstream("Linear refused the query")
        return obj(body.get("data"))

    async def test(self, source: Source) -> tuple[bool, str]:
        try:
            await self._graphql(source, "query { viewer { id } }")
        except (ValueError, Upstream, httpx.HTTPError) as e:
            return False, str(e)
        return True, "ok"

    async def live_search(self, source: Source, query: str, limit: int) -> list[LiveHit]:
        data = await self._graphql(source, SEARCH, term=query, first=limit)
        return [hit(n) for n in rows(obj(data.get("searchIssues")).get("nodes"))]

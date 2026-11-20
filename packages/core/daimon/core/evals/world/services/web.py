"""Public web pages and search: the case's recorded pages and search result sets, served to
plain HTTP GETs, Daimon's `web_fetch` and the eval `web_search` tool."""

from __future__ import annotations

import json
from typing import Final
from urllib.parse import urlsplit

from daimon.core.evals.format import (
    ContentBlock,
    Effect,
    ExchangeResponse,
    HttpResponse,
    SearchSet,
    WebPage,
    to_document,
)
from daimon.core.evals.world.matching import SameJudge, normalized_text
from daimon.core.evals.world.model import HttpCall, ItemRef, WorldRequest, body_bytes
from daimon.core.evals.world.overlay import Overlay
from daimon.core.host_web_fetch import page_text
from pydantic import JsonValue

FETCH_TOOL: Final = "web_fetch"
SEARCH_TOOL: Final = "web_search"


def _url_id(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{(parts.hostname or '').lower()}{path}?{parts.query}"


def page_for(item: ItemRef, url: str) -> WebPage | None:
    wanted = _url_id(url)
    return next((p for p in item.case.world.web or () if _url_id(p.url) == wanted), None)


def _url(request: WorldRequest) -> str | None:
    if request.request.http is not None:
        return request.request.http.url
    args = request.request.args
    url = args.get("url") if isinstance(args, dict) else None
    return url if isinstance(url, str) else None


def _query(request: WorldRequest) -> str | None:
    args = request.request.args
    query = args.get("query") if isinstance(args, dict) else None
    return query if isinstance(query, str) else None


def _tool_reply(result: JsonValue, *, is_error: bool = False) -> ExchangeResponse:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return ExchangeResponse(
        result=result, is_error=is_error, content=[ContentBlock(type="text", text=text)]
    )


def search_reply(found: SearchSet | None) -> ExchangeResponse:
    results: list[JsonValue] = [to_document(r) for r in found.results] if found else []
    return _tool_reply({"results": results})


async def search_set(item: ItemRef, query: str, same: SameJudge) -> SearchSet | None:
    """The recorded result set for `query`: equal text first, then a `semantic` set the judge
    says asks the same thing."""
    sets = item.case.world.search or []
    wanted = normalized_text(query)
    exact = next((s for s in sets if normalized_text(s.query) == wanted), None)
    if exact is not None:
        return exact
    for candidate in sets:
        if candidate.match == "semantic" and await same("web", candidate.query, query):
            return candidate
    return None


class WebService:
    name = "web"

    def claims(self, item: ItemRef, call: HttpCall) -> bool:
        return call.method == "GET" and page_for(item, call.url) is not None

    def effects(self, request: WorldRequest) -> tuple[Effect, ...]:
        if request.tool == SEARCH_TOOL and (query := _query(request)) is not None:
            return (Effect(verb="web.search.query", args={"query": query}),)
        url = _url(request)
        return (Effect(verb="web.page.fetch", target=url),) if url else ()

    def answer(
        self, item: ItemRef, request: WorldRequest, overlay: Overlay
    ) -> ExchangeResponse | None:
        url = _url(request)
        page = page_for(item, url) if url else None
        if page is None:
            return None
        if request.surface == "http":
            media = page.body.media_type if page.body else None
            headers = {"content-type": media or "text/html; charset=utf-8"}
            return ExchangeResponse(
                http=HttpResponse(status=page.status, headers=headers, body=page.body)
            )
        if not 200 <= page.status < 300:
            return _tool_reply(f"The page answered {page.status}.", is_error=True)
        raw = body_bytes(page.body)
        if raw is None:
            return None  # a blob page: the simulator words it from the case
        media = (page.body.media_type if page.body else None) or "text/html"
        return _tool_reply(page_text(raw, media, "utf-8", truncated=False))

    def remember(self, request: WorldRequest, response: ExchangeResponse, overlay: Overlay) -> None:
        return None

    def check(self, request: WorldRequest, response: ExchangeResponse) -> str | None:
        return None

    def guidance(self, item: ItemRef, request: WorldRequest) -> str:
        return "A public web page or search; answer as the open web would."

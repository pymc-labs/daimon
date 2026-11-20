"""Whether a live request asks what a recorded exchange answered: `exact`, `normalized`,
`keys`, `semantic` (case-format "Matching"). Pure; the dispatcher asks the semantic judge."""

from __future__ import annotations

import copy
import hashlib
from urllib.parse import parse_qsl, urlsplit

from daimon.core.evals.effects import pointer_get
from daimon.core.evals.format import (
    Exchange,
    ExchangeRequest,
    JsonObject,
    McpTool,
    canonical_json,
    to_document,
)
from daimon.core.evals.world.model import WorldRequest, body_json
from pydantic import JsonValue


def clean(value: JsonValue) -> JsonValue:
    """Whitespace collapsed in strings, nulls dropped from objects."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def _defaults(tool: McpTool | None) -> JsonObject:
    properties = (tool.input_schema.get("properties") if tool else None) or {}
    if not isinstance(properties, dict):
        return {}
    return {
        name: spec["default"]
        for name, spec in properties.items()
        if isinstance(spec, dict) and "default" in spec
    }


def match_document(request: ExchangeRequest, tool: McpTool | None = None) -> JsonValue:
    """The part of a request matching compares: arguments for a tool call (defaults dropped),
    method, host, path, query and body for HTTP, argv for a command line."""
    if request.http is not None:
        parts = urlsplit(request.http.url)
        query: dict[str, list[JsonValue]] = {}
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query.setdefault(key, []).append(value)
        body = request.http.body
        parsed = body_json(body)
        return {
            "method": request.http.method.upper(),
            "host": (parts.hostname or "").lower(),
            "path": parts.path.rstrip("/") or "/",
            "query": dict(query),
            "body": parsed if parsed is not None else (body.text if body else None),
        }
    if request.cli is not None:
        return {"argv": list(request.cli.argv)}
    args = request.args
    if isinstance(args, dict):
        defaults = _defaults(tool)
        return {k: v for k, v in args.items() if k not in defaults or defaults[k] != v}
    return args


def _without(document: JsonValue, pointers: list[str] | None) -> JsonValue:
    if not pointers or not isinstance(document, dict | list):
        return document
    pruned = copy.deepcopy(document)
    for pointer in pointers:
        *parents, last = pointer.lstrip("/").split("/")
        found, parent = pointer_get(pruned, "/" + "/".join(parents)) if parents else (True, pruned)
        if found and isinstance(parent, dict):
            parent.pop(last.replace("~1", "/").replace("~0", "~"), None)
    return pruned


def is_candidate(exchange: Exchange, request: WorldRequest) -> bool:
    """Same surface and same target: tool (and server when both name one), or method and host."""
    if exchange.surface != request.surface:
        return False
    if request.surface == "http":
        live, recorded = request.request.http, exchange.request.http
        return (
            live is not None
            and recorded is not None
            and live.method.upper() == recorded.method.upper()
            and (urlsplit(live.url).hostname or "").lower()
            == (urlsplit(recorded.url).hostname or "").lower()
        )
    same_server = (
        exchange.server is None or request.server is None or (exchange.server == request.server)
    )
    return exchange.tool == request.tool and same_server


def static_match(exchange: Exchange, request: WorldRequest) -> bool | None:
    """Whether the exchange answers the request; None when only the semantic judge can say."""
    match = exchange.match
    mode = match.mode if match else "normalized"
    if mode == "exact":
        return canonical_json(to_document(exchange.request)) == canonical_json(
            to_document(request.request)
        )
    if mode == "semantic":
        return None
    ignore = match.ignore if match else None
    recorded = clean(_without(match_document(exchange.request, request.tool_info), ignore))
    live = clean(_without(match_document(request.request, request.tool_info), ignore))
    if mode == "keys" and match and match.keys:  # no keys: compared as `normalized`
        return all(pointer_get(recorded, k) == pointer_get(live, k) for k in match.keys)
    return canonical_json(recorded) == canonical_json(live)


def sim_key(request: WorldRequest, overlay_digest: str) -> str:
    """The cache key of a simulated answer: the normalized request, the representation it
    accepts and the overlay state."""
    http = request.request.http
    headers = (http.headers or {}) if http else {}
    accept = next((v for k, v in headers.items() if k.lower() == "accept"), None)
    document: JsonValue = {
        "surface": request.surface,
        "service": request.service,
        "server": request.server,
        "tool": request.tool,
        "request": clean(match_document(request.request, request.tool_info)),
        "overlay": overlay_digest,
        "accept": accept.lower() if accept else None,
    }
    return hashlib.sha256(canonical_json(document)).hexdigest()


def normalized_text(text: str) -> str:
    return " ".join(text.casefold().split())

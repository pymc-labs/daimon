"""What the fake world takes and gives: the item it answers for, requests, answers, services."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit

from daimon.core.evals.format import (
    Body,
    Case,
    Effect,
    ExchangeRequest,
    ExchangeResponse,
    McpTool,
    Origin,
)
from pydantic import JsonValue

if TYPE_CHECKING:
    from daimon.core.evals.world.overlay import Overlay

Surface = Literal["mcp", "http", "provider_tool"]


@dataclass(frozen=True)
class ItemRef:
    """A running item: its id, the tenant holding its run and blobs, and its case."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    case: Case
    case_sha256: str


@dataclass(frozen=True)
class HttpCall:
    """One HTTP request as the sandbox gateway saw it, body as raw bytes."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes = b""

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == wanted), None)

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"


@dataclass(frozen=True)
class WorldRequest:
    """A request normalized for dispatch: where it came from and what it asks."""

    surface: Surface
    service: str
    request: ExchangeRequest
    server: str | None = None
    tool: str | None = None
    tool_info: McpTool | None = None
    raw: HttpCall | None = None


@dataclass(frozen=True)
class WorldAnswer:
    """What the world answered, where the answer came from and what the call meant."""

    response: ExchangeResponse
    origin: Origin
    effects: tuple[Effect, ...] = ()
    seq: int = 0
    reason: str | None = None
    raw_body: bytes = b""  # HTTP answers: the body's bytes, blobs and pack data included


class Service(Protocol):
    """One service the world can play: routing, state-shaped answers, checks, guidance."""

    name: str

    def claims(self, item: ItemRef, call: HttpCall) -> bool: ...

    def effects(self, request: WorldRequest) -> tuple[Effect, ...]: ...

    def answer(
        self, item: ItemRef, request: WorldRequest, overlay: Overlay
    ) -> ExchangeResponse | None:
        """An answer from the case's seed state and the item's overlay, or None to go on."""
        ...

    def remember(self, request: WorldRequest, response: ExchangeResponse, overlay: Overlay) -> None:
        """Keep what an answer from elsewhere (recorded, simulated) says about state."""
        ...

    def check(self, request: WorldRequest, response: ExchangeResponse) -> str | None:
        """Why a simulated answer is not one the real service could give, or None."""
        ...

    def guidance(self, item: ItemRef, request: WorldRequest) -> str: ...


def body_of(data: bytes, content_type: str | None) -> Body | None:
    """Raw bytes as a format `Body`: JSON, else text; None for binary (kept as a blob)."""
    if not data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "json" in (content_type or "") or text[:1] in ("{", "["):
        try:
            parsed: JsonValue = json.loads(text)
        except ValueError:
            return Body(text=text, media_type=content_type)
        return Body(json=parsed, media_type=content_type)
    return Body(text=text, media_type=content_type)


def body_bytes(body: Body | None) -> bytes | None:
    """A body's bytes; None when it is a blob the caller must load."""
    if body is None:
        return b""
    if body.blob is not None:
        return None
    if body.text is not None:
        return body.text.encode()
    return json.dumps(body.json_, separators=(",", ":")).encode()


def body_json(body: Body | None) -> JsonValue:
    """The JSON a body holds, parsing text that is JSON; None otherwise."""
    if body is None:
        return None
    if body.json_ is not None:
        return body.json_
    if body.text:
        try:
            parsed: JsonValue = json.loads(body.text)
        except ValueError:
            return None
        return parsed
    return None


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()

"""SeamClient — the report host's only outbound surface.

Every question a reader asks becomes a sequence of seam tool calls carrying
the report's own token: the token minted for the reader variant under the
publishing account, which is what makes the Q&A billed and admission-gated
in the right tenant. The token is a parameter on every method, never
instance state, so one client object can safely serve many reports at once
with no chance of one report's calls being billed or attributed to another.

Transport shape: a fresh streamable-HTTP MCP session is opened per call, one
``httpx.AsyncClient`` per call carrying that call's own ``Authorization``
header. An early throwaway prototype measured roughly one second of overhead
per call and judged it acceptable at a two-second poll interval; nothing
about this module's scope changes that trade-off, so the shape is kept
rather than pooling one long-lived MCP session across reports (which would
reintroduce exactly the shared-state risk this module exists to avoid).

Boundary timestamps (``turn_started_at``) are carried as the opaque ISO-8601
strings the seam returns and are never round-tripped through ``datetime`` —
whatever string ``start_turn``/``continue_turn`` hand back is byte-identical
to what a caller later sends as ``list_events``'s ``created_at_gte``.

Errors, all deriving from one base:

- ``SeamError`` — any seam failure not otherwise typed.
- ``SeamUnauthorizedError`` — the transport answered 401, or the MCP layer
  refused the bearer. Callers should mark the report unauthorized.
- ``BundleExpiredError`` — the tool error text names the bundle's underlying
  Files API object as gone. Callers should re-push the archive and retry.

Nothing else gets its own type; a taxonomy nobody branches on is noise.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import IO, cast

import httpx
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger(__name__)

_BUNDLE_EXPIRED_TEXT = "bundle expired; re-upload"


def _flatten_first[ExcT: BaseException](group: BaseExceptionGroup[ExcT]) -> ExcT:
    """Unwrap nested ``except*`` ExceptionGroups down to one leaf exception.

    mcp's streamable-HTTP transport runs its request loop inside an anyio
    task group, so a transport failure always arrives wrapped in an
    ExceptionGroup (PEP 654) even for a single request — a plain
    ``except httpx.HTTPStatusError`` would never match it, and a nested
    task group can wrap it more than once.
    """
    current: BaseException = group
    while isinstance(current, BaseExceptionGroup):
        current = cast("BaseException", current.exceptions[0])
    return cast("ExcT", current)


class SeamError(Exception):
    """Raised on any seam failure not otherwise typed."""


class SeamUnauthorizedError(SeamError):
    """The seam rejected the bearer token (HTTP 401)."""


class BundleExpiredError(SeamError):
    """The bundle's underlying Files API object is gone; re-push the archive."""


@dataclass(frozen=True)
class StartedTurn:
    """The three boundary fields returned by ``start_turn``/``continue_turn``."""

    handle: str
    turn_event_id: str
    turn_started_at: str


@dataclass(frozen=True)
class SessionStatus:
    """A session's plain status string, as returned by ``get_my_session``/``cancel_turn``."""

    status: str


@dataclass(frozen=True)
class TurnEvents:
    """A page of a session's transcript."""

    items: list[dict[str, object]]
    next_page: str | None


@dataclass(frozen=True)
class TurnCost:
    """One finished turn's raw provider cost, before markup."""

    cost_usd: Decimal | None
    event_count: int


@dataclass(frozen=True)
class BundlePushed:
    """The signed handle and metadata returned by ``PUT /bundles``."""

    handle: str
    sha256: str
    size_bytes: int
    expires_at: str


def _require_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise SeamError(f"seam response missing string field {key!r}: {data!r}")
    return value


def _require_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise SeamError(f"seam response missing int field {key!r}: {data!r}")
    return value


def _optional_str(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SeamError(f"seam response field {key!r} was not a string: {data!r}")
    return value


def _require_list_of_dicts(data: dict[str, object], key: str) -> list[dict[str, object]]:
    value = data.get(key)
    if not isinstance(value, list):
        raise SeamError(f"seam response missing list field {key!r}: {data!r}")
    items: list[dict[str, object]] = []
    for entry in cast("list[object]", value):
        if not isinstance(entry, dict):
            raise SeamError(f"seam response list field {key!r} contained a non-object entry")
        items.append(cast("dict[str, object]", entry))
    return items


def _started_turn_from(data: dict[str, object]) -> StartedTurn:
    return StartedTurn(
        handle=_require_str(data, "handle"),
        turn_event_id=_require_str(data, "turn_event_id"),
        turn_started_at=_require_str(data, "turn_started_at"),
    )


def _first_text(result: mcp_types.CallToolResult) -> str:
    for block in result.content:
        if isinstance(block, mcp_types.TextContent):
            return block.text
    return ""


def _decode_tool_result(tool: str, result: mcp_types.CallToolResult) -> dict[str, object]:
    if result.isError:
        text = _first_text(result)
        if _BUNDLE_EXPIRED_TEXT in text:
            raise BundleExpiredError(text)
        raise SeamError(f"{tool}: {text}")
    structured = result.structuredContent
    if isinstance(structured, dict):
        return cast("dict[str, object]", structured)
    text = _first_text(result)
    parsed: object = json.loads(text) if text else {}
    if not isinstance(parsed, dict):
        raise SeamError(f"{tool} returned non-object JSON: {parsed!r}")
    return cast("dict[str, object]", parsed)


class SeamClient:
    """Typed wrappers over the agent-chat seam, one client object serving many reports.

    Collaborators are fixed at construction (the seam's MCP endpoint URL and
    an ``httpx.AsyncClient`` used for the plain-HTTP bundle push); the
    report's own token is a parameter on every method call, never instance
    state — this is what makes cross-report token confusion structurally
    impossible rather than a matter of caller discipline.

    ``transport`` is an optional seam for tests to redirect the per-call MCP
    sessions onto an in-process ASGI fake (``httpx.ASGITransport``) instead of
    the real network; production leaves it ``None`` and gets a real
    connection per call, same as the prototype.
    """

    def __init__(
        self,
        *,
        mcp_url: str,
        http_client: httpx.AsyncClient,
        max_bundle_bytes: int = 25 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._mcp_url = mcp_url
        self._bundles_url = str(httpx.URL(mcp_url).copy_with(path="/bundles"))
        self._http = http_client
        self._max_bundle_bytes = max_bundle_bytes
        self._transport = transport

    async def _call_tool(
        self, *, token: str, tool: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {token}"}
        log.debug("seam call tool=%s handle=%s", tool, arguments.get("handle"))
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport, headers=headers, follow_redirects=True
                ) as call_client,
                streamable_http_client(self._mcp_url, http_client=call_client) as streams,
            ):
                # mcp 1.x yields a 3-tuple (read, write, get_session_id); 2.x
                # narrowed this to a 2-tuple (read, write). Index rather than
                # destructure so a resolver drift past the pyproject pin
                # (mcp>=1.27,<2) fails loudly there, not as a bare
                # `ValueError: not enough values to unpack` at runtime.
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments)
        except* httpx.HTTPStatusError as eg:
            err = _flatten_first(eg)
            if err.response.status_code == 401:
                raise SeamUnauthorizedError(f"seam rejected the bearer for {tool}") from err
            raise SeamError(f"{tool} transport error: {err}") from err
        except* httpx.HTTPError as eg:
            err = _flatten_first(eg)
            raise SeamError(f"{tool} transport error: {err}") from err
        return _decode_tool_result(tool, result)

    async def start_turn(
        self, *, token: str, message: str, bundle: str | None = None
    ) -> StartedTurn:
        arguments: dict[str, object] = {"message": message}
        if bundle is not None:
            arguments["bundle"] = bundle
        data = await self._call_tool(token=token, tool="start_turn", arguments=arguments)
        return _started_turn_from(data)

    async def continue_turn(self, *, token: str, handle: str, message: str) -> StartedTurn:
        data = await self._call_tool(
            token=token,
            tool="continue_turn",
            arguments={"handle": handle, "message": message},
        )
        return _started_turn_from(data)

    async def get_session(self, *, token: str, handle: str) -> SessionStatus:
        data = await self._call_tool(
            token=token, tool="get_my_session", arguments={"handle": handle}
        )
        return SessionStatus(status=_require_str(data, "status"))

    async def list_events(
        self,
        *,
        token: str,
        handle: str,
        created_at_gte: str,
        types: list[str],
        page: str | None = None,
    ) -> TurnEvents:
        arguments: dict[str, object] = {
            "handle": handle,
            "created_at_gte": created_at_gte,
            "types": types,
            "order": "asc",
        }
        if page is not None:
            arguments["page"] = page
        data = await self._call_tool(token=token, tool="list_events", arguments=arguments)
        return TurnEvents(
            items=_require_list_of_dicts(data, "items"),
            next_page=_optional_str(data, "next_page"),
        )

    async def cancel_turn(self, *, token: str, handle: str) -> SessionStatus:
        data = await self._call_tool(token=token, tool="cancel_turn", arguments={"handle": handle})
        return SessionStatus(status=_require_str(data, "status"))

    async def get_turn_cost(
        self, *, token: str, handle: str, turn_started_at: str, turn_event_id: str
    ) -> TurnCost:
        data = await self._call_tool(
            token=token,
            tool="get_turn_cost",
            arguments={
                "handle": handle,
                "turn_started_at": turn_started_at,
                "turn_event_id": turn_event_id,
            },
        )
        raw_cost = data.get("cost_usd")
        cost_usd = Decimal(str(raw_cost)) if raw_cost is not None else None
        return TurnCost(cost_usd=cost_usd, event_count=_require_int(data, "event_count"))

    async def archive_session(self, *, token: str, handle: str) -> None:
        await self._call_tool(token=token, tool="archive_my_session", arguments={"handle": handle})

    async def push_bundle(
        self, *, token: str, archive: bytes | IO[bytes], size_bytes: int
    ) -> BundlePushed:
        if size_bytes > self._max_bundle_bytes:
            raise SeamError(
                f"bundle of {size_bytes} bytes exceeds the {self._max_bundle_bytes} byte cap"
            )
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/gzip"}
        log.debug("seam call tool=push_bundle")
        try:
            response = await self._http.put(self._bundles_url, content=archive, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 401:
                raise SeamUnauthorizedError("seam rejected the bearer for push_bundle") from err
            raise SeamError(f"push_bundle transport error: {err}") from err
        except httpx.HTTPError as err:
            raise SeamError(f"push_bundle transport error: {err}") from err
        data: object = response.json()
        if not isinstance(data, dict):
            raise SeamError(f"push_bundle returned non-object JSON: {data!r}")
        payload = cast("dict[str, object]", data)
        return BundlePushed(
            handle=_require_str(payload, "bundle"),
            sha256=_require_str(payload, "sha256"),
            size_bytes=_require_int(payload, "size_bytes"),
            expires_at=_require_str(payload, "expires_at"),
        )

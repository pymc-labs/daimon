"""Tests for SeamClient against an in-process fake seam.

The fake seam is a small `mcp.server.lowlevel.Server` mounted on the real
streamable-HTTP ASGI transport (`StreamableHTTPSessionManager`), driven over
`httpx.ASGITransport` — never a monkeypatch of `SeamClient`'s own methods, so
every test exercises the real MCP wire protocol our client actually speaks.

The harness itself (`build_fake_seam` / `fake_seam_lifespan`) is a shared
fixture from `conftest.py`, not defined here, so plan 21-14's shell tests for
`turns.py` can drive the same fake seam without a second implementation.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from decimal import Decimal

import httpx
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.streamable_http import streamable_http_client as real_streamable_http_client
from mcp.shared.message import SessionMessage
from report_host import mcp_client as mcp_client_module
from report_host.mcp_client import (
    BundleExpiredError,
    BundlePushed,
    SeamClient,
    SeamError,
    SeamUnauthorizedError,
    StartedTurn,
)
from starlette.types import Receive, Scope, Send

pytestmark = pytest.mark.asyncio

# Local aliases for the fixture types `conftest.py` provides (`build_fake_seam`,
# `fake_seam_lifespan`) — a plain `from conftest import ...` doesn't work under
# this project's `--import-mode=importlib` pytest config, so these mirror
# conftest's own aliases for type-hint purposes only, not a second harness.
FakeASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
FakeSeamBuilder = Callable[..., FakeASGIApp]
FakeSeamLifespan = Callable[[FakeASGIApp], AbstractAsyncContextManager[None]]


def _seam_client(app: FakeASGIApp) -> SeamClient:
    transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
    return SeamClient(
        mcp_url="http://testserver/mcp",
        http_client=httpx.AsyncClient(),
        transport=transport,
    )


async def test_start_turn_returns_all_three_boundary_fields(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {
            "handle": "ses_1",
            "turn_event_id": "evt_1",
            "turn_started_at": "2026-01-01T00:00:00Z",
        }

    captured: list[str] = []
    app = build_fake_seam(behaviors={"start_turn": start_turn}, captured_auth=captured)
    async with fake_seam_lifespan(app):
        result = await _seam_client(app).start_turn(token="tok", message="hello")
    assert result == StartedTurn(
        handle="ses_1", turn_event_id="evt_1", turn_started_at="2026-01-01T00:00:00Z"
    ), "start_turn must return all three boundary fields the seam sent"


async def test_start_turn_with_bundle_passes_it_through(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    seen: dict[str, dict[str, object]] = {}

    def start_turn(args: dict[str, object]) -> dict[str, object]:
        return {"handle": "ses_1", "turn_event_id": "evt_1", "turn_started_at": "t0"}

    captured: list[str] = []
    app = build_fake_seam(
        behaviors={"start_turn": start_turn}, captured_auth=captured, captured_arguments=seen
    )
    async with fake_seam_lifespan(app):
        await _seam_client(app).start_turn(token="tok", message="hello", bundle="bundle-handle-1")
    assert seen["start_turn"].get("bundle") == "bundle-handle-1", (
        "a provided bundle handle must be forwarded to the seam"
    )


async def test_start_turn_without_bundle_omits_the_parameter(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    seen: dict[str, dict[str, object]] = {}

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {"handle": "ses_1", "turn_event_id": "evt_1", "turn_started_at": "t0"}

    captured: list[str] = []
    app = build_fake_seam(
        behaviors={"start_turn": start_turn}, captured_auth=captured, captured_arguments=seen
    )
    async with fake_seam_lifespan(app):
        await _seam_client(app).start_turn(token="tok", message="hello")
    assert "bundle" not in seen["start_turn"], (
        "omitting bundle must omit the wire argument entirely, not send bundle=null"
    )


async def test_authorization_header_carries_the_per_call_token(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    """The load-bearing test: two calls on ONE client, two different tokens."""

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {"handle": "ses_1", "turn_event_id": "evt_1", "turn_started_at": "t0"}

    captured: list[str] = []
    app = build_fake_seam(behaviors={"start_turn": start_turn}, captured_auth=captured)
    client = _seam_client(app)
    async with fake_seam_lifespan(app):
        await client.start_turn(token="token-a", message="hello")
        headers_after_first = list(captured)
        captured.clear()
        await client.start_turn(token="token-b", message="hello")
        headers_after_second = list(captured)

    assert headers_after_first, "the first call must have reached the transport"
    assert headers_after_second, "the second call must have reached the transport"
    assert set(headers_after_first) == {"Bearer token-a"}, headers_after_first
    assert set(headers_after_second) == {"Bearer token-b"}, headers_after_second
    assert set(headers_after_first) != set(headers_after_second), (
        "the same client object must carry a DIFFERENT bearer per call, "
        "proving the token is not held as instance state"
    )


async def test_list_events_forwards_created_at_gte_and_types_and_returns_items(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    seen: dict[str, dict[str, object]] = {}

    def list_events(_args: dict[str, object]) -> dict[str, object]:
        return {
            "items": [{"type": "agent.message", "text": "hi"}],
            "next_page": None,
        }

    captured: list[str] = []
    app = build_fake_seam(
        behaviors={"list_events": list_events}, captured_auth=captured, captured_arguments=seen
    )
    async with fake_seam_lifespan(app):
        result = await _seam_client(app).list_events(
            token="tok",
            handle="ses_1",
            created_at_gte="2026-01-01T00:00:00Z",
            types=["agent.message", "session.status_idle"],
        )
    assert seen["list_events"]["created_at_gte"] == "2026-01-01T00:00:00Z"
    assert seen["list_events"]["types"] == ["agent.message", "session.status_idle"]
    assert result.items == [{"type": "agent.message", "text": "hi"}]
    assert result.next_page is None


async def test_get_turn_cost_parses_string_cost_usd_into_exact_decimal(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    def get_turn_cost(_args: dict[str, object]) -> dict[str, object]:
        return {"cost_usd": "0.123456", "event_count": 7}

    captured: list[str] = []
    app = build_fake_seam(behaviors={"get_turn_cost": get_turn_cost}, captured_auth=captured)
    async with fake_seam_lifespan(app):
        result = await _seam_client(app).get_turn_cost(
            token="tok", handle="ses_1", turn_started_at="t0", turn_event_id="evt_1"
        )
    assert result.cost_usd == Decimal("0.123456"), (
        "cost_usd must parse to an exact Decimal, not a float-rounded value"
    )
    assert result.event_count == 7


async def test_get_turn_cost_with_null_cost_usd_returns_none(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    def get_turn_cost(_args: dict[str, object]) -> dict[str, object]:
        return {"cost_usd": None, "event_count": 3}

    captured: list[str] = []
    app = build_fake_seam(behaviors={"get_turn_cost": get_turn_cost}, captured_auth=captured)
    async with fake_seam_lifespan(app):
        result = await _seam_client(app).get_turn_cost(
            token="tok", handle="ses_1", turn_started_at="t0", turn_event_id="evt_1"
        )
    assert result.cost_usd is None, "a null cost_usd must stay None, never coerce to zero"


async def test_tool_error_with_bundle_expired_wording_raises_bundle_expired_error(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        raise Exception("bundle expired; re-upload")  # noqa: TRY002

    captured: list[str] = []
    app = build_fake_seam(behaviors={"start_turn": start_turn}, captured_auth=captured)
    async with fake_seam_lifespan(app):
        with pytest.raises(BundleExpiredError):
            await _seam_client(app).start_turn(token="tok", message="hello", bundle="stale")


async def test_tool_error_with_other_wording_raises_seam_error_not_bundle_expired(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        raise Exception("bundle not found")  # noqa: TRY002

    captured: list[str] = []
    app = build_fake_seam(behaviors={"start_turn": start_turn}, captured_auth=captured)
    async with fake_seam_lifespan(app):
        with pytest.raises(SeamError) as excinfo:
            await _seam_client(app).start_turn(token="tok", message="hello", bundle="bad")
    assert not isinstance(excinfo.value, BundleExpiredError), (
        "only the exact bundle-expired wording may raise BundleExpiredError"
    )


async def test_unauthorized_bearer_raises_seam_unauthorized_error(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {"handle": "ses_1", "turn_event_id": "evt_1", "turn_started_at": "t0"}

    captured: list[str] = []
    app = build_fake_seam(
        behaviors={"start_turn": start_turn},
        captured_auth=captured,
        unauthorized_tokens=frozenset({"revoked-token"}),
    )
    async with fake_seam_lifespan(app):
        with pytest.raises(SeamUnauthorizedError):
            await _seam_client(app).start_turn(token="revoked-token", message="hello")


async def test_call_tool_tolerates_a_two_tuple_from_streamable_http_client(
    monkeypatch: pytest.MonkeyPatch,
    build_fake_seam: FakeSeamBuilder,
    fake_seam_lifespan: FakeSeamLifespan,
) -> None:
    """Pins the fix for the 1.x-vs-2.x unpack: `_call_tool` must index into
    ``streamable_http_client``'s result rather than destructure it, so a
    2-tuple (mcp 2.x's narrower yield) works exactly like the 3-tuple mcp 1.x
    yields today.

    No installed mcp release actually produces a 2-tuple (this project pins
    ``mcp<2``), so the real transport is wrapped and its third element is
    dropped — a validated fake that speaks the real wire protocol underneath,
    not a stand-in for the whole client, only for the shape this test needs.
    """

    @contextlib.asynccontextmanager
    async def two_tuple_streamable_http_client(
        url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        terminate_on_close: bool = True,
    ) -> AsyncIterator[
        tuple[
            MemoryObjectReceiveStream[SessionMessage | Exception],
            MemoryObjectSendStream[SessionMessage],
        ]
    ]:
        async with real_streamable_http_client(
            url, http_client=http_client, terminate_on_close=terminate_on_close
        ) as (read, write, _get_session_id):
            yield read, write

    monkeypatch.setattr(
        mcp_client_module, "streamable_http_client", two_tuple_streamable_http_client
    )

    def start_turn(_args: dict[str, object]) -> dict[str, object]:
        return {"handle": "ses_1", "turn_event_id": "evt_1", "turn_started_at": "t0"}

    captured: list[str] = []
    app = build_fake_seam(behaviors={"start_turn": start_turn}, captured_auth=captured)
    async with fake_seam_lifespan(app):
        result = await _seam_client(app).start_turn(token="tok", message="hello")
    assert result == StartedTurn(handle="ses_1", turn_event_id="evt_1", turn_started_at="t0"), (
        "a 2-tuple from streamable_http_client must not raise an unpack error"
    )


async def test_archive_session_returns_none_and_issues_exactly_one_call(
    build_fake_seam: FakeSeamBuilder, fake_seam_lifespan: FakeSeamLifespan
) -> None:
    calls: list[dict[str, object]] = []

    def archive_my_session(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {"handle": args["handle"], "archived": "true"}

    captured: list[str] = []
    app = build_fake_seam(
        behaviors={"archive_my_session": archive_my_session}, captured_auth=captured
    )
    async with fake_seam_lifespan(app):
        result = await _seam_client(app).archive_session(token="tok", handle="ses_1")
    assert result is None
    assert len(calls) == 1, "archive_session must issue exactly one tool call"


async def test_push_bundle_sends_gzip_content_type_and_returns_parsed_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "bundle": "bundle-handle-xyz",
                "sha256": "deadbeef",
                "size_bytes": 5,
                "expires_at": "2026-02-01T00:00:00Z",
            },
        )

    client = SeamClient(
        mcp_url="http://testserver/mcp",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_bundle_bytes=1024,
    )
    result = await client.push_bundle(token="tok", archive=b"12345", size_bytes=5)
    assert result == BundlePushed(
        handle="bundle-handle-xyz",
        sha256="deadbeef",
        size_bytes=5,
        expires_at="2026-02-01T00:00:00Z",
    )
    assert len(requests) == 1
    assert requests[0].headers["content-type"] == "application/gzip"
    assert requests[0].url.path == "/bundles"


async def test_push_bundle_over_cap_raises_before_any_request_is_issued() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = SeamClient(
        mcp_url="http://testserver/mcp",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_bundle_bytes=10,
    )
    with pytest.raises(SeamError):
        await client.push_bundle(token="tok", archive=b"x" * 20, size_bytes=20)
    assert requests == [], "an over-cap bundle must never open a connection to the seam"

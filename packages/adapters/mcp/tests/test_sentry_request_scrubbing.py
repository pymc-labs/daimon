"""OAuth callback queries, headers and cookies never leave the process via Sentry.

Runs the production `init_sentry` with the MCP server's StarletteIntegration
against an in-process Starlette app and an in-memory transport.
"""

from __future__ import annotations

import secrets
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sentry_sdk
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.slack_file_proxy import build_slack_file_proxy_route
from daimon.adapters.mcp.uploads import build_upload_route
from daimon.core.observability import capture_exception_with_scope, init_sentry
from daimon.core.slack_file_token import mint_file_token
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from sentry_sdk.envelope import Envelope


class _CapturingTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[object] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            self.payloads.append(
                item.payload.json if item.payload.json is not None else item.payload.bytes
            )


@pytest.mark.parametrize("raise_unhandled", [False, True], ids=["captured", "unhandled"])
def test_oauth_callback_code_state_headers_and_cookies_never_reach_the_transport(
    monkeypatch: pytest.MonkeyPatch, raise_unhandled: bool
) -> None:
    code, state, bearer, cookie = (f"canary-{n}-{uuid.uuid4().hex}" for n in range(4))
    transport = _CapturingTransport()
    real_init: Callable[..., object] = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, "init", lambda *a, **k: real_init(*a, transport=transport, **k))
    init_sentry(
        dsn="https://public@o0.ingest.sentry.io/0",
        environment="test",
        process="mcp",
        release=None,
        traces_sample_rate=1.0,
        integrations=[StarletteIntegration()],
    )

    async def callback(request: Request) -> PlainTextResponse:
        try:
            raise RuntimeError("token exchange failed")
        except RuntimeError as exc:
            if raise_unhandled:
                raise
            capture_exception_with_scope(exc)
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/oauth/callback", callback)])
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            client.get(
                f"/oauth/callback?code={code}&state={state}&next=/home",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "X-Api-Key": bearer,
                    "X-Slack-Token": bearer,
                    "Cookie": f"session={cookie}",
                },
            )
        sentry_sdk.flush()
    finally:
        sentry_sdk.init()

    rendered = repr(transport.payloads)
    assert "token exchange failed" in rendered, "the error event was captured"
    for canary in (code, state, bearer, cookie):
        assert canary not in rendered


def _init_capturing(monkeypatch: pytest.MonkeyPatch) -> _CapturingTransport:
    transport = _CapturingTransport()
    real_init: Callable[..., object] = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, "init", lambda *a, **k: real_init(*a, transport=transport, **k))
    init_sentry(
        dsn="https://public@o0.ingest.sentry.io/0",
        environment="test",
        process="mcp",
        release=None,
        traces_sample_rate=1.0,
        integrations=[StarletteIntegration()],
    )
    return transport


@asynccontextmanager
async def _broken_database() -> AsyncIterator[object]:
    raise RuntimeError("database unavailable")
    yield  # pragma: no cover


async def _no_fetch(*args: object) -> object:
    raise AssertionError("no upstream request in this test")


_FILE_SECRET = "test-signing-key"


def _routes(database: Callable[[], object], fetch: Callable[..., object]) -> list[Route]:
    return [
        Route("/uploads/{token}", build_upload_route(database), methods=["PUT"]),  # pyright: ignore[reportArgumentType]
        Route(
            "/slack/file/{token}",
            build_slack_file_proxy_route(
                sessionmaker=database,  # pyright: ignore[reportArgumentType]
                fernet=MultiFernet([Fernet(Fernet.generate_key())]),
                secret=_FILE_SECRET,
                fetch_file=fetch,  # pyright: ignore[reportArgumentType]
                now=lambda: 1000,
            ),
        ),
    ]


@pytest.mark.parametrize("route", ["upload", "slack-file"])
def test_capability_tokens_in_paths_never_reach_the_transport_on_errors(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Production route handlers failing on the database: no token in any envelope item."""
    transport = _init_capturing(monkeypatch)
    if route == "upload":
        token, method, path = secrets.token_urlsafe(24), "PUT", "/uploads/"
    else:
        token = mint_file_token(team_id="T1", file_id="F1", exp=2000, secret=_FILE_SECRET)
        method, path = "GET", "/slack/file/"
    app = Starlette(routes=_routes(_broken_database, _no_fetch))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.request(method, path + token)
        sentry_sdk.flush()
    finally:
        sentry_sdk.init()

    assert response.status_code == 500
    rendered = repr(transport.payloads)
    assert "database unavailable" in rendered, "the error event was captured"
    assert token not in rendered
    for part in token.split("."):
        assert part not in rendered


def test_slack_file_token_never_reaches_the_transport_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with tracing on still carries the URL; the token must be gone."""
    transport = _init_capturing(monkeypatch)
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    token = mint_file_token(team_id="T1", file_id="F1", exp=2000, secret=_FILE_SECRET)

    @asynccontextmanager
    async def _database() -> AsyncIterator[object]:
        yield object()

    async def _bot_token(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(encrypted_token=fernet.encrypt(b"bot-token"))

    async def _fetch(*args: object) -> tuple[bytes, str, str]:
        return b"file", "text/plain", "f.txt"

    monkeypatch.setattr("daimon.adapters.mcp.slack_file_proxy.get_slack_bot_token", _bot_token)
    handler = build_slack_file_proxy_route(
        sessionmaker=_database,  # pyright: ignore[reportArgumentType]
        fernet=fernet,
        secret=_FILE_SECRET,
        fetch_file=_fetch,  # pyright: ignore[reportArgumentType]
        now=lambda: 1000,
    )
    app = Starlette(routes=[Route("/slack/file/{token}", handler)])
    try:
        with TestClient(app) as client:
            response = client.get("/slack/file/" + token)
        sentry_sdk.flush()
    finally:
        sentry_sdk.init()

    assert response.status_code == 200
    assert transport.payloads, "the transaction was captured"
    rendered = repr(transport.payloads)
    assert token not in rendered
    for part in token.split("."):
        assert part not in rendered

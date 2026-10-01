"""OAuth callback queries, headers and cookies never leave the process via Sentry.

Runs the production `init_sentry` with the MCP server's StarletteIntegration
against an in-process Starlette app and an in-memory transport.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
import sentry_sdk
from daimon.core.observability import capture_exception_with_scope, init_sentry
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Callable

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

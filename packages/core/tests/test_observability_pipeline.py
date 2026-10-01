"""End-to-end Sentry scrubbing: real `init_sentry` → SDK pipeline → transport.

These drive the production configuration (before_send, event scrubber,
locals/breadcrumb settings) and assert on the final envelope, not on
`_scrub_event` in isolation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import pytest
import sentry_sdk
from daimon.core.observability import capture_exception_with_scope, init_sentry
from sentry_sdk.transport import Transport

if TYPE_CHECKING:
    from sentry_sdk.envelope import Envelope


class CapturingTransport(Transport):
    """Keeps every envelope item's payload in memory instead of sending it."""

    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[object] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            self.payloads.append(
                item.payload.json if item.payload.json is not None else item.payload.bytes
            )

    def rendered(self) -> str:
        return repr(self.payloads)


def init_capturing_sentry(
    monkeypatch: pytest.MonkeyPatch, *, integrations: list[object] | None = None
) -> CapturingTransport:
    """Run the production `init_sentry`, adding only an in-memory transport."""
    transport = CapturingTransport()
    real_init: Callable[..., object] = sentry_sdk.init

    def _init_with_transport(*args: object, **kwargs: object) -> object:
        return real_init(*args, transport=transport, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", _init_with_transport)
    init_sentry(
        dsn="https://public@o0.ingest.sentry.io/0",
        environment="test",
        process="mcp",
        release=None,
        traces_sample_rate=1.0,
        integrations=integrations or [],  # pyright: ignore[reportArgumentType]
    )
    return transport


@pytest.fixture
def capturing_sentry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CapturingTransport]:
    transport = init_capturing_sentry(monkeypatch)
    try:
        yield transport
    finally:
        sentry_sdk.flush()
        sentry_sdk.init()  # reset to a disabled client


def _canary() -> str:
    return "canary-" + uuid.uuid4().hex


@pytest.mark.parametrize(
    "render",
    [
        lambda c: json.dumps({"api_key": c}),
        lambda c: json.dumps({"detail": {"access_token": c, "scope": "repo"}}),
        lambda c: f"HTTP 400 from provider: {json.dumps({'error': 'bad', 'refresh_token': c})}",
        lambda c: repr({"access_token": c}),
        lambda c: repr({"nested": [{"client_secret": c}]}),
        lambda c: f"rejected 'password': '{c}' for user",
        lambda c: f'oauth failed "code": "{c}"',
    ],
    ids=[
        "json",
        "nested-json",
        "json-in-text",
        "dict-repr",
        "nested-repr",
        "quoted-pair",
        "dq-pair",
    ],
)
def test_structured_secret_in_exception_text_never_reaches_the_transport(
    capturing_sentry: CapturingTransport, render: Callable[[str], str]
) -> None:
    canary = _canary()
    try:
        raise RuntimeError(render(canary))
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    assert capturing_sentry.payloads, "the event was captured"
    assert canary not in capturing_sentry.rendered()
    assert "RuntimeError" in capturing_sentry.rendered()


def test_secret_in_a_log_message_never_reaches_the_transport(
    capturing_sentry: CapturingTransport,
) -> None:
    canary = _canary()
    sentry_sdk.capture_message(f"callback failed: {json.dumps({'token': canary})} code={canary}")
    sentry_sdk.flush()

    assert capturing_sentry.payloads
    assert canary not in capturing_sentry.rendered()


def test_transaction_events_are_scrubbed_too(capturing_sentry: CapturingTransport) -> None:
    canary = _canary()
    with sentry_sdk.start_transaction(name="oauth-callback", op="http.server") as txn:
        txn.set_data("url", f"https://h/oauth/callback?code={canary}&state={canary}")
        sentry_sdk.get_current_scope().set_context("request_info", {"token": canary})
    sentry_sdk.flush()

    assert capturing_sentry.payloads, "the transaction was captured"
    assert canary not in capturing_sentry.rendered()

"""Unit tests for daimon.core.observability.

Tests cover:
  - init_sentry no-ops when dsn is None
  - _scrub_event redacts secret-keyed tag values
  - _scrub_event drops request.data and extra fields
  - capture_exception_with_scope tags from contextvars, omits unbound, never raises
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
import sentry_sdk
from daimon.core.observability import (
    _scrub_event,
    capture_exception_with_scope,
    init_sentry,
)
from sentry_sdk.transport import Transport
from structlog.contextvars import bind_contextvars, clear_contextvars

if TYPE_CHECKING:
    from sentry_sdk._types import Event
    from sentry_sdk.envelope import Envelope


class _RecordingTransport(Transport):
    """A real sentry Transport that records captured events instead of sending them.

    Driving Sentry through a transport (not an AsyncMock on capture_exception) runs the
    real scope-application + before_send pipeline, so the recorded tags are exactly what
    would ship in production.
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture
def recording_sentry() -> Iterator[_RecordingTransport]:
    """Init Sentry with a recording transport and clean contextvars before/after."""
    clear_contextvars()
    transport = _RecordingTransport()
    sentry_sdk.init(
        dsn="https://public@o0.ingest.sentry.io/0",
        transport=transport,
        before_send=_scrub_event,
    )
    try:
        yield transport
    finally:
        clear_contextvars()
        sentry_sdk.flush()


def _captured_tags(transport: _RecordingTransport) -> dict[str, object]:
    """Return the tags dict of the single captured event (asserting exactly one)."""
    assert len(transport.events) == 1, (
        f"expected exactly one captured event, got {len(transport.events)}"
    )
    tags = transport.events[0].get("tags")
    return dict(tags) if isinstance(tags, dict) else {}


def test_capture_tags_all_three_when_rid_tenant_guild_bound(
    recording_sentry: _RecordingTransport,
) -> None:
    """All three contextvars bound → the captured event carries all three id tags."""
    bind_contextvars(rid="r-1", tenant_id="t-1", guild_id="g-1")

    capture_exception_with_scope(ValueError("boom"))
    sentry_sdk.flush()

    tags = _captured_tags(recording_sentry)
    assert tags.get("rid") == "r-1", "rid tag should carry the bound contextvar value"
    assert tags.get("tenant_id") == "t-1", "tenant_id tag should carry the bound value"
    assert tags.get("guild_id") == "g-1", "guild_id tag should carry the bound value"


def test_capture_omits_unbound_tags_when_only_tenant_bound(
    recording_sentry: _RecordingTransport,
) -> None:
    """Only tenant_id bound → event has a tenant_id tag and NO rid/guild_id tags."""
    bind_contextvars(tenant_id="t-only")

    capture_exception_with_scope(ValueError("boom"))
    sentry_sdk.flush()

    tags = _captured_tags(recording_sentry)
    assert tags.get("tenant_id") == "t-only", "tenant_id tag should be present"
    assert "rid" not in tags, "rid tag must be omitted when rid is unbound"
    assert "guild_id" not in tags, "guild_id tag must be omitted when guild_id is unbound"


def test_capture_still_records_when_nothing_bound(
    recording_sentry: _RecordingTransport,
) -> None:
    """Nothing bound (e.g. early OAuth) → event still captures with no id tags."""
    capture_exception_with_scope(ValueError("boom"))
    sentry_sdk.flush()

    tags = _captured_tags(recording_sentry)
    assert "rid" not in tags, "rid tag must be omitted when unbound"
    assert "tenant_id" not in tags, "tenant_id tag must be omitted when unbound"
    assert "guild_id" not in tags, "guild_id tag must be omitted when unbound"


def test_capture_returns_none_and_does_not_raise(
    recording_sentry: _RecordingTransport,
) -> None:
    """The helper returns None and never raises — call-site control flow is preserved."""
    bind_contextvars(rid="r-x")

    result = capture_exception_with_scope(RuntimeError("kaboom"))

    assert result is None, "capture_exception_with_scope must return None"


def test_init_sentry_noops_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """init_sentry(dsn=None) must return without calling sentry_sdk.init."""
    import sentry_sdk

    call_count: list[int] = []

    def _recording_init(*args: object, **kwargs: object) -> None:
        call_count.append(1)

    monkeypatch.setattr(sentry_sdk, "init", _recording_init)

    init_sentry(
        dsn=None,
        environment="production",
        process="discord",
        release=None,
        traces_sample_rate=0.0,
        integrations=[],
    )

    assert len(call_count) == 0, "init_sentry should not call sentry_sdk.init when dsn is None"


def test_scrub_event_redacts_token_when_secret_key_in_tags() -> None:
    """_scrub_event replaces values whose tag key matches the secret denylist."""
    event: dict[str, object] = {
        "tags": {
            "authorization": "Bearer super-secret-token",
            "tenant_id": "t-abc123",
        }
    }
    hint: dict[str, object] = {}

    result = _scrub_event(event, hint)  # type: ignore[arg-type]

    assert result is not None, "_scrub_event should return the event (not None)"
    tags = result.get("tags")
    assert isinstance(tags, dict), "tags should remain a dict after scrubbing"
    assert tags["authorization"] == "[redacted]", (
        "secret-keyed tag value should be replaced with [redacted]"
    )
    assert tags["tenant_id"] == "t-abc123", "non-secret tag values should be left intact"


def test_scrub_event_drops_message_body_when_request_data_present() -> None:
    """_scrub_event removes request.data so user message content cannot leave the process."""
    event: dict[str, object] = {
        "request": {
            "url": "https://example.com/",
            "data": "hello, this is raw user message content",
        },
        "extra": {"user_turn": "some private message"},
    }
    hint: dict[str, object] = {}

    result = _scrub_event(event, hint)  # type: ignore[arg-type]

    assert result is not None, "_scrub_event should return the event (not None)"
    request = result.get("request")
    assert isinstance(request, dict), "request field should remain a dict"
    assert "data" not in request, (
        "request.data must be removed to prevent user message content from leaving the process"
    )
    assert "extra" not in result, (
        "extra field must be removed to prevent arbitrary payload from leaving the process"
    )

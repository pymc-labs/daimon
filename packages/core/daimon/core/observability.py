"""Sentry observability helpers for daimon-core.

Functional-core / imperative-shell split:
  - Pure:  _scrub_event (before_send callback)
  - Shell: init_sentry (the single I/O escape — calls sentry_sdk.init)

Do NOT call sentry_sdk.init at module import time (architecture rule 3).
Adapters call init_sentry once at their entrypoint (Plan 02).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import sentry_sdk
import structlog
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint
    from sentry_sdk.integrations import Integration

# Keys whose values must never leave the process in a Sentry event.
_APP_DENYLIST: list[str] = [
    "bot_token",
    "jwt",
    "api_key",
    "google_sa_json",
]

# Keys whose values are message/body content — drop the entire field.
_BODY_FIELDS: frozenset[str] = frozenset({"data", "body", "content", "message_body"})

# Case-insensitive set of all secret-keyed patterns.
_SECRET_KEYS: frozenset[str] = frozenset(
    k.lower()
    for k in [
        *DEFAULT_DENYLIST,
        *_APP_DENYLIST,
    ]
)


def _scrub_event(event: Event, hint: Hint) -> Event | None:
    """Pure before_send callback: redact secrets and drop message-body fields.

    1. Removes request.data and extra fields that could carry user message content.
    2. Redacts any value whose key matches the secret denylist (case-insensitive).
    Returns the scrubbed event dict, or None to drop the event entirely.
    """
    # Drop request body — may contain raw user message content.
    request = event.get("request")
    if isinstance(request, dict) and "data" in request:
        del request["data"]

    # Drop extra fields entirely — arbitrary payload, too risky.
    if "extra" in event:
        del event["extra"]

    # Redact secret-keyed values anywhere in the event tags mapping.
    tags = event.get("tags")
    if isinstance(tags, dict):
        for key in list(tags.keys()):
            if key.lower() in _SECRET_KEYS:
                tags[key] = "[redacted]"

    return event


# The id-only Sentry scope tag keys used by the omit-unbound capture helper.
_SCOPE_TAG_KEYS: tuple[str, str, str] = ("tenant_id", "rid", "guild_id")


def capture_exception_with_scope(exc: BaseException) -> None:
    """Capture an exception, tagging it with whatever id contextvars are bound.

    Reads rid/tenant_id/guild_id from structlog contextvars and sets a Sentry tag for
    each one that is bound, omitting any that is not. Adds only id tags — never the
    exception's content — so it inherits the existing _scrub_event / EventScrubber PII
    protection. Changes no control flow: call it inside an existing except block; it
    returns None and never raises.
    """
    bound = structlog.contextvars.get_contextvars()
    with sentry_sdk.new_scope() as scope:
        for key in _SCOPE_TAG_KEYS:
            value = bound.get(key)
            if value is not None:
                scope.set_tag(key, str(value))
        sentry_sdk.capture_exception(exc)


def init_sentry(
    *,
    dsn: str | None,
    environment: str,
    process: Literal["discord", "mcp", "scheduler", "slack"],
    release: str | None,
    traces_sample_rate: float,
    integrations: list[Integration],
) -> None:
    """Shell helper: initialise Sentry for one process group.

    No-ops when dsn is None so dev / self-host deployments keep booting
    without any Sentry configuration (mirrors McpSettings optional pattern).

    Args:
        dsn: Sentry DSN string. None = Sentry disabled.
        environment: Sentry environment tag (e.g. "production", "staging").
        process: Process group name — set as a Sentry tag post-init.
        release: Optional release identifier (e.g. git SHA).
        traces_sample_rate: Fraction of transactions to sample for tracing.
            0.0 disables tracing while still capturing errors.
        integrations: SDK integrations to enable (e.g. AsyncioIntegration).
            Each process group passes its own list (Plan 02).
    """
    if dsn is None:
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        send_default_pii=False,
        traces_sample_rate=traces_sample_rate,
        before_send=_scrub_event,
        event_scrubber=EventScrubber(
            denylist=[*DEFAULT_DENYLIST, *_APP_DENYLIST],
        ),
        integrations=integrations,
    )
    sentry_sdk.set_tag("process", process)

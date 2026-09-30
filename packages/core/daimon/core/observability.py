"""Sentry observability helpers for daimon-core.

Functional-core / imperative-shell split:
  - Pure:  _scrub_event (before_send callback)
  - Shell: init_sentry (the single I/O escape — calls sentry_sdk.init)

Do NOT call sentry_sdk.init at module import time (architecture rule 3).
Adapters call init_sentry once at their entrypoint (Plan 02).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal, cast

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


# Secret-shaped substrings inside free text: key=value pairs whose key names a
# secret, bearer credentials, and Fernet tokens/keys.
_SECRET_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:access_token|refresh_token|token|api_key|apikey|secret|password|"
        r"passwd|authorization|code_verifier|client_secret)(?:%3D|=|:\s*)"
        r"['\"]?)[^\s&'\",;]+"
    ),
    re.compile(r"()gAAAAA[A-Za-z0-9_-]{20,}={0,2}"),
    re.compile(r"()\b[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])"),
)


def _redact_secret_text(text: str) -> str:
    for pattern in _SECRET_TEXT_PATTERNS:
        text = pattern.sub(r"\1[redacted]", text)
    return text


def _redact_secret_keys(mapping: dict[str, object]) -> None:
    """Redact values whose key is on the secret denylist, recursing into dicts/lists."""
    for key in list(mapping.keys()):
        value = mapping[key]
        if key.lower() in _SECRET_KEYS:
            mapping[key] = "[redacted]"
        elif isinstance(value, dict):
            _redact_secret_keys(cast("dict[str, object]", value))
        elif isinstance(value, list):
            for item in cast("list[object]", value):
                if isinstance(item, dict):
                    _redact_secret_keys(cast("dict[str, object]", item))
        elif isinstance(value, str):
            mapping[key] = _redact_secret_text(value)


def _drop_frame_vars(section: object) -> None:
    """Remove ``vars`` from every stack frame under an exception/threads section."""
    if not isinstance(section, dict):
        return
    values = cast("dict[str, object]", section).get("values")
    if not isinstance(values, list):
        return
    for value in cast("list[object]", values):
        if not isinstance(value, dict):
            continue
        stacktrace = cast("dict[str, object]", value).get("stacktrace")
        if not isinstance(stacktrace, dict):
            continue
        frames = cast("dict[str, object]", stacktrace).get("frames")
        if not isinstance(frames, list):
            continue
        for frame in cast("list[object]", frames):
            if isinstance(frame, dict):
                cast("dict[str, object]", frame).pop("vars", None)


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

    # Drop captured frame locals: a frame can hold decrypted agent keys or
    # submitted form values under any variable name. init_sentry also turns
    # include_local_variables off; this covers any other client config.
    for section in ("exception", "threads"):
        _drop_frame_vars(event.get(section))

    # Breadcrumbs replay earlier log lines and HTTP calls (URLs with tokens);
    # init_sentry records none, and any another config sends are dropped.
    event.pop("breadcrumbs", None)

    # Contexts are free-form mappings: redact secret-keyed values at any depth.
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        _redact_secret_keys(cast("dict[str, object]", contexts))

    # Exception messages can quote a secret (a URL query, a header, a key).
    exceptions = event.get("exception")
    if isinstance(exceptions, dict):
        for exc_value in cast("list[object]", exceptions.get("values") or []):
            if isinstance(exc_value, dict):
                entry = cast("dict[str, object]", exc_value)
                text = entry.get("value")
                if isinstance(text, str):
                    entry["value"] = _redact_secret_text(text)

    # Redact secret-keyed values anywhere in the event tags mapping.
    tags = event.get("tags")
    if isinstance(tags, dict):
        for key in list(tags.keys()):
            if key.lower() in _SECRET_KEYS:
                tags[key] = "[redacted]"

    return event


def _event_scrubber() -> EventScrubber:
    """Sentry's denylist scrubber, recursive: a token nested in a frame local
    (a request dict, an SDK activity) is as secret as a top-level one."""
    return EventScrubber(denylist=[*DEFAULT_DENYLIST, *_APP_DENYLIST], recursive=True)


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
    process: Literal["discord", "mcp", "scheduler", "slack", "teams"],
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
        include_local_variables=False,
        max_breadcrumbs=0,
        traces_sample_rate=traces_sample_rate,
        before_send=_scrub_event,
        event_scrubber=_event_scrubber(),
        integrations=integrations,
    )
    sentry_sdk.set_tag("process", process)

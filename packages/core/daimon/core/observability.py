"""Sentry observability helpers for daimon-core.

Functional-core / imperative-shell split:
  - Pure:  _scrub_event (before_send callback)
  - Shell: init_sentry (the single I/O escape — calls sentry_sdk.init)

Do NOT call sentry_sdk.init at module import time (architecture rule 3).
Adapters call init_sentry once at their entrypoint (Plan 02).
"""

from __future__ import annotations

import ast
import json
import re
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


# Names that mark a value as secret wherever they appear as a key: a query
# parameter, a header, a JSON/dict field or a context entry. OAuth's `code`
# and `state` are bearer material for the few minutes they are valid.
_SECRET_NAME_EXACT: frozenset[str] = frozenset(
    {
        *_SECRET_KEYS,
        "code",
        "state",
        "k",
        "key",
        "sig",
        "signature",
        "token",
        "id_token",
        "access_token",
        "refresh_token",
        "client_secret",
        "code_verifier",
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
    }
)
_SECRET_NAME_PATTERN = re.compile(
    r"(?i)(token|secret|passw|api[_-]?key|auth|cookie|session|signature|credential|"
    r"private[_-]?key|verifier)"
)
# The same names as an alternation for free-text `name=value` / `name: value`.
_SECRET_TEXT_NAMES = (
    r"(?:access_token|refresh_token|id_token|token|api[_-]?key|apikey|client_secret|secret|"
    r"password|passwd|authorization|code_verifier|code|state|signature|sig|session|cookie|"
    r"private[_-]?key)"
)

# Free-text patterns, applied after structured redaction: bearer credentials,
# unquoted name=value pairs, and Fernet tokens/keys.
_SECRET_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\b" + _SECRET_TEXT_NAMES + r"(?:%3D|=|:\s*)['\"]?)[^\s&'\",;}\]]+"),
    re.compile(r"()gAAAAA[A-Za-z0-9_-]{20,}={0,2}"),
    re.compile(r"()\b[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])"),
)
# A quoted key followed by a quoted value, as in JSON or a Python dict repr.
_QUOTED_PAIR = re.compile(r"""(["'])([^"'\\\n]{1,64})\1(\s*:\s*)(["'])((?:\\.|(?!\4)[^\\])*)\4""")
_STRUCTURED_TEXT_LIMIT = 64 * 1024
_REDACTED = "[redacted]"


def _is_secret_name(name: str) -> bool:
    lowered = name.strip().lower()
    return lowered in _SECRET_NAME_EXACT or _SECRET_NAME_PATTERN.search(lowered) is not None


def _redact_value(value: object) -> object:
    """Redact secrets inside any JSON-like value, by key at every depth."""
    if isinstance(value, dict):
        out: dict[object, object] = {}
        for key, item in cast("dict[object, object]", value).items():
            if isinstance(key, str) and _is_secret_name(key):
                out[key] = _REDACTED
            else:
                out[key] = _redact_value(item)
        return out
    if isinstance(value, list):
        return [_redact_value(item) for item in cast("list[object]", value)]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in cast("tuple[object, ...]", value))
    if isinstance(value, str):
        return _redact_secret_text(value)
    return value


def _redact_embedded_structures(text: str) -> str:
    """Parse JSON or a dict/list repr embedded in `text` and redact it by key."""
    if len(text) > _STRUCTURED_TEXT_LIMIT:
        return text
    decoder = json.JSONDecoder()
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] not in "{[":
            out.append(text[i])
            i += 1
            continue
        try:
            parsed, end = decoder.raw_decode(text, i)
            rendered = json.dumps(_redact_value(parsed))
        except ValueError:
            closer = "}" if text[i] == "{" else "]"
            end = text.rfind(closer) + 1
            try:
                parsed = ast.literal_eval(text[i:end]) if end > i else None
            except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
                parsed = None
            if not isinstance(parsed, (dict, list, tuple)):
                out.append(text[i])
                i += 1
                continue
            rendered = repr(_redact_value(cast("object", parsed)))
        out.append(rendered)
        i = end
    return "".join(out)


def _redact_quoted_pairs(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        if not _is_secret_name(match.group(2)):
            return match.group(0)
        key_quote, key, colon, quote = match.group(1, 2, 3, 4)
        return f"{key_quote}{key}{key_quote}{colon}{quote}{_REDACTED}{quote}"

    return _QUOTED_PAIR.sub(_sub, text)


def _redact_secret_text(text: str) -> str:
    """Redact secrets in free text: embedded JSON/reprs, quoted pairs, name=value."""
    if any(c in text for c in "{["):
        text = _redact_embedded_structures(text)
    text = _redact_quoted_pairs(text)
    for pattern in _SECRET_TEXT_PATTERNS:
        text = pattern.sub(r"\1" + _REDACTED, text)
    return text


def _redact_secret_keys(mapping: dict[str, object]) -> None:
    """Redact secret-named values in place, at any depth, including list items."""
    for key in list(mapping.keys()):
        if _is_secret_name(key):
            mapping[key] = _REDACTED
        else:
            mapping[key] = _redact_value(mapping[key])


def _redact_url(url: str) -> str:
    """Keep scheme, host and path; drop userinfo, query and fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _REDACTED
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _redact_query(query: str) -> str:
    """Keep parameter names, redact every value."""
    pairs = parse_qsl(query, keep_blank_values=True)
    return urlencode([(name, _REDACTED) for name, _ in pairs], safe="[]")


def _scrub_request(request: dict[str, object]) -> None:
    for field in ("data", "cookies", "env"):
        request.pop(field, None)
    url = request.get("url")
    if isinstance(url, str):
        request["url"] = _redact_url(url)
    query = request.get("query_string")
    if isinstance(query, str):
        request["query_string"] = _redact_query(query)
    elif isinstance(query, (bytes, bytearray)):
        request["query_string"] = _redact_query(bytes(query).decode("latin-1"))
    headers = request.get("headers")
    if isinstance(headers, dict):
        typed = cast("dict[str, object]", headers)
        for name in list(typed.keys()):
            lowered = name.lower()
            if _is_secret_name(lowered) or (
                lowered.startswith("x-") and ("token" in lowered or "key" in lowered)
            ):
                typed[name] = _REDACTED
            elif isinstance(typed[name], str) and lowered in ("referer", "origin", "location"):
                typed[name] = _redact_url(cast("str", typed[name]))


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
    # Requests carry OAuth codes/state in the query, credentials in headers
    # and cookies, and arbitrary bodies.
    request = event.get("request")
    if isinstance(request, dict):
        _scrub_request(request)

    # Log-message events (capture_message, logging integration).
    message = event.get("message")
    if isinstance(message, str):
        event["message"] = _redact_secret_text(message)
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        entry = logentry
        for field in ("message", "formatted"):
            text = entry.get(field)
            if isinstance(text, str):
                entry[field] = _redact_secret_text(text)
        entry.pop("params", None)

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

    # Transaction spans: descriptions and data can hold URLs and SQL.
    spans = event.get("spans")
    if isinstance(spans, list):
        for span in cast("list[object]", spans):
            if isinstance(span, dict):
                typed_span = cast("dict[str, object]", span)
                description = typed_span.get("description")
                if isinstance(description, str):
                    typed_span["description"] = _redact_secret_text(description)
                data = typed_span.get("data")
                if isinstance(data, dict):
                    _redact_secret_keys(cast("dict[str, object]", data))
    transaction = event.get("transaction")
    if isinstance(transaction, str):
        event["transaction"] = _redact_secret_text(transaction)

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
        before_send_transaction=_scrub_event,
        event_scrubber=_event_scrubber(),
        integrations=integrations,
    )
    sentry_sdk.set_tag("process", process)

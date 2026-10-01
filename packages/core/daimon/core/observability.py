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


# A name marks its value as secret wherever it appears as a key: a header, a
# JSON/dict field, a context entry or a `NAME=value` pair in free text. The
# rule is one function, so the structured and free-text paths agree.
_SECRET_NAME_EXACT: frozenset[str] = frozenset(
    {
        *_SECRET_KEYS,
        "k",
        "key",
        "sig",
        "signature",
        "token",
        "pwd",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)
_SECRET_NAME_PATTERN = re.compile(
    r"token|secret|passw|pwd|api[_-]?key|apikey|auth|cookie|session|signature|credential|"
    r"private[_-]?key|verifier|bearer|(?:^|[_.-])(?:key|sig|dsn)$"
)
# Names that contain a secret-looking word but carry counts, ids or labels.
_SECRET_NAME_ALLOW = re.compile(
    r"^(?:author|authors|authored|authority|session_id|sessionid_count|token_count|tokens|"
    r"num_tokens|key_count|key_name|key_names|keys)$|_tokens$|_token_count$|^(?:max|min)_token$"
)
# OAuth's `code` and `state` are bearer material, but common words in prose, so
# they are secret only in a query string (handled there) or an OAuth context.
_OAUTH_NAMES: frozenset[str] = frozenset({"code", "state"})

_REDACTED = "[redacted]"
_TEXT_LIMIT = 64 * 1024
_LITERAL_EVAL_LIMIT = 8 * 1024
_MAX_STRUCTURE_ATTEMPTS = 32

_FREE_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # pydantic echoes the rejected input: never keep it.
    (
        re.compile(r"""(input_value=)(?:'(?:[^'\\]|\\.){0,4096}'|"(?:[^"\\]|\\.){0,4096}"|\S+)"""),
        r"\1",
    ),
    # Authorization header in text: keep the scheme, drop the credential.
    (
        re.compile(
            r"(?i)(\bauthorization\s*[:=]\s*['\"]?[A-Za-z][A-Za-z0-9-]{0,20}\s+)[^\s'\",;]+"
        ),
        r"\1",
    ),
    (re.compile(r"(?i)(\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1"),
    # userinfo in a URL: scheme://user:SECRET@host
    (re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]{0,15}://[^\s:/@]{0,64}:)[^\s@/]{1,256}(?=@)"), r"\1"),
    # Fernet tokens and keys.
    (re.compile(r"()gAAAAA[A-Za-z0-9_-]{20,}={0,2}"), r"\1"),
    (re.compile(r"()\b[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])"), r"\1"),
)
# Any `name = value` / `name: value` / `name%3Dvalue`. The value sits in a
# lookahead so a non-secret pair never swallows the pair after it
# (`failed: SLACK_BOT_TOKEN=x`); the scan decides by name.
_NAME_VALUE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z_][A-Za-z0-9_.-]{0,63})(\s{0,4}(?:=|%3[Dd]|:)\s{0,4})"
    r"(?=(['\"]?)([^\s&'\",;:=}\]\)]{1,4096}))"
)
# Characters that end a URL query inside free text.
_QUERY_END = re.compile(r"[\s#'\"<>]")
# Words that mark text as an OAuth exchange, where `code`/`state` are secrets.
_OAUTH_CONTEXT = re.compile(r"(?i)oauth|callback|authori[sz]e|redirect_uri|token exchange")
# A quoted key and the opening quote of its quoted value (JSON, dict repr).
_QUOTED_KEY = re.compile(r"""(["'])([^"'\\\n]{1,64})\1(\s{0,4}:\s{0,4})(["'])""")


def _is_secret_name(name: str, *, oauth: bool = False) -> bool:
    lowered = name.strip().lower()
    if oauth and lowered in _OAUTH_NAMES:
        return True
    if _SECRET_NAME_ALLOW.search(lowered):
        return False
    return lowered in _SECRET_NAME_EXACT or _SECRET_NAME_PATTERN.search(lowered) is not None


def _redact_value(value: object, *, oauth: bool = False, depth: int = 0) -> object:
    """Redact secrets inside any JSON-like value, by key at every depth."""
    if depth > 32:
        return _REDACTED
    if isinstance(value, dict):
        out: dict[object, object] = {}
        for key, item in cast("dict[object, object]", value).items():
            if isinstance(key, str) and _is_secret_name(key, oauth=oauth):
                out[key] = _REDACTED
            else:
                out[key] = _redact_value(item, oauth=oauth, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [
            _redact_value(item, oauth=oauth, depth=depth + 1)
            for item in cast("list[object]", value)
        ]
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(value, str):
        return _redact_secret_text(value)
    return value


def _matching_closers(text: str) -> dict[int, int]:
    """One linear pass: opener index -> index of its matching closer.

    Quotes delimit strings only inside a bracket, so an apostrophe in prose
    doesn't swallow the rest of the message.
    """
    pairs = {"}": "{", "]": "["}
    stack: list[int] = []
    matches: dict[int, int] = {}
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = ""
        elif char in "{[":
            stack.append(i)
        elif char in "}]":
            if stack and text[stack[-1]] == pairs[char]:
                matches[stack.pop()] = i
            else:
                stack.clear()
        elif char in "\"'" and stack:
            quote = char
        i += 1
    return matches


def _parse_structure(fragment: str) -> object | None:
    try:
        return json.loads(fragment)
    except (ValueError, RecursionError):
        pass
    if len(fragment) > _LITERAL_EVAL_LIMIT:
        return None
    try:
        return ast.literal_eval(fragment)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None


def _redact_embedded_structures(text: str) -> str:
    """Parse JSON or a dict/list repr embedded in `text` and redact it by key."""
    matches = _matching_closers(text)
    if not matches:
        return text
    out: list[str] = []
    cursor = 0
    attempts = 0
    for start in sorted(matches):
        if start < cursor or attempts >= _MAX_STRUCTURE_ATTEMPTS:
            continue
        end = matches[start] + 1
        attempts += 1
        parsed = _parse_structure(text[start:end])
        if not isinstance(parsed, (dict, list, tuple)):
            continue
        redacted = _redact_value(cast("object", parsed))
        try:
            rendered = (
                json.dumps(redacted)
                if text[start + 1 : start + 2] != "'" and "'" not in text[start:end]
                else repr(redacted)
            )
        except (TypeError, ValueError):
            rendered = repr(redacted)
        out.append(text[cursor:start])
        out.append(rendered)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _redact_quoted_pairs(text: str) -> str:
    """Redact the quoted value after a quoted secret key, scanning linearly."""
    out: list[str] = []
    cursor = 0
    oauth = _OAUTH_CONTEXT.search(text) is not None
    for match in _QUOTED_KEY.finditer(text):
        if match.start() < cursor or not _is_secret_name(match.group(2), oauth=oauth):
            continue
        quote = match.group(4)
        value_start = match.end()
        limit = min(len(text), value_start + 4096)
        i = value_start
        while i < limit and text[i] != quote:
            i += 2 if text[i] == "\\" else 1
        if i >= limit:
            continue
        out.append(text[cursor:value_start])
        out.append(_REDACTED)
        cursor = i
    out.append(text[cursor:])
    return "".join(out)


def _redact_name_values(text: str, *, oauth: bool) -> str:
    out: list[str] = []
    cursor = 0
    for match in _NAME_VALUE.finditer(text):
        if match.start() < cursor or not _is_secret_name(match.group(1), oauth=oauth):
            continue
        value_start = match.end() + len(match.group(3))
        out.append(text[cursor:value_start])
        out.append(_REDACTED)
        cursor = value_start + len(match.group(4))
    out.append(text[cursor:])
    return "".join(out)


def _redact_url_queries(text: str) -> str:
    """Redact every value in each `scheme://…?query` found in free text.

    Anchored on `?` and searched backwards a bounded distance for `://`, so
    the cost stays linear however the text repeats URL fragments.
    """
    out: list[str] = []
    cursor = 0
    position = text.find("?")
    while position != -1:
        scheme = text.rfind("://", max(cursor, position - 512), position)
        if scheme != -1 and not any(c.isspace() for c in text[scheme:position]):
            end_match = _QUERY_END.search(text, position + 1, position + 8192)
            end = end_match.start() if end_match else min(len(text), position + 8192)
            query = text[position + 1 : end]
            out.append(text[cursor : position + 1])
            out.append(
                "&".join(
                    f"{part.split('=', 1)[0]}={_REDACTED}" if "=" in part else part
                    for part in query.split("&")
                )
            )
            cursor = end
            position = text.find("?", end)
        else:
            position = text.find("?", position + 1)
    out.append(text[cursor:])
    return "".join(out)


def _redact_secret_text(text: str) -> str:
    """Redact secrets in free text, in time linear in its length.

    Text over 64 KB is truncated first. Embedded JSON/reprs are redacted by
    key, then quoted pairs, `name=value` pairs and credential shapes.
    """
    if len(text) > _TEXT_LIMIT:
        text = text[:_TEXT_LIMIT] + " [truncated]"
    if "{" in text or "[" in text:
        text = _redact_embedded_structures(text)
    if "'" in text or '"' in text:
        text = _redact_quoted_pairs(text)
    if "://" in text and "?" in text:
        text = _redact_url_queries(text)
    for pattern, keep in _FREE_TEXT_PATTERNS:
        text = pattern.sub(keep + _REDACTED, text)
    return _redact_name_values(text, oauth=_OAUTH_CONTEXT.search(text) is not None)


def _redact_secret_keys(mapping: dict[str, object], *, oauth: bool = False) -> None:
    """Redact secret-named values in place, at any depth, including list items."""
    for key in list(mapping.keys()):
        if _is_secret_name(key, oauth=oauth):
            mapping[key] = _REDACTED
        else:
            mapping[key] = _redact_value(mapping[key], oauth=oauth)


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
    """before_send / before_send_transaction: scrub, or send a stripped event.

    A scrubber bug must neither drop the error report nor let the original
    through, so any failure falls back to type names and a fixed message.
    """
    try:
        return _scrub_event_fields(event, hint)
    except Exception:
        return _stripped_event(event)


def _stripped_event(event: Event) -> Event:
    stripped: dict[str, object] = {
        key: event[key]  # pyright: ignore[reportTypedDictNotRequiredAccess]
        for key in ("event_id", "timestamp", "level", "platform", "environment", "release", "type")
        if key in event
    }
    exceptions = event.get("exception")
    if isinstance(exceptions, dict):
        values = [
            {
                "type": str(cast("dict[str, object]", v).get("type", "Exception")),
                "value": "[redaction failed]",
            }
            for v in cast("list[object]", exceptions.get("values") or [])
            if isinstance(v, dict)
        ]
        stripped["exception"] = {"values": values}
    else:
        stripped["message"] = "[redaction failed]"
    return cast("Event", stripped)


def _scrub_event_fields(event: Event, hint: Hint) -> Event | None:
    """Redact secrets and drop message-body fields.

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

    # Tags: secret-named keys, and secret text in any value.
    tags = event.get("tags")
    if isinstance(tags, dict):
        for key in list(tags.keys()):
            value = tags[key]
            if _is_secret_name(key):
                tags[key] = _REDACTED
            else:
                tags[key] = _redact_secret_text(value)

    # User data (ids, emails, IPs) never leaves the process.
    event.pop("user", None)

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

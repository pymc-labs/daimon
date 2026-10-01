"""Sentry observability helpers for daimon-core.

Functional-core / imperative-shell split:
  - Pure:  _scrub_event (before_send callback)
  - Shell: init_sentry (the single I/O escape — calls sentry_sdk.init)

Do NOT call sentry_sdk.init at module import time (architecture rule 3).
Adapters call init_sentry once at their entrypoint (Plan 02).

What the scrubber does to every error and transaction event: no frame
locals, breadcrumbs, user, cookies, request bodies or environ; request URLs
reduced to scheme/host/path, capability tokens in paths replaced, and every
query value redacted; credential headers redacted and other header values
text-scrubbed; and secrets in free text (exception values, messages, tags,
contexts, spans) redacted by name or by shape, in time linear in the text.

Known limitations, so error text at credential boundaries should still
prefer fixed messages over provider bodies:

- Path tokens are found by route (`_CAPABILITY_PATHS`) and by shape (long
  mixed-case segments, dot-joined signed tokens); a new token route that is
  neither listed nor credential-shaped (e.g. all-lowercase hex) is not.
- Free-text redaction is heuristic. It finds values after a secret-looking
  name (`*TOKEN*`, `*SECRET*`, `*PASSW*`, `*KEY`/`*KEYS`, `auth*` …) and a few
  known shapes (Slack, GitHub, `sk-`, JWT, Fernet, Discord webhook paths). A
  secret under an innocuous name ("value", "id") or in an unknown format is
  not recognised.
- OAuth `code`/`state` are redacted in query strings everywhere, but in free
  text only when the text looks like an OAuth exchange.
- It errs towards over-redaction: an unquoted value runs to the next
  whitespace, so trailing punctuation goes too, and a name like `--token-ttl`
  hides its value.
- Text over 64 KB is truncated; values over 4 KB are cut at 4 KB; at most 32
  embedded JSON/repr structures per string are parsed (the rest are still
  covered by the name/shape rules).
- Form-encoded text is URL-decoded one level, so a reported message can show
  decoded characters.
- If the scrubber itself fails, the event is replaced by exception types and
  "[redaction failed]".
"""

from __future__ import annotations

import ast
import bisect
import contextvars
import json
import logging
import re
import traceback
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sentry_sdk
import structlog
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint
    from sentry_sdk.integrations import Integration
    from structlog.typing import EventDict, WrappedLogger

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
    r"private[_-]?key|verifier|bearer|(?:^|[_.-])(?:keys?|sig|dsn)$"
)
# Names that contain a secret-looking word but carry counts, ids or labels.
_SECRET_NAME_ALLOW = re.compile(
    r"^(?:author|authors|authored|authority|session_id|token_count|key_count|key_name|key_names)$"
    r"|^(?:input|output|prompt|completion|total|num|max|min|reasoning|cache(?:_[a-z]+)*)_tokens?$"
)
# OAuth's `code` and `state` are bearer material, but common words in prose, so
# they are secret only in a query string (handled there) or an OAuth context.
_OAUTH_NAMES: frozenset[str] = frozenset({"code", "state"})

_REDACTED = "[redacted]"
_TEXT_LIMIT = 64 * 1024
_LITERAL_EVAL_LIMIT = 8 * 1024
_MAX_STRUCTURE_ATTEMPTS = 32

_TOKEN_START = r"(?<![A-Za-z0-9_-])"

_FREE_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # pydantic echoes the rejected input: never keep it.
    (
        re.compile(r"""(input_value=)(?:'(?:[^'\\]|\\.){0,4096}'|"(?:[^"\\]|\\.){0,4096}"|\S+)"""),
        r"\1",
    ),
    # Authorization header in text: keep the scheme, drop the credential.
    (
        re.compile(r"(?i)(\bauthorization\s*[:=]\s*['\"]?[A-Za-z][A-Za-z0-9-]{0,20}\s+)\S+"),
        r"\1",
    ),
    (re.compile(r"(?i)(\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1"),
    # userinfo in a URL: scheme://user:SECRET@host
    (re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]{0,15}://[^\s:/@]{0,64}:)[^\s@/]{1,256}(?=@)"), r"\1"),
    # Provider token shapes: Slack, GitHub, Anthropic/OpenAI-style keys.
    # Each shape starts only where a run of token characters starts
    # (`_TOKEN_START`), so a long run is scanned once, not once per offset.
    (re.compile(_TOKEN_START + r"()xox[abposr]-[A-Za-z0-9-]{8,}"), r"\1"),
    (
        re.compile(_TOKEN_START + r"()(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})"),
        r"\1",
    ),
    (re.compile(_TOKEN_START + r"()sk-(?:ant-)?[A-Za-z0-9_-]{16,}"), r"\1"),
    (re.compile(_TOKEN_START + r"()xapp-\d-[A-Za-z0-9-]{8,}"), r"\1"),
    # JWTs (three base64url segments, the first a JSON header).
    (
        re.compile(_TOKEN_START + r"()eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
        r"\1",
    ),
    # Discord webhook URLs carry their token in the path.
    (re.compile(r"(/api/webhooks/\d{5,25}/)[A-Za-z0-9_.-]+"), r"\1"),
    # Fernet tokens and keys.
    (re.compile(_TOKEN_START + r"()gAAAAA[A-Za-z0-9_-]{20,}={0,2}"), r"\1"),
    (re.compile(r"()\b[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_-])"), r"\1"),
)
# Any `name = value` / `name: value` / `name%3Dvalue`. The value sits in a
# lookahead so a non-secret pair never swallows the pair after it
# (`failed: SLACK_BOT_TOKEN=x`); the scan decides by name.
_NAME_VALUE = re.compile(
    r"(?<![A-Za-z0-9_.-])-{0,2}([A-Za-z_][A-Za-z0-9_.-]{0,63})(\s{0,4}(?:=|%3[Dd]|:)\s{0,4})"
)
# A command-line flag followed by its value as the next word: `--password x`.
_FLAG_VALUE = re.compile(r"(?<!\S)-{1,2}([A-Za-z][A-Za-z0-9_-]{0,63})(\s+)(?=[^\s-])")
_FLAG_NAME = re.compile(r"^-{1,2}([A-Za-z][A-Za-z0-9_.-]{0,63})$")
# Characters that end a URL query inside free text.
_QUERY_END = re.compile(r"[\s#'\"<>]")
# Form-encoded separators: `%26` (&) and `%3D` (=), also double-encoded
# (`%2526`, `%253D`).
_FORM_ENCODED = re.compile(r"%(?:25)?(?:26|3[Dd])")
# Words that mark text as an OAuth exchange, where `code`/`state` are secrets.
_OAUTH_CONTEXT = re.compile(r"(?i)oauth|callback|authori[sz]e|redirect_uri|token exchange")
# A quoted key and the opening quote of its quoted value (JSON, dict repr).
# Optionally backslash-escaped (JSON inside a JSON string).
_QUOTED_KEY = re.compile(r"""(\\?["'])([^"'\\\n]{1,64})\1(\s{0,4}:\s{0,4})""")
_VALUE_LIMIT = 4096
_WHITESPACE = re.compile(r"\s")
# A quote preceded by an even number of backslashes (unescaped), and one
# preceded by an odd number (escaped, as in JSON inside a JSON string).
_UNESCAPED_QUOTE = re.compile(r"""(?<!\\)((?:\\\\)*)(["'])""")
_ESCAPED_QUOTE = re.compile(r"""(?<!\\)(?:\\\\)*\\(["'])""")


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
        items: list[object] = []
        redact_next = False
        for item in cast("list[object]", value):
            if redact_next:
                items.append(_REDACTED)
                redact_next = False
                continue
            # argv lists (CalledProcessError): `--password`, then its value.
            flag = _FLAG_NAME.match(item) if isinstance(item, str) else None
            redact_next = flag is not None and _is_secret_name(flag.group(1), oauth=oauth)
            items.append(_redact_value(item, oauth=oauth, depth=depth + 1))
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


class _ValueIndex:
    """Per-string lookups that make each value end O(log n).

    Built once per text, from regex scans and `_matching_closers`, so the
    cost of redacting stays linear however many secret names repeat.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._whitespace = [m.start() for m in _WHITESPACE.finditer(text)]
        self._quotes: dict[str, list[int]] = {'"': [], "'": []}
        for match in _UNESCAPED_QUOTE.finditer(text):
            self._quotes[match.group(2)].append(match.start(2))
        self._escaped_quotes: dict[str, list[int]] = {'"': [], "'": []}
        for match in _ESCAPED_QUOTE.finditer(text):
            self._escaped_quotes[match.group(1)].append(match.start(1) - 1)
        self._closers: dict[int, int] | None = None

    @staticmethod
    def _next(positions: list[int], after: int) -> int | None:
        index = bisect.bisect_right(positions, after)
        return positions[index] if index < len(positions) else None

    def value_end(self, start: int) -> int:
        """End of the value that starts at `start`, erring towards redacting more.

        A quoted value runs to its matching unescaped quote (an escaped `\\"`
        opener to the matching `\\"`); a bracketed value to its matching closer;
        anything else to the next whitespace. Bounded by `_VALUE_LIMIT`.
        """
        text = self._text
        limit = min(len(text), start + _VALUE_LIMIT)
        if start >= limit:
            return start
        first = text[start]
        if first == "\\" and start + 1 < limit and text[start + 1] in "\"'":
            end = self._next(self._escaped_quotes[text[start + 1]], start + 1)
            return limit if end is None or end + 2 > limit else end + 2
        if first in "\"'":
            end = self._next(self._quotes[first], start)
            return limit if end is None or end + 1 > limit else end + 1
        if first in "{[":
            if self._closers is None:
                self._closers = _matching_closers(text)
            end = self._closers.get(start)
            return limit if end is None or end + 1 > limit else end + 1
        end = self._next(self._whitespace, start - 1)
        return limit if end is None or end > limit else end


def _redact_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace each span with the marker; overlapping spans merge by max end.

    A lone quoted span keeps its quotes, so the redacted text stays readable.
    """
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = 0
        else:
            merged.append([start, end, 1])
    out: list[str] = []
    cursor = 0
    for start, end, single in merged:
        head, tail = start, end
        if (
            single
            and text.startswith("\\", start)
            and end - start >= 4
            and text[start + 1] in "\"'"
        ):
            head, tail = start + 2, end - 2
        elif single and text[start] in "\"'" and end - start >= 2 and text[end - 1] == text[start]:
            head, tail = start + 1, end - 1
        out.append(text[cursor:head])
        out.append(_REDACTED)
        cursor = tail
    out.append(text[cursor:])
    return "".join(out)


def _secret_value_spans(text: str, *, oauth: bool) -> list[tuple[int, int]]:
    """Value spans after secret names: quoted keys, `name=value`, `--flag value`."""
    starts: list[int] = []
    if "'" in text or '"' in text:
        starts.extend(
            m.end() for m in _QUOTED_KEY.finditer(text) if _is_secret_name(m.group(2), oauth=oauth)
        )
    starts.extend(
        m.end() for m in _NAME_VALUE.finditer(text) if _is_secret_name(m.group(1), oauth=oauth)
    )
    if "-" in text:
        starts.extend(
            m.end() for m in _FLAG_VALUE.finditer(text) if _is_secret_name(m.group(1), oauth=oauth)
        )
    if not starts:
        return []
    index = _ValueIndex(text)
    return [(start, index.value_end(start)) for start in starts]


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


# Total free text one event may have scrubbed; beyond it values are replaced
# outright, so a huge event costs bounded time on the capturing thread.
_EVENT_TEXT_BUDGET = 256 * 1024
_EVENT_BUDGET_MARKER = "[redacted: event budget]"
_event_text_budget: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "daimon_sentry_event_text_budget", default=None
)


def _redact_secret_text(text: str) -> str:
    """Redact secrets in free text, in time linear in its length.

    Text over 64 KB is truncated first. Embedded JSON/reprs are redacted by
    key, then quoted pairs, `name=value` pairs and credential shapes.
    """
    if len(text) > _TEXT_LIMIT:
        text = text[:_TEXT_LIMIT] + " [truncated]"
    remaining = _event_text_budget.get()
    if remaining is not None:
        if len(text) > remaining:
            _event_text_budget.set(0)
            return _EVENT_BUDGET_MARKER
        _event_text_budget.set(remaining - len(text))
    if "{" in text or "[" in text:
        text = _redact_embedded_structures(text)
    if "://" in text and "?" in text:
        text = _redact_url_queries(text)
    if _FORM_ENCODED.search(text):
        # Form-encoded bodies: decode only the pair separators, so
        # `%26name%3Dvalue` reads as `&name=value` while `%20`, `%23` and
        # friends stay encoded and can't end a value early.
        text = _FORM_ENCODED.sub(lambda m: "&" if m.group(0)[-2:] == "26" else "=", text)
    for pattern, keep in _FREE_TEXT_PATTERNS:
        text = pattern.sub(keep + _REDACTED, text)
    text = _redact_path_tokens(text)
    oauth = _OAUTH_CONTEXT.search(text) is not None
    return _redact_spans(text, _secret_value_spans(text, oauth=oauth))


def _redact_secret_keys(mapping: dict[str, object], *, oauth: bool = False) -> None:
    """Redact secret-named values in place, at any depth, including list items."""
    for key in list(mapping.keys()):
        if _is_secret_name(key, oauth=oauth):
            mapping[key] = _REDACTED
        else:
            mapping[key] = _redact_value(mapping[key], oauth=oauth)


# Routes whose path carries a bearer capability, as (prefix, segment) pairs:
# the segment after the prefix is the token. Keep in step with the servers
# that report to Sentry (MCP uploads and Slack file proxy, Discord webhooks)
# and the hosts' token routes.
_CAPABILITY_PATHS = re.compile(
    r"(/(?:uploads|upload|publish|slack/file|recipients)/|/api/webhooks/\d{1,25}/)"
    r"([^/?#\s'\"<>]{1,4096})"
)
# Defence in depth for routes not listed: a long path segment that looks like
# a random credential (mixed letter cases and digits, or dot-joined base64url
# runs as in a signed token).
_PATH_SEGMENT = re.compile(r"(?<=/)[A-Za-z0-9_.~=-]{20,4096}(?=[/?#\s'\"<>]|$)")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SIGNED_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_=-]{8,})+$")


def _looks_like_credential(segment: str) -> bool:
    if _UUID.match(segment):
        return False
    if _SIGNED_TOKEN.match(segment):
        return True
    return (
        any(c.islower() for c in segment)
        and any(c.isupper() for c in segment)
        and any(c.isdigit() for c in segment)
    )


def _redact_path_tokens(text: str) -> str:
    """Replace capability path segments with a marker (route-aware, then by shape)."""
    if "/" not in text:
        return text
    text = _CAPABILITY_PATHS.sub(lambda m: m.group(1) + _REDACTED, text)
    return _PATH_SEGMENT.sub(
        lambda m: _REDACTED if _looks_like_credential(m.group(0)) else m.group(0), text
    )


def _redact_url(url: str) -> str:
    """Keep scheme, host and path; drop userinfo, query and fragment; redact
    capability tokens in the path."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _REDACTED
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, _redact_path_tokens(parts.path), "", ""))


def _scrub_url_fields(data: dict[str, object]) -> None:
    """SDK span/trace URL fields by meaning: fragments dropped, every query
    value redacted (whatever the parameter is called), URLs reduced."""
    for key in list(data.keys()):
        lowered = key.lower()
        value = data[key]
        if lowered.endswith("fragment"):
            del data[key]
        elif lowered.endswith("query") and isinstance(value, str):
            data[key] = _redact_query(value.lstrip("?"))
        elif lowered in ("url", "http.url", "url.full", "http.target", "url.path") and isinstance(
            value, str
        ):
            data[key] = (
                redact_request_target(value) if value.startswith("/") else _redact_url(value)
            )


def redact_request_target(target: str) -> str:
    """A request target (`/path?query#frag`) safe to log: capability path
    segments replaced, every query value redacted, fragment dropped."""
    without_fragment = target.split("#", 1)[0]
    path, separator, query = without_fragment.partition("?")
    path = _redact_path_tokens(path)
    return f"{path}?{_redact_query(query)}" if separator else path


def redact_log_text(text: str) -> str:
    """Free text safe to log: the same rules as Sentry exception text."""
    return _redact_secret_text(text)


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
            elif isinstance(typed[name], str):
                typed[name] = _redact_secret_text(cast("str", typed[name]))


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
    reset = _event_text_budget.set(_EVENT_TEXT_BUDGET)
    try:
        return _scrub_event_fields(event, hint)
    except Exception:
        return _stripped_event(event)
    finally:
        _event_text_budget.reset(reset)


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
        trace = cast("dict[str, object]", contexts).get("trace")
        trace_data = (
            cast("dict[str, object]", trace).get("data") if isinstance(trace, dict) else None
        )
        if isinstance(trace_data, dict):
            _scrub_url_fields(cast("dict[str, object]", trace_data))
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
                for field in ("data", "tags"):
                    mapping = typed_span.get(field)
                    if isinstance(mapping, dict):
                        typed_mapping = cast("dict[str, object]", mapping)
                        _scrub_url_fields(typed_mapping)
                        _redact_secret_keys(typed_mapping)
    transaction = event.get("transaction")
    if isinstance(transaction, str):
        event["transaction"] = _redact_secret_text(transaction)

    # Tags: secret-named keys, and secret text in any value.
    tags = event.get("tags")
    if isinstance(tags, dict):
        for key in list(tags.keys()):
            value = cast("object", tags[key])
            if _is_secret_name(key):
                tags[key] = _REDACTED
            elif isinstance(value, str):
                tags[key] = _redact_secret_text(value)

    # User data (ids, emails, IPs) never leaves the process.
    event.pop("user", None)

    return event


_ACCESS_LOGGER = "uvicorn.access"
# Loggers whose records can carry request targets or third-party URLs.
_REDACTED_LOGGERS = ("", "uvicorn", "uvicorn.error", _ACCESS_LOGGER)
# Outbound-request loggers that print full URLs at INFO.
_QUIETED_LOGGERS = ("httpx", "httpcore")


class LogRedactionFilter(logging.Filter):
    """Redact stdlib log records (uvicorn access/error, anything on root).

    Access records keep their positional args (uvicorn's formatter reads
    them) with the request target redacted; other records are redacted as
    formatted text, and their traceback text is redacted too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == _ACCESS_LOGGER
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            args = list(record.args)
            if isinstance(args[2], str):
                args[2] = redact_request_target(args[2])
            record.args = tuple(args)
            return True
        if record.exc_info and not record.exc_text:
            record.exc_text = _redact_secret_text(
                "".join(traceback.format_exception(*record.exc_info))
            ).rstrip("\n")
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = _redact_secret_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def install_log_redaction() -> None:
    """Redact uvicorn and root stdlib logging in this process; idempotent.

    Call after the server has configured logging (an app factory runs after
    uvicorn's own `dictConfig`). Filters go on the loggers and on every
    handler they already have, so propagated records are covered too.
    """
    redaction = LogRedactionFilter()
    for name in _REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        targets: list[logging.Filterer] = [logger, *logger.handlers]
        for target in targets:
            if not any(isinstance(f, LogRedactionFilter) for f in target.filters):
                target.addFilter(redaction)
    for name in _QUIETED_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(max(logger.getEffectiveLevel(), logging.WARNING))


def redact_log_event(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: redact string fields and rendered exceptions.

    Place it after the exception renderer/formatter so traceback text is
    already a field.
    """
    del logger, method_name
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = _redact_secret_text(value)
        elif key == "exception":
            event_dict[key] = _redact_value(value)
    return event_dict


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

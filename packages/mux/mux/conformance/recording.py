"""Metadata and normalized-event tapes. No native HTTP bodies or live fallback."""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Literal, cast
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import Field, JsonValue, ValidationError, model_validator

from mux.conformance.runner import Adapter, Result, run_fixture
from mux.contracts._base import Contract, FrozenMap
from mux.contracts.events import PAYLOAD_MODELS, Event, RequiredAction, ToolUsePayload

_CREDENTIAL = re.compile(
    r"^(authorization|proxy_?authorization|cookies?|set_?cookie|api_?key|x_?api_?key|"
    r"x_?goog_?api_?key|key|token|api_?token|bearer(?:_?token)?|session_?token|"
    r"(?:x_?)?auth_?token|client_?secret|access_?token|refresh_?token|password|"
    r"secret(?:_?key)?|credentials?)$",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bbearer\s+[^\s\"'<>;,]+", re.IGNORECASE)
_BASIC = re.compile(r"\bbasic\s+([A-Za-z0-9+/_-]{8,}={0,2})", re.IGNORECASE)
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")
# Any prefix plus a key-length tail is sensitive, regardless of its neighbour.
# sk-proj-/sk-ant- and other sk- variants also satisfy the generic sk- pattern.
_KEY = re.compile(r"(?:sk-|AIza)[A-Za-z0-9_-]{32,}")
_QUERY = re.compile(
    r"([?&](?:key|api[-_]?key|access_token|refresh_token|token|api_token|session_token)=)"
    r"([^&\s#]*)",
    re.IGNORECASE,
)
_ESCAPED = re.compile(r"""\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[nrtbf\\"'/])""")
_SHORT_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "/": "/",
}


class RecordingError(Exception):
    """A constant diagnostic; never echo provider data or validation input."""


# Header values are a deliberately closed vocabulary. Credential-bearing
# headers retain their names with a constant marker; all other names are dropped.
_HEADERS = frozenset(("accept", "content-type", "authorization", "x-api-key", "x-goog-api-key"))
_MEDIA = frozenset(("application/json", "text/event-stream", "application/json; charset=utf-8"))
_MAX_TAPE_BYTES = 4 * 1024 * 1024


class RequestMetadata(Contract):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: str = Field(pattern=r"^/[^?#\r\n]*$", max_length=2048)
    headers: FrozenMap[str, str] = Field(default_factory=dict[str, str])
    body_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _headers(self) -> RequestMetadata:
        for name, value in self.headers.items():
            if name not in _HEADERS or (
                value != "[redacted]"
                and not (name in ("accept", "content-type") and value in _MEDIA)
            ):
                raise ValueError("headers must use the redacted allowlist")
        return self

    @classmethod
    def from_request(
        cls,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> RequestMetadata:
        """Project keys only. URL query/userinfo and body values are never retained."""
        try:
            projected = {
                name.lower(): value
                if name.lower() in ("accept", "content-type") and value in _MEDIA
                else "[redacted]"
                for name, value in (headers or {}).items()
                if name.lower() in _HEADERS
            }
            return cls.model_validate(
                {
                    "method": method.upper(),
                    "path": urlsplit(url).path or "/",
                    "headers": projected,
                    "body_fields": tuple(body or ()),
                }
            )
        except (ValueError, TypeError):
            raise RecordingError("invalid request metadata") from None


class EventBatch(Contract):
    request: RequestMetadata
    events: tuple[Event, ...]


class Tape(Contract):
    version: Literal[2] = 2
    fixture_id: str = Field(pattern=r"^C(0[1-9]|1[0-8])$")
    provider: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$")
    model: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}$")
    complete: bool = False
    batches: tuple[EventBatch, ...]


def decode_base64(blob: str) -> bytes | None:
    try:
        return base64.b64decode(blob + "=" * (-len(blob) % 4), altchars=b"-_", validate=True)
    except ValueError:
        return None


def string_fields(
    value: JsonValue, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from string_fields(item, (*path, key))
    elif isinstance(value, list):
        for item in value:
            yield from string_fields(item, (*path, "[]"))
    elif isinstance(value, str):
        yield path, value


def looks_text(text: str) -> bool:
    sample = text[:4096] + text[-4096:]
    return all(char.isprintable() or char in "\r\n\t" for char in sample)


def decode_escape(match: re.Match[str]) -> str:
    escape = match[0][1:]
    return chr(int(escape[1:], 16)) if escape[0] in ("u", "x") else _SHORT_ESCAPES[escape]


class Audit:
    """One bounded audit context; decoded text/results are never cached globally."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        self.secrets = secrets
        self.decoded: dict[str, bytes | None] = {}
        self.strings: dict[tuple[str, int, bool], bool] = {}
        self.bodies: dict[tuple[bytes, int], bool] = {}
        self.plain_results: dict[tuple[str, bool], bool] = {}

    def base64(self, blob: str) -> bytes | None:
        if blob not in self.decoded:
            self.decoded[blob] = decode_base64(blob)
        return self.decoded[blob]

    def plain(self, value: str, queries: bool) -> bool:
        key = value, queries
        if key not in self.plain_results:
            self.plain_results[key] = bool(
                any(secret in value for secret in self.secrets)
                or _BEARER.search(value)
                or (("sk-" in value or "AIza" in value) and _KEY.search(value))
                or any(
                    (decoded := self.base64(match[1])) is not None and b":" in decoded
                    for match in _BASIC.finditer(value)
                )
                or (
                    queries
                    and any(match[2] not in ("", "[redacted]") for match in _QUERY.finditer(value))
                )
            )
        return self.plain_results[key]

    def sensitive(self, value: str, depth: int = 0, *, queries: bool = True) -> bool:
        key = value, depth, queries
        if key not in self.strings:
            self.strings[key] = self.scan(value, depth, queries)
        return self.strings[key]

    def scan(self, value: str, depth: int, queries: bool) -> bool:
        for _ in range(4):
            if self.plain(value, queries):
                return True
            for match in _BASE64.finditer(value):
                decoded = self.base64(match[0])
                if decoded is not None and (depth >= 4 or self.binary(decoded, depth + 1)):
                    return True
            decoded_text = _ESCAPED.sub(decode_escape, unquote(value))
            if decoded_text == value:
                return False
            value = decoded_text
        return True

    def binary(self, value: bytes, depth: int = 0) -> bool:
        key = value, depth
        if key not in self.bodies:
            self.bodies[key] = self.scan_binary(value, depth)
        return self.bodies[key]

    def scan_binary(self, value: bytes, depth: int) -> bool:
        if any(secret.encode() in value for secret in self.secrets):
            return True
        texts = {value.decode("latin1")}
        with suppress(UnicodeError):
            texts.add(value.decode("utf-8"))
        if b"\0" in value:
            # Also inspect complete UTF-16 credentials before a partial code unit.
            texts.update(
                value.decode(encoding, errors="ignore") for encoding in ("utf-16-le", "utf-16-be")
            )
        for text in texts:
            # Always scan raw patterns/literals, including opaque binary data.
            if self.plain(text, True):
                return True
            # Random image/audio bytes do not need recursive base64/JSON scans.
            if not looks_text(text):
                continue
            if self.sensitive(text, depth):
                return True
            try:
                parsed: JsonValue = json.loads(text)
            except ValueError:
                continue
            try:
                self.audit(parsed, depth)
            except RecordingError:
                return True
        return False

    def canonical_text(self, value: str, depth: int = 0) -> str:
        """Decode text leaves before joining; never persist the decoded projection."""
        if depth >= 4:
            raise RecordingError("ambiguous encoded content; tape refused")
        decoded = _ESCAPED.sub(decode_escape, unquote(value))
        if decoded != value:
            return self.canonical_text(decoded, depth + 1)

        def replace(match: re.Match[str]) -> str:
            blob = self.base64(match[0])
            if blob is None:
                if len(match[0]) >= 48:
                    raise RecordingError("ambiguous encoded content; tape refused")
                return match[0]
            encodings = ("utf-16-le", "utf-16-be") if b"\0" in blob else ("utf-8",)
            for encoding in encodings:
                try:
                    text = blob.decode(encoding)
                except UnicodeError:
                    continue
                if looks_text(text) and (b"\0" not in blob or text.isascii()):
                    return self.canonical_text(text, depth + 1)
            if len(match[0]) >= 48:
                raise RecordingError("ambiguous encoded content; tape refused")
            return match[0]

        return _BASE64.sub(replace, value)

    def fragments(self, values: Iterable[JsonValue], depth: int = 0) -> None:
        parts: list[str] = []
        fields: dict[tuple[str, ...], list[str]] = {}
        for value in values:
            for path, text in string_fields(value):
                parts.append(text)
                fields.setdefault(path, []).append(text)
        for fragments in (parts, *fields.values()):
            for projection in (fragments, [self.canonical_text(text) for text in fragments]):
                for separator in ("", "\n"):
                    joined = separator.join(projection)
                    if self.sensitive(joined, depth, queries=False):
                        raise RecordingError("credential survived redaction; tape refused")

    def events(self, events: list[JsonValue], depth: int) -> None:
        self.fragments(events, depth)
        text: list[str] = []
        for event in events:
            self.walk(event, depth)
            for path, value in string_fields(event):
                # Containers also carry type/id/role metadata. Only content
                # leaves belong to the text stream; list indices are not fields.
                fields = tuple(part.lower() for part in path if part != "[]")
                if not fields or fields[-1] in ("delta", "text", "content"):
                    text.append(value)
        self.fragments(text, depth)

    def audit(self, value: JsonValue, depth: int = 0) -> None:
        # Joined-content scanning happens once per root, not at every ancestor.
        self.fragments((value,), depth)
        self.walk(value, depth)

    def walk(self, value: JsonValue, depth: int) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if self.sensitive(key, depth) or (
                    _CREDENTIAL.fullmatch(key.replace("-", "_")) and item != "[redacted]"
                ):
                    raise RecordingError("credential survived redaction; tape refused")
                self.walk(item, depth)
        elif isinstance(value, list):
            for item in value:
                self.walk(item, depth)
        elif isinstance(value, str) and self.sensitive(value, depth):
            raise RecordingError("credential survived redaction; tape refused")


def sensitive(value: str, secrets: tuple[str, ...]) -> bool:
    return Audit(secrets).sensitive(value)


def _input_omitted(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    mapping = cast(Mapping[object, object], value)
    return tuple(mapping) == ("input_omitted",) and mapping["input_omitted"] is True


def _closed_payload(value: object, *, project: bool) -> JsonValue:
    """Only contract-declared keys survive; no free-form mapping traversal.

    New arbitrary mapping slots fail closed until explicitly handled here.
    The two current slots are replaced before the event is serialized.
    """
    if isinstance(value, Contract):
        result: dict[str, JsonValue] = {}
        for field in type(value).model_fields:
            item: object = getattr(value, field)
            omitted_slot = (isinstance(value, ToolUsePayload) and field == "input") or (
                isinstance(value, RequiredAction) and field == "payload"
            )
            # Preserve absent optional fields exactly, including nested DTOs.
            if field not in value.model_fields_set and not omitted_slot:
                continue
            if omitted_slot:
                if not project and not _input_omitted(item):
                    raise RecordingError("free-form event mapping is outside recording scope")
                result[field] = {"input_omitted": True}
            else:
                result[field] = _closed_payload(item, project=project)
        return result
    if isinstance(value, Mapping):
        raise RecordingError("free-form event mapping is outside recording scope")
    if isinstance(value, (tuple, list)):
        sequence = cast(tuple[object, ...] | list[object], value)
        return [_closed_payload(item, project=project) for item in sequence]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise RecordingError("event value is outside the fixed normalized schema")


def _event(event: object, *, project: bool) -> Event:
    if not isinstance(event, Event) or event.type not in PAYLOAD_MODELS:
        raise RecordingError("only fixed normalized event types can be recorded")
    try:
        # typed_payload revalidates nested mappings, which contracts do not
        # recursively freeze. No native record is serialized even transiently.
        payload = _closed_payload(event.typed_payload(), project=project)
        if not project and (event.native.record is not None or event.native.raw_ref is not None):
            raise RecordingError("opaque native provenance is forbidden in a tape")
        value = event.model_dump(
            mode="json", exclude={"payload": True, "native": {"record", "raw_ref"}}
        )
        _normalized_parts(payload)
        value["payload"] = payload
        return Event.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise RecordingError("invalid normalized event") from None


def _normalized_parts(value: JsonValue) -> None:
    if isinstance(value, dict):
        if value.get("type") == "native" or value.get("kind") == "native":
            raise RecordingError("opaque native content is outside recording scope")
        if value.get("data_base64") is not None:
            raise RecordingError("inline binary content is outside recording scope")
        for item in value.values():
            _normalized_parts(item)
    elif isinstance(value, list):
        for item in value:
            _normalized_parts(item)


def audit_tape(tape: Tape, secrets: tuple[str, ...]) -> None:
    for batch in tape.batches:
        for event in batch.events:
            _event(event, project=False)
    if len(tape.model_dump_json().encode()) > _MAX_TAPE_BYTES:
        raise RecordingError("normalized tape exceeds size limit")
    scanner = Audit(secrets)
    scanner.audit(tape.model_dump(mode="json"))
    scanner.events(
        [event.model_dump(mode="json") for batch in tape.batches for event in batch.events], 0
    )


class Recorder:
    """Observe explicitly supplied mux events; never intercept a native callback."""

    def __init__(self, *, secrets: tuple[str, ...] = ()) -> None:
        if any(not secret for secret in secrets):
            raise RecordingError("redaction secrets must be nonempty")
        self.secrets = secrets
        self._batches: list[EventBatch] = []
        self._unsafe = False

    def record(self, request: RequestMetadata, events: Iterable[Event]) -> None:
        try:
            if not isinstance(request, RequestMetadata):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise RecordingError("only request metadata can be recorded")
            batch = EventBatch(
                request=RequestMetadata.model_validate(request.model_dump()),
                events=tuple(_event(event, project=True) for event in events),
            )
            # Defer cross-batch reconstruction until export; never partially save.
            Audit(self.secrets).audit(batch.model_dump(mode="json"))
            self._batches.append(batch)
        except (RecordingError, ValidationError, ValueError, TypeError, RecursionError):
            self._unsafe = True
            raise RecordingError("unsafe normalized evidence; tape refused") from None

    def save(
        self, path: Path, *, fixture_id: str, provider: str, model: str, complete: bool
    ) -> None:
        if self._unsafe:
            raise RecordingError("unsafe normalized evidence; tape refused")
        try:
            tape = Tape(
                fixture_id=fixture_id,
                provider=provider,
                model=model,
                complete=complete,
                batches=tuple(self._batches),
            )
            audit_tape(tape, self.secrets)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise RecordingError("invalid normalized tape") from None
        data = (
            tape.model_dump_json(
                exclude={
                    "batches": {
                        "__all__": {"events": {"__all__": {"native": {"record", "raw_ref"}}}}
                    }
                }
            )
            + "\n"
        )
        if len(data.encode()) > _MAX_TAPE_BYTES:
            raise RecordingError("normalized tape exceeds size limit")
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            # Create with private mode before writing any evidence.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class Replay:
    """An ordered normalized-event fake. Native codec certification is deferred."""

    def __init__(self, tape: Tape, *, secrets: tuple[str, ...] = ()) -> None:
        if not tape.complete:
            raise RecordingError("incomplete run cannot be replayed for conformance")
        if any(not secret for secret in secrets):
            raise RecordingError("redaction secrets must be nonempty")
        # Revalidate before auditing model_copy/model_construct bypasses.
        try:
            tape = Tape.model_validate_json(tape.model_dump_json())
            audit_tape(tape, secrets)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise RecordingError("invalid normalized tape") from None
        self.tape = tape
        self.position = 0

    @classmethod
    def load(cls, path: Path, *, secrets: tuple[str, ...] = ()) -> Replay:
        if path.stat().st_size > _MAX_TAPE_BYTES:
            raise RecordingError("normalized tape exceeds size limit")
        try:
            tape = Tape.model_validate_json(path.read_text())
        except (ValidationError, ValueError):
            raise RecordingError("invalid recording") from None
        return cls(tape, secrets=secrets)

    async def events(self, request: RequestMetadata) -> tuple[Event, ...]:
        if self.position == len(self.tape.batches):
            raise RecordingError("replay exhausted; no live fallback")
        entry = self.tape.batches[self.position]
        if entry.request != request:
            raise RecordingError("request metadata does not match the recording")
        self.position += 1
        return tuple(_event(event, project=False) for event in entry.events)

    def finish(self) -> None:
        if self.position != len(self.tape.batches):
            raise RecordingError("conformance probe left normalized batches unconsumed")


async def replay_fixture(
    path: Path, factory: Callable[[Replay], Adapter], *, secrets: tuple[str, ...] = ()
) -> Result:
    """Fresh event fake per check; never trust a stored verdict or certify codecs."""
    replay = Replay.load(path, secrets=secrets)
    result = await run_fixture(replay.tape.fixture_id, factory(replay))
    if result.status == "pending":
        return result
    try:
        replay.finish()
    except RecordingError:
        return Result(
            replay.tape.fixture_id, "fail", ("normalized replay left evidence unconsumed",)
        )
    return result

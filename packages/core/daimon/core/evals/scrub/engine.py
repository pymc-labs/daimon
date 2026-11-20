"""`Scrubber`: replaces what `Detector` finds with consistent fakes, in text and in JSON.

One scrubber serves a whole suite (or tenant), so a real value gets one fake everywhere. It counts
replacements by kind and never keeps or prints the real text. `learn` reads a document first and
adds what it names (people found by NER, accounts, repos, email users) to the dictionary, so a
name is replaced everywhere it appears, also where it stands alone.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Final

from daimon.core.evals.scrub.detect import SECRET, SECRET_NAME, Detector, Hit, Ner, namey
from daimon.core.evals.scrub.entities import MIN_PART, KnownEntities, Term, normalize, terms_of
from daimon.core.evals.scrub.fakes import FakeMap, adapt, key_check, scrub_key
from pydantic import JsonValue

REDACTED = "[REDACTED]"
PAYLOAD: Final = "payload"
"""Kind counted for an embedded file (base64) that was dropped."""
_SHAPED = frozenset(
    {"person", "person_first", "person_last", "company", "company_first", "project", "channel"}
    | {"repo", "phrase", "place", "host"}
)
# Values safe to keep as they are: a hash or a schema id cannot name anyone.
_HEX: Final = re.compile(r"(?:sha256:)?[0-9a-f]{32,128}")
_SCHEMA_ID: Final = re.compile(r"daimon\.eval\.[a-z_]+/\d+")
_HASH_KEYS: Final = frozenset({"sha256", "hash"})
# Schema fields whose names look secret but hold settings.
_SETTING_KEYS: Final = frozenset({"token_env", "token_shape", "credential_request"})
# Keys whose short values name a person, an account, a company or a project.
_IDENTITY: Final = {
    **dict.fromkeys(
        ("login", "username", "user_name", "screen_name", "nick", "nickname", "handle", "user"),
        "handle",
    ),
    **dict.fromkeys(
        (
            "assignee",
            "assignees",
            "reporter",
            "creator",
            "requester",
            "sender",
            "real_name",
            "full_name",
            "first_name",
            "last_name",
            "author_name",
            "owner_name",
        ),
        "person",
    ),  # fmt: skip
    **dict.fromkeys(("owner", "org", "organization", "company", "workspace"), "company"),
    **dict.fromkeys(("repo", "repository", "project"), "project"),
    **dict.fromkeys(("channel_name", "group", "group_name", "team_name"), "channel"),
}
_LEARNED: Final = frozenset({"person", "company"})
_ROLE_MAIL: Final = frozenset(
    {"admin", "info", "support", "noreply", "no-reply", "hello", "team", "sales", "contact",
     "help", "billing", "security", "dev", "ops", "mail", "office", "jobs", "careers",
     "notifications", "postmaster", "webmaster", "root", "abuse", "privacy", "legal", "press",
     "news"}
)  # fmt: skip
_PUBLIC_MAIL: Final = frozenset(
    {"gmail", "googlemail", "outlook", "hotmail", "live", "yahoo", "icloud", "me", "aol",
     "proton", "protonmail", "pm", "gmx", "mail", "yandex", "fastmail", "zoho", "example"}
)  # fmt: skip
_B64: Final = re.compile(r"[A-Za-z0-9+/\r\n]{16,}={0,2}")
_PAYLOAD_KEYS: Final = ("data", "blob", "bytes", "base64")
_TYPE_KEYS: Final = ("mimeType", "mime_type", "media_type", "mediaType", "contentType")
_MIN_PAYLOAD: Final = 48
_MIN_ID_INT: Final = 10**14


def _snake(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold().replace("-", "_")


def secret_key(key: str) -> bool:
    return bool(key) and key not in _SETTING_KEYS and bool(SECRET_NAME.search(key))


def _plain(key: str, value: str) -> bool:
    return (key in _HASH_KEYS and bool(_HEX.fullmatch(value))) or (
        key == "schema" and bool(_SCHEMA_ID.fullmatch(value))
    )


def _json_in(text: str) -> JsonValue | None:
    """The JSON a string holds (a tool result that is itself JSON), else None."""
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] + stripped[-1] not in ("{}", "[]"):
        return None
    try:
        found: JsonValue = json.loads(stripped)
    except ValueError:
        return None
    return found if isinstance(found, dict | list) else None


def _dump_like(original: str, value: JsonValue) -> str:
    compact = '": ' not in original and '", ' not in original
    return json.dumps(
        value,
        ensure_ascii="\\u" in original,
        indent=2 if "\n" in original.strip() else None,
        separators=(",", ":") if compact else None,
    )


def _b64(text: str) -> bytes | None:
    """The bytes of a base64 string, or None for anything that is not plainly base64."""
    flat = text.replace("\r", "").replace("\n", "")
    if (
        len(flat) % 4
        or not _B64.fullmatch(text)
        or flat.isalpha()
        or flat.isdigit()
        or _HEX.fullmatch(flat)
    ):
        return None
    try:
        return base64.b64decode(flat, validate=True)
    except (binascii.Error, ValueError):
        return None


def _as_text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if all(c.isprintable() or c in "\n\r\t" for c in text) else None


def _payload_key(node: Mapping[str, JsonValue]) -> str | None:
    """The key of an embedded file's base64 (an MCP image or resource), if `node` is one."""
    typed = any(isinstance(node.get(k), str) for k in _TYPE_KEYS) or node.get("type") in (
        "image", "audio", "resource", "file",
    )  # fmt: skip
    if not typed:
        return None
    return next((k for k in _PAYLOAD_KEYS if isinstance(node.get(k), str)), None)


def _fake_key(text: str) -> str:
    return " ".join(re.split(r"[\s_.+\-]+", normalize(text))).strip()


class Scrubber:
    def __init__(
        self,
        entities: KnownEntities,
        key: bytes,
        *,
        salt: str,
        ner_model: str | None = "en_core_web_sm",
        known: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        terms = terms_of(entities)
        self.key = scrub_key(key, salt)
        self.detector = Detector(terms, Ner(ner_model) if ner_model else None)
        self.fakes = FakeMap(self.key, known=known, forbidden=[t.text for t in terms])
        self.counts: Counter[str] = Counter()
        self._fake_cache: tuple[int, set[str]] = (-1, set())

    @property
    def names_checked(self) -> bool:
        """Whether names outside the dictionary are found too (NER loaded)."""
        return self.detector.ner_available

    @property
    def key_check(self) -> str:
        return key_check(self.key)

    # ---------- learning ----------

    def add_terms(self, terms: Iterable[Term]) -> None:
        added = self.detector.add(t for t in terms if len(t.text.strip()) >= MIN_PART)
        self.fakes.forbid(t.text for t in added)

    def learn(self, node: JsonValue, *, key: str = "") -> None:
        """Add what `node` names to the dictionary: replaced everywhere from now on."""
        terms: list[Term] = []
        self._learn(node, key, terms)
        self.add_terms(terms)

    def _learn(self, node: JsonValue, key: str, terms: list[Term]) -> None:
        if isinstance(node, list):
            for item in node:
                self._learn(item, key, terms)
        elif isinstance(node, dict):
            for k, v in node.items():
                self._learn(v, k, terms)
        elif isinstance(node, str) and node != REDACTED:
            if (inner := _json_in(node)) is not None:
                self._learn(inner, "", terms)
                return
            kind = _IDENTITY.get(_snake(key))
            short = len(node) <= 80 and len(node.split()) <= 4 and "://" not in node
            if kind and short and not node.isdigit() and not self._is_fake(node):
                terms.append(Term("repo" if "/" in node else kind, node))  # type: ignore[arg-type]
            for hit in self.detector.find(node, names=namey(node)):
                terms += self._from_hit(hit)

    def _from_hit(self, hit: Hit) -> list[Term]:
        if hit.canon is not None or self._is_fake(hit.text):
            return []
        if hit.kind in _LEARNED:
            return [Term(hit.kind, hit.text)]  # type: ignore[arg-type]
        if hit.kind == "repo":
            owner, _, name = hit.text.partition("/")
            return [Term("repo", hit.text), Term("company", owner), Term("project", name)]
        if hit.kind == "email":
            local, _, domain = hit.text.partition("@")
            labels = domain.split(".")
            found = [Term("handle", local)] if local.casefold() not in _ROLE_MAIL else []
            if len(labels) > 1 and labels[-2].casefold() not in _PUBLIC_MAIL:
                found.append(Term("company", labels[-2]))
            return found
        return []

    # ---------- text ----------

    def _fake_keys(self) -> set[str]:
        values = self.fakes.values()
        if self._fake_cache[0] != len(values):
            self._fake_cache = (len(values), {_fake_key(v) for v in values})
        return self._fake_cache[1]

    def _is_fake(self, text: str) -> bool:
        """Whether `text` is exactly one of our fakes. Never word by word: real names share
        words with fakes."""
        return _fake_key(text) in self._fake_keys()

    def _own(self, text: str, hit: Hit) -> bool:
        if text == REDACTED:
            return True
        return hit.kind != SECRET and hit.canon is None and self._is_fake(hit.text)

    def _replacement(self, matched: str, hit: Hit) -> str:
        if hit.kind == SECRET:
            return REDACTED
        fake = self.fakes.fake(hit.kind, hit.canon or hit.text, of=hit.of)
        out = adapt(hit.text, fake) if hit.kind in _SHAPED else fake
        encoded = matched != hit.text and "%" in matched
        return urllib.parse.quote(out, safe="@.-_/") if encoded else out

    def text(self, text: str, *, names: bool = True) -> str:
        out: list[str] = []
        pos = 0
        for hit in self.detector.find(text, names=names):
            matched = text[hit.start : hit.end]
            if self._own(matched, hit):
                continue
            out += [text[pos : hit.start], self._replacement(matched, hit)]
            self.counts[hit.kind] += 1
            pos = hit.end
        out.append(text[pos:])
        return "".join(out)

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Where `text` holds something to replace, for masking OCR lines in images."""
        return [
            (h.start, h.end)
            for h in self.detector.find(text)
            if not self._own(text[h.start : h.end], h)
        ]

    def residue(self, text: str, *, names: bool = True) -> Counter[str]:
        """What a scrub would still replace in already-scrubbed `text`, by kind."""
        return Counter(
            hit.kind
            for hit in self.detector.find(text, names=names)
            if not self._own(text[hit.start : hit.end], hit)
        )

    # ---------- documents ----------

    def value(self, node: JsonValue, *, key: str = "") -> JsonValue:
        """A copy of `node` with every string scrubbed; secret-named keys lose their values,
        long ids in numbers and base64 inside strings are handled too."""
        self.learn(node, key=key)
        return self._value(node, key)

    def _value(self, node: JsonValue, key: str) -> JsonValue:
        if isinstance(node, bool) or node is None or isinstance(node, float):
            return node
        if isinstance(node, int):
            if abs(node) < _MIN_ID_INT or self._is_fake(str(node)):
                return node
            self.counts["id"] += 1
            return int(self.fakes.fake("id", str(node)))
        if isinstance(node, str):
            return self._string(node, key)
        if isinstance(node, list):
            return [self._value(v, "") for v in node]
        payload = _payload_key(node)
        return {
            self.text(k, names=False): self._payload(v) if k == payload else self._value(v, k)
            for k, v in node.items()
        }

    def _string(self, node: str, key: str) -> str:
        if _plain(key, node) or node == REDACTED:
            return node
        if node and secret_key(key):
            self.counts[SECRET] += 1
            return REDACTED
        if (inner := _json_in(node)) is not None:
            return _dump_like(node, self._value(inner, ""))
        if (data := _b64(node)) is not None:
            if (decoded := _as_text(data)) is not None:
                done = self.text(decoded)
                return node if done == decoded else base64.b64encode(done.encode()).decode()
            if len(data) >= _MIN_PAYLOAD:
                self.counts[PAYLOAD] += 1
                return REDACTED
        return self.text(node, names=namey(node))

    def _payload(self, node: JsonValue) -> JsonValue:
        assert isinstance(node, str)
        data = _b64(node)
        decoded = _as_text(data) if data is not None else None
        if decoded is not None:
            done = self.text(decoded)
            return node if done == decoded else base64.b64encode(done.encode()).decode()
        if node and node != REDACTED:
            self.counts[PAYLOAD] += 1
        return REDACTED if node else node

    def residue_of(self, node: JsonValue, *, key: str = "") -> Counter[str]:
        """`residue` over a document, keyed by `kind` only (never text)."""
        found: Counter[str] = Counter()
        if isinstance(node, bool) or node is None or isinstance(node, float):
            return found
        if isinstance(node, int):
            if abs(node) >= _MIN_ID_INT and not self._is_fake(str(node)):
                found["id"] += 1
        elif isinstance(node, str):
            found += self._string_residue(node, key)
        elif isinstance(node, list):
            for v in node:
                found += self.residue_of(v)
        else:
            payload = _payload_key(node)
            for k, v in node.items():
                found += self.residue(k, names=False)
                if k == payload and isinstance(v, str) and v not in ("", REDACTED):
                    data = _b64(v)
                    decoded = _as_text(data) if data is not None else None
                    found += self.residue(decoded) if decoded is not None else Counter([PAYLOAD])
                else:
                    found += self.residue_of(v, key=k)
        return found

    def _string_residue(self, node: str, key: str) -> Counter[str]:
        if _plain(key, node) or node == REDACTED:
            return Counter()
        if node and secret_key(key):
            return Counter([SECRET])
        if (inner := _json_in(node)) is not None:
            return self.residue_of(inner)
        if (data := _b64(node)) is not None:
            if (decoded := _as_text(data)) is not None:
                return self.residue(decoded)
            if len(data) >= _MIN_PAYLOAD:
                return Counter([PAYLOAD])
        return self.residue(node, names=namey(node))

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
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from typing import Final

from daimon.core.evals.scrub.detect import (
    PUBLIC_NAMES,
    SECRET,
    SECRET_NAME,
    Detector,
    Hit,
    Ner,
    TermIndex,
    common_words,
    english_words,
    fold,
    namey,
)
from daimon.core.evals.scrub.entities import MIN_PART, KnownEntities, Term, normalize, terms_of
from daimon.core.evals.scrub.fakes import FakeMap, adapt, key_check, keyed_hash, scrub_key
from daimon.core.evals.scrub.shape import ID, KEEP, NAME, Shape, field_shape, shape_of
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
_SETTING_KEYS: Final = frozenset({"token_env", "token_shape", "credential_request", "pass_rule"})
# `*_key` names that hold no secret.
_PLAIN_KEYS: Final = frozenset(
    {"sim_key", "sort_key", "primary_key", "partition_key", "foreign_key", "cache_key",
     "dedupe_key", "idempotency_key", "issue_key", "project_key", "group_key", "routing_key"}
)  # fmt: skip
_GENERIC_KEY: Final = re.compile(r"[a-z0-9]_keys?$")
# Secret keys whose numbers are secret too (a token count is not).
_NUMERIC_SECRET: Final = re.compile(
    r"pass(?:word|wd|code)?|(?<![a-z])(?:pwd|pin|otp|cvv|cvc)(?![a-z])"
)
_PHONE_KEY: Final = re.compile(r"(?<![a-z])(?:phone|mobile|tel|telephone|fax|cell|msisdn)(?![a-z])")
_PHONE_VALUE: Final = re.compile(r"\+?[\d\s().-]{7,20}")
# `{"name": "Authorization", "value": ...}` (HAR, header lists): the name says the value is secret.
_NAME_KEYS: Final = ("name", "key", "header", "field")
_VALUE_KEYS: Final = frozenset({"value", "values", "val"})
# Fakes of these kinds are random digits, reserved addresses or random word slugs, so a real value
# equal to one is vanishingly rare: a second scrub leaves them as they are. A name fake is never
# skipped, since real names share the small pool fakes come from.
_STABLE_FAKES: Final = frozenset({"id", "phone", "email", "repo", "host", "ip"})
# Words that may stand next to a fake inside a name the sweep finds ("CruzPartners Team").
_BESIDE_FAKE: Final = frozenset(
    {"the", "of", "and", "team", "group", "inc", "llc", "ltd", "corp", "co", "company", "labs",
     "mr", "mrs", "ms", "dr", "prof", "sir", "jr", "sr"}
)  # fmt: skip
_WORD: Final = re.compile(r"[^\W\d_]{2,}")
_ASCII_SHARE: Final = 0.9
_INLINE_B64: Final = re.compile(r"(?<![\w+/-])[A-Za-z0-9+/_-]{12,}={0,2}(?![\w+/=-])")
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
# What a learned term may look like: words joined by single marks ("O'Neil", "AT&T", "St. Ives"),
# an account, or an owner/name slug. Code, JSON and shell fragments are never a name.
_NAME_TEXT: Final = re.compile(r"[^\W\d_](?:[^\W_]|\. |[ .'’&-](?![ .'’&-]))*(?<![ '’&-])")
_LEARNED_TEXT: Final = {
    "handle": re.compile(r"(?=[\w.-]*[^\W\d_])[\w][\w.-]{1,38}"),
    "repo": re.compile(r"[\w.-]+/[\w.-]+"),
}
_PASSES: Final = 3
"""Most passes after the first; the names model reads only the lines the last one changed."""
_MAX_NAME: Final = (64, 5)
"""Most characters and words in a learned name."""
_NAME_PART: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+|[^\W\d_]+")
_ACRONYM: Final = 4
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
_ID_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._:-]+")
_ID_MAX: Final = 128
_NAME_UNSAFE: Final = re.compile(r"[^a-z0-9]+")


def _snake(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold().replace("-", "_")


def secret_key(key: str) -> bool:
    snake = _snake(key)
    if not key or snake in _SETTING_KEYS or snake in _PLAIN_KEYS:
        return False
    return bool(SECRET_NAME.search(key) or SECRET_NAME.search(snake) or _GENERIC_KEY.search(snake))


def _short_name(value: JsonValue) -> bool:
    return isinstance(value, str) and len(value) <= 64 and " " not in value.strip()


def _named_secret(node: Mapping[str, JsonValue]) -> bool:
    """Whether a sibling name field says this dict's value is a secret."""
    return any(_short_name(v := node.get(k)) and secret_key(str(v)) for k in _NAME_KEYS)


def _secret_pair(node: list[JsonValue]) -> bool:
    """`["Authorization", "Basic ..."]`, a header as a pair."""
    return len(node) == 2 and _short_name(node[0]) and secret_key(str(node[0]))


def _phone_like(key: str, node: str) -> bool:
    return (
        bool(_PHONE_KEY.search(_snake(key)))
        and bool(_PHONE_VALUE.fullmatch(node.strip()))
        and (sum(c.isdigit() for c in node) >= 7)
    )


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


def _inline_b64(word: str) -> tuple[str, bool] | None:
    """The text a base64 word in free text holds and whether it is URL-safe, else None."""
    urlsafe = "-" in word or "_" in word
    body = word.rstrip("=")
    if body.isalpha() or body.isdigit() or _HEX.fullmatch(body) or (urlsafe and "/" in body):
        return None
    try:
        data = (base64.urlsafe_b64decode if urlsafe else base64.b64decode)(
            body + "=" * (-len(body) % 4)
        )
    except (binascii.Error, ValueError):
        return None
    text = _as_text(data)
    # Random bytes can be valid text ("dc-3dc21cc202a4" holds "uϷu͵q…"); real text is ASCII.
    if not text or len(text.strip()) < 4 or sum(map(str.isascii, text)) < _ASCII_SHARE * len(text):
        return None
    return text, urlsafe


def _in_marker(text: str, hit: Hit) -> bool:
    """Whether `hit` lies inside a `[REDACTED]` this scrub wrote ("?k=[REDACTED" ends a URL)."""
    return text.find(REDACTED, max(0, hit.end - len(REDACTED)), hit.start + len(REDACTED)) >= 0


def _id_safe(text: str) -> str:
    """`text` as the schema's ids are written: a fake's spaces and signs become dashes."""
    return _ID_UNSAFE.sub("-", text).lstrip("._:-")[:_ID_MAX] or "x"


def _strings(node: JsonValue) -> Iterator[str]:
    if isinstance(node, str):
        inner = _json_in(node)
        yield from (node,) if inner is None else _strings(inner)
    elif isinstance(node, list | dict):
        for item in node.values() if isinstance(node, dict) else node:
            yield from _strings(item)


class Scrubber:
    def __init__(
        self,
        entities: KnownEntities,
        key: bytes,
        *,
        salt: str,
        ner_model: str | None = "en_core_web_sm",
        known: Mapping[tuple[str, str], str] | None = None,
        learned: Iterable[Term] = (),
        jobs: int = 1,
    ) -> None:
        self.detector = Detector(
            [], Ner(ner_model, jobs=jobs) if ner_model else None, [*PUBLIC_NAMES, *entities.keep]
        )
        terms = [*terms_of(entities), *filter(self._name_like, learned)]
        self.detector.add(terms)
        self.key = scrub_key(key, salt)
        self.fakes = FakeMap(self.key, known=known, forbidden=[t.text for t in terms])
        self.counts: Counter[str] = Counter()
        self._fake_cache: tuple[int, set[str]] = (0, set())
        self._fake_index = TermIndex()
        self._indexed = 0
        self._fake_words: set[str] = set()
        self._ids: dict[str, str] = {}
        self.learned: list[Term] = []
        """Terms found while scrubbing (not in the dictionary or `learned` given), to carry on."""

    @property
    def names_checked(self) -> bool:
        """Whether names outside the dictionary are found too (NER loaded)."""
        return self.detector.ner_available

    @property
    def key_check(self) -> str:
        return key_check(self.key)

    # ---------- learning ----------

    def _name_like(self, term: Term) -> bool:
        """Whether a found term looks like a name: no code, and a word that is no common word,
        kept name, acronym or number ("WebFetch" and "EAP Review" are none)."""
        text = term.text.strip()
        pattern = _LEARNED_TEXT.get(term.kind, _NAME_TEXT)
        if len(text) < MIN_PART or len(text) > _MAX_NAME[0] or not pattern.fullmatch(text):
            return False
        kept, common = self.detector.kept_words, english_words()
        named = (
            p for p in _NAME_PART.findall(fold(text).text)
            if len(p) > 1 and not p.isdigit() and not (p.isupper() and len(p) <= _ACRONYM)
            and p.casefold() not in common and p.casefold() not in kept
        )  # fmt: skip
        return len(text.split()) <= _MAX_NAME[1] and next(named, None) is not None

    def add_terms(self, terms: Iterable[Term]) -> None:
        added = self.detector.add(t for t in terms if self._name_like(t))
        self.fakes.forbid(t.text for t in added)
        self.learned += added

    def prefetch(self, node: JsonValue) -> None:
        """Run NER ahead, in batches, over the strings in `node`."""
        self.detector.prefetch(_strings(node))

    def learn(self, node: JsonValue, *, key: str = "") -> None:
        """Add what `node` names to the dictionary: replaced everywhere from now on."""
        self.prefetch(node)
        terms: list[Term] = []
        self._learn(node, key, terms, shape_of(node))
        self.add_terms(terms)

    def _learn(self, node: JsonValue, key: str, terms: list[Term], shape: Shape = None) -> None:
        if shape in (KEEP, NAME):
            return
        if isinstance(node, list):
            for item in node:
                self._learn(item, key, terms, shape)
        elif isinstance(node, dict):
            for k, v in node.items():
                self._learn(v, k, terms, field_shape(shape, k))
        elif isinstance(node, str) and node != REDACTED:
            if (inner := _json_in(node)) is not None:
                self._learn(inner, "", terms)
                return
            kind = _IDENTITY.get(_snake(key))
            short = len(node) <= 80 and len(node.split()) <= 4 and "://" not in node
            if kind and short and not node.isdigit():
                terms.append(Term("repo" if "/" in node else kind, node))  # type: ignore[arg-type]
            for hit in self.detector.find(node, names=namey(node)):
                terms += self._from_hit(hit)

    def _from_hit(self, hit: Hit) -> list[Term]:
        if hit.canon is not None or self._stable_fake(hit):
            return []
        if hit.kind in _LEARNED or hit.kind == "handle":
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

    # ---------- fakes ----------

    def _fake_keys(self) -> set[str]:
        values, (seen, keys) = self.fakes.values(), self._fake_cache
        keys.update(_fake_key(v) for v in values[seen:])
        self._fake_cache = (len(values), keys)
        return keys

    def _is_fake(self, text: str) -> bool:
        """Whether `text` is exactly one of our fakes."""
        return _fake_key(text) in self._fake_keys()

    def _stable_fake(self, hit: Hit) -> bool:
        return hit.kind in _STABLE_FAKES and hit.canon is None and self._is_fake(hit.text)

    def _keep(self, text: str, hit: Hit) -> bool:
        """Whether a hit in raw text stays: our marker, or a fake no real value has. A real name
        equal to a fake is still replaced."""
        return _in_marker(text, hit) or self._stable_fake(hit)

    def _fake_spans(self, text: str) -> list[tuple[int, int]]:
        """Where `text` holds our fakes, written any way (`CruzPartners`, `cruz-partners`):
        sorted spans, overlapping ones merged."""
        values = self.fakes.values()
        for value in values[self._indexed :]:
            self._fake_index.add(Term("phrase", value))
            self._fake_words.update(w.casefold() for w in _WORD.findall(value))
        self._indexed = len(values)
        view = fold(text)
        spans: list[tuple[int, int]] = []
        for start, end in sorted(view.span(s, e) for s, e, _ in self._fake_index.find(view.text)):
            if spans and start <= spans[-1][1]:
                start, end = spans[-1][0], max(end, spans.pop()[1])
            spans.append((start, end))
        return spans

    def _own(self, text: str, hit: Hit, fakes: list[tuple[int, int]]) -> bool:
        """Whether a hit in scrubbed text is ours: a fake, or a name the NER widened from one by
        words no name is written in (a title, "Team", a fake's word, a lowercase common word)."""
        if _in_marker(text, hit):
            return True
        matched = text[hit.start : hit.end]
        if hit.kind == SECRET:
            return False
        rest = list(matched)
        overlap = False
        for i in range(bisect_right(fakes, hit.start, key=lambda s: s[1]), len(fakes)):
            start, end = fakes[i]
            if start >= hit.end:
                break
            lo, hi = max(start, hit.start), min(end, hit.end)
            overlap = True
            rest[lo - hit.start : hi - hit.start] = " " * (hi - lo)
        if not overlap:
            return False
        left = "".join(rest)
        if hit.kind not in _SHAPED:
            return not re.search(r"[^\W_]", left)
        return all(self._beside(w) for w in _WORD.findall(left))

    def _beside(self, word: str) -> bool:
        folded = word.casefold()
        return (
            folded in _BESIDE_FAKE
            or folded in self._fake_words
            or (word.islower() and folded in common_words())
        )

    # ---------- text ----------

    def _replacement(self, matched: str, hit: Hit) -> str:
        if hit.kind == SECRET:
            return REDACTED
        fake = self.fakes.fake(hit.kind, hit.canon or hit.text, of=hit.of)
        out = adapt(hit.text, fake) if hit.kind in _SHAPED else fake
        encoded = matched != hit.text and "%" in matched
        return urllib.parse.quote(out, safe="@.-_/") if encoded else out

    def _b64_word(self, m: re.Match[str]) -> str:
        found = _inline_b64(m.group())
        if found is None:
            return m.group()
        decoded, urlsafe = found
        done = self.text(decoded)
        if done == decoded:
            return m.group()
        out = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(done.encode()).decode()
        return out if m.group().endswith("=") else out.rstrip("=")

    def text(self, text: str, *, names: bool = True, words: bool = True) -> str:
        text = _INLINE_B64.sub(self._b64_word, text)
        before, done = text, self._replace(text, names=names, words=words, own=None)
        # A fake changes the context NER reads: a name it then finds next to one is replaced
        # too, until nothing changes, so the sweep (reading the same text) finds nothing left.
        for _ in range(_PASSES):
            if not names or done == before:
                break
            before = done
            done = self._replace(done, names=names, words=words, own=self._fake_spans(done))
        return done

    def _replace(
        self, text: str, *, names: bool, words: bool, own: list[tuple[int, int]] | None
    ) -> str:
        """`text` with its hits replaced; `own` holds the fake spans of already-scrubbed text."""
        out: list[str] = []
        pos = 0
        for hit in self.detector.find(text, names=names, words=words):
            matched = text[hit.start : hit.end]
            if self._keep(text, hit) if own is None else self._own(text, hit, own):
                continue
            out += [text[pos : hit.start], self._replacement(matched, hit)]
            self.counts[hit.kind] += 1
            pos = hit.end
        out.append(text[pos:])
        return "".join(out)

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Where `text` holds something to replace, for masking OCR lines in images."""
        return [(h.start, h.end) for h in self.detector.find(text) if not self._keep(text, h)]

    def residue(self, text: str, *, names: bool = True, words: bool = True) -> Counter[str]:
        """What a scrub would still replace in already-scrubbed `text`, by kind."""
        found: Counter[str] = Counter()
        for m in _INLINE_B64.finditer(text):
            if (inline := _inline_b64(m.group())) is not None:
                found += self.residue(inline[0])
        fakes = self._fake_spans(text)
        for hit in self.detector.find(text, names=names, words=words):
            if not self._own(text, hit, fakes):
                found[hit.kind] += 1
        return found

    # ---------- documents ----------

    def value(self, node: JsonValue, *, key: str = "") -> JsonValue:
        """A copy of `node` with every string scrubbed; values under secret-named keys (and in
        their lists and dicts) are dropped, long ids in numbers and base64 in strings handled.
        A bundle document keeps its keys and the fields its schema fixes (`shape`)."""
        self.learn(node, key=key)
        return self._value(node, key, secret=False, shape=shape_of(node))

    def _int(self, node: int, key: str, secret: bool) -> tuple[str, JsonValue] | None:
        """What to do with a number: (kind, replacement), or None to keep it."""
        snake = _snake(key)
        if secret and _NUMERIC_SECRET.search(snake):
            return SECRET, REDACTED
        phone = bool(_PHONE_KEY.search(snake)) and abs(node) >= 10**6
        if (abs(node) < _MIN_ID_INT and not phone) or self._is_fake(str(node)):
            return None
        kind = "phone" if phone else "id"
        return kind, int(self.fakes.fake(kind, str(node)))

    def _value(self, node: JsonValue, key: str, *, secret: bool, shape: Shape = None) -> JsonValue:
        secret = secret or secret_key(key)
        if isinstance(node, bool) or node is None or isinstance(node, float) or shape == KEEP:
            return node
        if isinstance(node, int):
            if (done := self._int(node, key, secret)) is None:
                return node
            self.counts[done[0]] += 1
            return done[1]
        if isinstance(node, str) and shape == ID:
            return self._id(node, key, secret)
        if isinstance(node, str) and shape == NAME:
            return self._name(node)
        if isinstance(node, str):
            return self._string(node, key, secret)
        if isinstance(node, list):
            if _secret_pair(node):
                return [
                    self._value(node[0], key, secret=secret),
                    self._value(node[1], str(node[0]), secret=True),
                ]
            return [self._value(v, key, secret=secret, shape=shape) for v in node]
        payload = _payload_key(node)
        named = _named_secret(node)
        return {
            self._key(k, shape): self._payload(v)
            if k == payload
            else self._value(
                v, k, secret=secret or (named and k in _VALUE_KEYS), shape=field_shape(shape, k)
            )
            for k, v in node.items()
        }

    def _id(self, node: str, key: str, secret: bool) -> str:
        """An id gets one fake in every document it is in, so references to it still hold."""
        if (done := self._ids.get(node)) is None:
            done = self._string(node, key, secret)
            if done != node:
                # Every secret-looking id would otherwise become the same "REDACTED-".
                suffix = f"-{keyed_hash(self.key, 'id', node)[:8]}" if REDACTED in done else ""
                done = _id_safe(done) + suffix
            self._ids[node] = done
        return done

    def _known_names(self, node: str) -> Iterator[Hit]:
        """The dictionary names in a tool or effect name (the model reads no such text)."""
        return (h for h in self.detector.find(node, names=False) if h.canon is not None)

    def _name(self, node: str) -> str:
        """A tool or effect name with its known names replaced, in the same lowercase form."""
        out: list[str] = []
        pos = 0
        for hit in self._known_names(node):
            fake = self._replacement(node[hit.start : hit.end], hit)
            out += [node[pos : hit.start], _NAME_UNSAFE.sub("_", fake.casefold()).strip("_") or "x"]
            self.counts[hit.kind] += 1
            pos = hit.end
        return "".join([*out, node[pos:]])

    def _key(self, key: str, shape: Shape) -> str:
        """A JSON key: a schema field stays; any other is scrubbed for patterns and known names
        (the strict index leaves out lowercase common words: "status", "weight")."""
        if isinstance(shape, dict) and key in shape:
            return key
        return self.text(key, names=False)

    def _string(self, node: str, key: str, secret: bool) -> str:
        if _plain(key, node) or node == REDACTED:
            return node
        if node and secret:
            self.counts[SECRET] += 1
            return REDACTED
        if _phone_like(key, node) and not self._is_fake(node):
            self.counts["phone"] += 1
            return self.fakes.fake("phone", node)
        if (inner := _json_in(node)) is not None:
            return _dump_like(node, self._value(inner, "", secret=False))
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

    def residue_of(self, node: JsonValue, *, key: str = "", secret: bool = False) -> Counter[str]:
        """`residue` over a document, keyed by `kind` only (never text)."""
        return self._residue(node, key, secret, shape_of(node))

    def _residue(self, node: JsonValue, key: str, secret: bool, shape: Shape) -> Counter[str]:
        """What `_value` would still change, walking the same shapes (found at the root only)."""
        secret = secret or secret_key(key)
        found: Counter[str] = Counter()
        if isinstance(node, bool) or node is None or isinstance(node, float) or shape == KEEP:
            return found
        if isinstance(node, int):
            if (done := self._int(node, key, secret)) is not None:
                found[done[0]] += 1
        elif isinstance(node, str) and shape == NAME:
            fakes = self._fake_spans(node)
            found.update(h.kind for h in self._known_names(node) if not self._own(node, h, fakes))
        elif isinstance(node, str):
            found += self._string_residue(node, key, secret)
        elif isinstance(node, list):
            pair = _secret_pair(node)
            for n, v in enumerate(node):
                last = pair and n == 1
                found += self._residue(v, str(node[0]) if last else key, secret or last, shape)
        else:
            payload = _payload_key(node)
            named = _named_secret(node)
            for k, v in node.items():
                found += self._key_residue(k, shape)
                if k == payload and isinstance(v, str) and v not in ("", REDACTED):
                    data = _b64(v)
                    decoded = _as_text(data) if data is not None else None
                    found += self.residue(decoded) if decoded is not None else Counter([PAYLOAD])
                else:
                    found += self._residue(
                        v, k, secret or (named and k in _VALUE_KEYS), field_shape(shape, k)
                    )
        return found

    def _key_residue(self, key: str, shape: Shape) -> Counter[str]:
        if isinstance(shape, dict) and key in shape:
            return Counter()
        return self.residue(key, names=False)

    def _string_residue(self, node: str, key: str, secret: bool) -> Counter[str]:
        if _plain(key, node) or node == REDACTED:
            return Counter()
        if node and secret:
            return Counter([SECRET])
        if _phone_like(key, node) and not self._is_fake(node):
            return Counter(["phone"])
        if (inner := _json_in(node)) is not None:
            return self._residue(inner, "", False, None)
        if (data := _b64(node)) is not None:
            if (decoded := _as_text(data)) is not None:
                return self.residue(decoded)
            if len(data) >= _MIN_PAYLOAD:
                return Counter([PAYLOAD])
        return self.residue(node, names=namey(node))

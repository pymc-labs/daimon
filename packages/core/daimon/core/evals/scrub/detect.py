"""Finding what to replace in a string: secrets, ids, emails, hosts, known names, then names by NER.

Matching runs on a folded view of the text (`fold`): accents and invisible marks dropped,
`\\n`, `\\u00e9`, `%20` and HTML entity escapes decoded. Known names match by word, so
`alice_smith`, `AliceSmith` and `Alice%20Smith` all match "Alice Smith". Hits are mapped back to the
original text. Overlapping hits merge into one covering both, so a longer name found by NER
is never cut short by a shorter known part. Name detection (Presidio with a spaCy model) is
optional: without it `Ner.available` is False and only the dictionary and patterns apply.
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Final

from daimon.core.evals.scrub.entities import Term, fold_char, normalize

SECRET: Final = "secret"
_P_SECRET, _P_EMAIL, _P_KNOWN, _P_ID, _P_HOST, _P_NER = 6, 5, 4, 4, 3, 1


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    kind: str
    priority: int
    of: str | None = None
    canon: str | None = None
    """The dictionary term this hit matched: its fake is keyed by the term, not the spelling."""
    text: str = ""
    """The folded text of the hit."""


# ---------- folding ----------

_ESCAPES: Final = re.compile(
    r"\\u[0-9a-fA-F]{4}|\\[nrt]|(?:%[0-9A-Fa-f]{2})+|&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-z]{2,8});"
)


def _decode(escape: str) -> str:
    if escape.startswith("\\u"):
        point = int(escape[2:], 16)
        return escape if 0xD800 <= point < 0xE000 else fold_char(chr(point))
    if escape.startswith("\\"):
        return " "
    if escape.startswith("&"):
        decoded = html.unescape(escape)
        return " " if decoded.isspace() else "".join(fold_char(c) for c in decoded)
    try:
        decoded = bytes.fromhex(escape.replace("%", "")).decode("utf-8")
    except UnicodeDecodeError:
        return escape
    return "".join(fold_char(c) for c in decoded)


@dataclass(frozen=True)
class View:
    """`text` folded for matching; `starts`/`ends` map each folded char to the source span."""

    text: str
    starts: list[int] | None = None
    ends: list[int] | None = None

    def span(self, start: int, end: int) -> tuple[int, int]:
        if self.starts is None or self.ends is None:
            return start, end
        return self.starts[start], self.ends[end - 1]


def fold(text: str) -> View:
    if text.isascii() and "\\" not in text and "%" not in text and "&" not in text:
        return View(text)
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []

    def emit(out: str, start: int, end: int) -> None:
        chars.extend(out)
        starts.extend([start] * len(out))
        ends.extend([end] * len(out))

    pos = 0
    for m in _ESCAPES.finditer(text):
        for i in range(pos, m.start()):
            emit(fold_char(text[i]), i, i + 1)
        emit(_decode(m.group()), m.start(), m.end())
        pos = m.end()
    for i in range(pos, len(text)):
        emit(fold_char(text[i]), i, i + 1)
    return View("".join(chars), starts, ends)


# ---------- known terms, matched by word ----------

_TOKEN: Final = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+|[^\W\d_]+")
_JOINERS: Final = frozenset(" \t\r\n_-.+")
_MIN_JOINED: Final = 6


def _joins(gap: str) -> bool:
    return len(gap) <= 3 and all(c in _JOINERS for c in gap)


def _same_gap(found: str, wanted: str) -> bool:
    return found.casefold() == wanted.casefold() or (_joins(found) and _joins(wanted))


def _words(view: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group().casefold()) for m in _TOKEN.finditer(view)]


@dataclass(frozen=True)
class _Entry:
    words: tuple[str, ...]
    gaps: tuple[str, ...]
    term: Term


class TermIndex:
    def __init__(self) -> None:
        self._first: dict[str, list[_Entry]] = {}

    def add(self, term: Term) -> None:
        view = fold(term.text).text
        found = _words(view)
        if not found:
            return
        words = tuple(w for _, _, w in found)
        gaps = tuple(view[a[1] : b[0]] for a, b in zip(found, found[1:], strict=False))
        entries = [_Entry(words, gaps, term)]
        joined = "".join(words)
        if len(words) > 1 and all(_joins(g) for g in gaps) and len(joined) >= _MIN_JOINED:
            entries.append(_Entry((joined,), (), term))  # ALICESMITH, alicesmith
        for entry in entries:
            bucket = self._first.setdefault(entry.words[0], [])
            bucket.append(entry)
            bucket.sort(key=lambda e: len(e.words), reverse=True)

    def find(self, view: str) -> Iterator[tuple[int, int, Term]]:
        found = _words(view)
        for i, (start, _, word) in enumerate(found):
            for entry in self._first.get(word, ()):
                n = len(entry.words)
                if i + n <= len(found) and all(
                    found[i + j][2] == entry.words[j]
                    and _same_gap(view[found[i + j - 1][1] : found[i + j][0]], entry.gaps[j - 1])
                    for j in range(1, n)
                ):
                    yield start, found[i + n - 1][1], entry.term
                    break


# ---------- patterns ----------

_SECRETS: Final = [
    re.compile(p)
    for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bglpat-[A-Za-z0-9_-]{20,}",
        r"\bxox[abprsce]-[A-Za-z0-9-]{10,}\b",
        r"\bxoxe\.xox[a-z]-[A-Za-z0-9-]{10,}",
        r"\bxapp-[A-Za-z0-9-]{10,}\b",
        r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b",
        r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{10,}\b",
        r"\bwhsec_[A-Za-z0-9]{16,}\b",
        r"\bhf_[A-Za-z0-9]{30,}\b",
        r"\bnpm_[A-Za-z0-9]{30,}\b",
        r"\bpypi-[A-Za-z0-9_-]{30,}",
        r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
        r"\bya29\.[A-Za-z0-9_-]{20,}",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        r"\b[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b",
        r"hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]+",
        r"\bdata:[\w/+.-]+;base64,[A-Za-z0-9+/=]{16,}",
        r"\b(?:SK|AC)[0-9a-f]{32}\b",
    )
]
# Key names that hold secrets, in `KEY=value` text and as JSON keys. `auth` and `authorization`
# but not `author`.
_PW_LABEL: Final = r"(?:pass(?:word|wd|phrase|code)?|(?<![a-z])(?:pwd|pin(?:_?code)?))"
_KEY_LABEL: Final = (
    r"(?:secret|token|api[_-]?key|private[_-]?key|access[_-]?key|credential|auth(?!or(?!i[sz]))"
    r"|(?:encryption|signing|account|master|client|license|shared[_-]?access)[_-]?key"
    r"|session[_-]?(?:id|key))"
)
SECRET_NAME: Final = re.compile(
    rf"pass(?:word|wd|phrase|code)?(?![a-z])|(?<![a-z])(?:pwd|pin(?:_?code)?)(?![a-z])"
    rf"|{_KEY_LABEL}|cookie",
    re.IGNORECASE,
)
# The secret is group `v` of each: the label before it stays readable. A quoted value is taken
# whole, a password's to the end of the line, any other labelled value as one word. Quantifiers
# are bounded: a long run of word characters must not make matching quadratic.
_LABEL: Final = rf"(?:{_PW_LABEL}|{_KEY_LABEL})[\w.-]{{0,40}}[\"']?[ \t]*[:=][ \t]*"
_SECRET_VALUES: Final = [
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in (
        r"\bBearer\s+(?P<v>[A-Za-z0-9._~+/=-]{16,})",
        r"\bToken\s+(?P<v>[A-Za-z0-9_.-]{20,})",
        r"^\s*(?:proxy-)?authorization\s*:\s*(?:bearer\s+)?(?P<v>.+?)\s*$",
        r"^\s*(?:set-)?cookie\s*:\s*(?P<v>.+?)\s*$",
        rf"{_LABEL}([\"'])(?P<v>(?:(?!\1)[^\n])+)\1",
        rf"{_PW_LABEL}[\w.-]{{0,40}}[\"']?[ \t]*[:=][ \t]*"
        r"(?P<v>[^\s\"'][^\n\"',;&]*?)[ \t]*(?=[\"',;&\n]|$)",
        rf"{_LABEL}(?:(?:bearer|basic|token)\s+)?(?P<v>[^\s\"',;&]{{6,}})",
    )
]
# `Basic` and a base64 `user:password`.
_BASIC: Final = re.compile(r"\bBasic\s+(?P<v>[A-Za-z0-9+/]{8,}={0,2})(?![\w+/=])")
# A login in a home folder path.
_HOME: Final = re.compile(
    r"(?:\b[A-Za-z]:[\\/]+(?:Users|Documents and Settings)|(?<![\w.])/(?:home|Users))[\\/]+"
    r"(?P<v>[^\\/\s\"'<>:*?|]+)"
)
_GENERIC_LOGINS: Final = frozenset(
    {"public", "default", "shared", "all users", "default user", "guest", "user", "runner",
     "ubuntu", "root", "admin", "administrator", "vscode", "node", "app", "me", "you"}
)  # fmt: skip
_URL: Final = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://[^\s<>\"'`)\]}]+", re.IGNORECASE)
_URL_USERINFO: Final = re.compile(r"//(?P<v>[^/@\s:?#]+:[^@\s?#]+)@")
_URL_SECRET_PARAM: Final = re.compile(
    r"[?&;#](?:[\w.-]{0,40}?(?:token|secret|signature|credential|pass(?:word|wd)?|api[_-]?key"
    r"|private[_-]?key|auth(?!or(?!i[sz])))[\w.-]{0,40}|x-amz-[\w-]+|x-goog-[\w-]+|sig|key|apikey|code"
    r"|t|pwd|hm|session|sessionid|sid|jwt|k|pw|pin)=([^&#;\s]+)",
    re.IGNORECASE,
)
_EMAIL: Final = re.compile(
    r"(?<![\w.%+-])(?!git@github\.com:)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_IDS: Final = re.compile(
    r"(?<![0-9A-Za-z])\d{15,20}(?![0-9A-Za-z])"  # Discord snowflakes and other long ids
    r"|\b[UWCDGT](?=[A-Z0-9]*\d)[A-Z0-9]{8,12}\b"  # Slack ids
    r"|\b(?:19|28|29):[0-9A-Za-z_-]{8,}(?:@thread\.[A-Za-z0-9]+|@unq\.gbl\.spaces)?"  # Teams ids
)
_DOC_IDS: Final = re.compile(
    r"(?:docs|drive|sheets|slides)\.google\.com/(?:[\w-]+/)*?(?:d/|folders/|open\?id=)([\w-]{20,})"
)
_PHONE: Final = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[-. ]?)?(?:\(\d{2,4}\)|\d{2,4})[-. ]\d{3,4}[-. ]\d{3,5}(?![\w-])"
)
_HOST: Final = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:internal|local|corp|lan|intranet|home\.arpa)\b", re.IGNORECASE
)
# The tenant part of a hosted service's address names the customer.
_TENANT_HOST: Final = re.compile(
    r"\b([a-z0-9][a-z0-9-]{1,62})\.(?:atlassian\.net|zoom\.us|slack\.com|sharepoint\.com"
    r"|(?:my\.)?salesforce\.com|lightning\.force\.com|my\.site\.com|zendesk\.com|freshdesk\.com"
    r"|okta\.com|service-now\.com|webex\.com|notion\.site|monday\.com|bamboohr\.com"
    r"|myworkday\.com|looker\.com|auth0\.com|onmicrosoft\.com|github\.io|gitlab\.io"
    r"|herokuapp\.com|vercel\.app|netlify\.app|azurewebsites\.net|hubspot\.com|pagerduty\.com"
    r"|statuspage\.io|readthedocs\.io)\b",
    re.IGNORECASE,
)
_SHARED_HOSTS: Final = frozenset(
    {"www", "app", "api", "files", "hooks", "status", "support", "help", "docs", "login",
     "auth", "id", "my", "account", "accounts", "developer", "developers", "community"}
)  # fmt: skip
_IP: Final = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
_REPOS: Final = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?:github|gitlab|bitbucket)\.(?:com|org)[/:]([\w.-]+/[\w-][\w.-]*?)"
        r"(?=\.git\b|[/\s?#\"')\]]|$)",
        r"(?:^|\s)(?:-R|--repo)[\s=]+([\w.-]+/[\w.-]+)",
    )
]
_NER_KINDS: Final = {
    "PERSON": "person", "ORGANIZATION": "company", "LOCATION": "place", "PHONE_NUMBER": "phone",
}  # fmt: skip
_NER_CHUNK: Final = 50_000
_NER_CACHE: Final = 100_000


def namey(text: str) -> bool:
    """Whether NER is worth running: free text, or a word that starts with a capital."""
    return " " in text or any(c.isupper() for c in text)


def _group(m: re.Match[str], kind: str, priority: int, base: int = 0) -> Hit:
    group = "v" if "v" in m.re.groupindex else 1
    return Hit(base + m.start(group), base + m.end(group), kind, priority)


def _basic(value: str) -> bool:
    try:
        return b":" in base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except ValueError:
        return False


class Ner:
    """Presidio over a spaCy model, built on first use. `available` tells whether it loaded."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._engine: object | None = None
        self._tried = False
        self._cache: dict[str, list[Hit]] = {}

    @property
    def available(self) -> bool:
        return self._load() is not None

    def _load(self) -> object | None:
        if not self._tried:
            self._tried = True
            try:
                from presidio_analyzer import AnalyzerEngine
                from presidio_analyzer.nlp_engine import NlpEngineProvider

                provider = NlpEngineProvider(
                    nlp_configuration={
                        "nlp_engine_name": "spacy",
                        "models": [{"lang_code": "en", "model_name": self._model}],
                    }
                )
                self._engine = AnalyzerEngine(
                    nlp_engine=provider.create_engine(), supported_languages=["en"]
                )
            except (ImportError, OSError, ValueError):
                self._engine = None
        return self._engine

    def find(self, text: str) -> list[Hit]:
        """Names in `text`; a scrub, its sweep and the learning pass ask about the same text."""
        if (cached := self._cache.get(text)) is not None:
            return cached
        from presidio_analyzer import AnalyzerEngine

        engine = self._load()
        if not isinstance(engine, AnalyzerEngine):
            return []
        hits: list[Hit] = []
        for base in range(0, len(text), _NER_CHUNK):
            chunk = text[base : base + _NER_CHUNK]
            for found in engine.analyze(
                chunk, language="en", entities=list(_NER_KINDS), score_threshold=0.4
            ):
                hits.append(
                    Hit(base + found.start, base + found.end, _NER_KINDS[found.entity_type], _P_NER)
                )
        if len(self._cache) >= _NER_CACHE:
            self._cache.clear()
        self._cache[text] = hits
        return hits


class Detector:
    def __init__(self, terms: Iterable[Term], ner: Ner | None = None) -> None:
        self._terms: dict[str, Term] = {}
        self._index = TermIndex()
        self._ner = ner
        self.add(terms)

    @property
    def ner_available(self) -> bool:
        return self._ner is not None and self._ner.available

    def add(self, terms: Iterable[Term]) -> list[Term]:
        """Add `terms` not known yet (by normalized text); returns the ones added."""
        added: list[Term] = []
        for term in sorted(terms, key=lambda t: len(t.text), reverse=True):
            norm = normalize(term.text)
            if norm and norm not in self._terms:
                self._terms[norm] = term
                self._index.add(term)
                added.append(term)
        return added

    def find(self, text: str, *, names: bool = True) -> list[Hit]:
        """Non-overlapping hits in `text`, in order. `names` adds the NER pass."""
        view = fold(text)
        hits = list(self._patterns(view.text))
        for start, end, term in self._index.find(view.text):
            hits.append(Hit(start, end, term.kind, _P_KNOWN, term.of, canon=term.text))
        if names and self._ner is not None and namey(view.text):
            hits += self._ner.find(view.text)
        out: list[Hit] = []
        for hit in _resolve(hits):
            start, end = view.span(hit.start, hit.end)
            out.append(replace(hit, start=start, end=end, text=view.text[hit.start : hit.end]))
        return out

    def term_for(self, text: str) -> Term | None:
        return self._terms.get(normalize(text))

    def _patterns(self, text: str) -> Iterator[Hit]:
        for p in _SECRETS:
            yield from (Hit(m.start(), m.end(), SECRET, _P_SECRET) for m in p.finditer(text))
        for p in _SECRET_VALUES:
            yield from (_group(m, SECRET, _P_SECRET) for m in p.finditer(text))
        for m in _BASIC.finditer(text):
            if _basic(m.group("v")):
                yield _group(m, SECRET, _P_SECRET)
        for m in _HOME.finditer(text):
            if m.group("v").casefold() not in _GENERIC_LOGINS:
                yield _group(m, "handle", _P_ID)
        for url in _URL.finditer(text):
            for p in (_URL_USERINFO, _URL_SECRET_PARAM):
                for m in p.finditer(url.group()):
                    yield _group(m, SECRET, _P_SECRET, url.start())
        for m in _EMAIL.finditer(text):
            term = self._terms.get(normalize(m.group()))
            yield Hit(m.start(), m.end(), "email", _P_EMAIL, term.of if term else None)
        yield from (Hit(m.start(), m.end(), "id", _P_ID) for m in _IDS.finditer(text))
        yield from (_group(m, "id", _P_ID) for m in _DOC_IDS.finditer(text))
        yield from (Hit(m.start(), m.end(), "phone", _P_ID) for m in _PHONE.finditer(text))
        for p in _REPOS:
            yield from (_group(m, "repo", _P_HOST) for m in p.finditer(text))
        yield from (Hit(m.start(), m.end(), "host", _P_HOST) for m in _HOST.finditer(text))
        for m in _TENANT_HOST.finditer(text):
            if m.group(1).casefold() not in _SHARED_HOSTS:
                yield _group(m, "company", _P_HOST)
        yield from (Hit(m.start(), m.end(), "ip", _P_HOST) for m in _IP.finditer(text))


def _merge(a: Hit, b: Hit) -> Hit:
    """One hit covering two that cross. A part never wins over the whole: the kind comes from
    the hit that spans both when there is one, and a secret makes the whole a secret."""
    start, end = min(a.start, b.start), max(a.end, b.end)
    top = max((a, b), key=lambda h: (h.priority, h.end - h.start))
    if (top.start, top.end) == (start, end):
        return top
    if SECRET in (a.kind, b.kind):
        return Hit(start, end, SECRET, _P_SECRET)
    wide = next((h for h in (a, b) if (h.start, h.end) == (start, end)), None)
    if wide is not None:
        return replace(wide, priority=top.priority)
    return Hit(start, end, top.kind, top.priority)


def _resolve(hits: list[Hit]) -> list[Hit]:
    kept: list[Hit] = []
    for hit in sorted(hits, key=lambda h: (h.start, -h.end)):
        if kept and hit.start < kept[-1].end:
            kept[-1] = _merge(kept[-1], hit)
        else:
            kept.append(hit)
    return kept

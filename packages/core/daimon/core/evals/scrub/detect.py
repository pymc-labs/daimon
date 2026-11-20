"""Finding what to replace in a string: secrets, ids, emails, hosts, known names, then names by NER.

Each finder returns `Hit`s; overlaps resolve by priority (a secret beats everything), then length.
Name detection (Presidio with a spaCy model) is optional: without it `Ner.available` is False
and the scrub still covers everything the dictionary and patterns know.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from daimon.core.evals.scrub.entities import Term, normalize

SECRET: Final = "secret"
_P_SECRET, _P_EMAIL, _P_KNOWN, _P_ID, _P_HOST, _P_NER = 6, 5, 4, 4, 3, 1


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    kind: str
    priority: int
    of: str | None = None


_SECRETS: Final = [
    re.compile(p)
    for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
        r"\bxox[abprsc]-[A-Za-z0-9-]{10,}\b",
        r"\bxapp-[A-Za-z0-9-]{10,}\b",
        r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        r"\b[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b",
    )
]
# The secret is group 1 of each: the label before it stays readable.
_SECRET_VALUES: Final = [
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in (
        r"\bBearer\s+([A-Za-z0-9._~+/=-]{16,})",
        r"^\s*(?:proxy-)?authorization\s*:\s*(?!bearer\b)(.+?)\s*$",
        r"^\s*(?:set-)?cookie\s*:\s*(.+?)\s*$",
        r"\b(?:api[_-]?key|secret|token|password|passwd|client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']?"
        r"([^\s\"',;&]{8,})",
    )
]
_URL: Final = re.compile(r"https?://[^\s<>\"'`)\]}]+", re.IGNORECASE)
_URL_USERINFO: Final = re.compile(r"//([^/@\s:]+:[^/@\s]+)@")
_URL_SECRET_PARAM: Final = re.compile(
    r"[?&;](?:x-amz-(?:signature|credential|security-token)|x-goog-(?:signature|credential)"
    r"|sig|signature|token|access_token|id_token|refresh_token|apikey|api_key|key|auth|code|t)"
    r"=([^&#;\s]+)",
    re.IGNORECASE,
)
_EMAIL: Final = re.compile(
    r"(?<![\w.%+-])(?!git@github\.com:)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_IDS: Final = re.compile(
    r"(?<![0-9A-Za-z])\d{17,20}(?![0-9A-Za-z])"  # Discord snowflake
    r"|\b[UWCDGT](?=[A-Z0-9]*\d)[A-Z0-9]{8,12}\b"  # Slack ids
    r"|\b(?:19|28|29):[0-9A-Za-z_-]{8,}(?:@thread\.[A-Za-z0-9]+|@unq\.gbl\.spaces)?"  # Teams ids
)
_HOST: Final = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:internal|local|corp|lan|intranet|home\.arpa)\b", re.IGNORECASE
)
_IP: Final = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
_REPOS: Final = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"github\.com[/:]([\w.-]+/[\w-][\w.-]*?)(?=\.git\b|[/\s?#\"')\]]|$)",
        r"(?:^|\s)(?:-R|--repo)[\s=]+([\w.-]+/[\w.-]+)",
    )
]
_NER_KINDS: Final = {
    "PERSON": "person", "ORGANIZATION": "company", "LOCATION": "place", "PHONE_NUMBER": "phone",
}  # fmt: skip
_NER_CHUNK: Final = 50_000
SECRET_KEYS: Final = frozenset(
    {
        "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
        "x-auth-token", "api_key", "apikey", "password", "passwd", "secret", "client_secret",
        "access_token", "refresh_token", "id_token", "token", "private_key", "bearer",
    }
)  # fmt: skip


def _group(m: re.Match[str], kind: str, priority: int) -> Hit:
    return Hit(m.start(1), m.end(1), kind, priority)


class Ner:
    """Presidio over a spaCy model, built on first use. `available` tells whether it loaded."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._engine: object | None = None
        self._tried = False

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

    def find(self, text: str) -> Iterator[Hit]:
        from presidio_analyzer import AnalyzerEngine

        engine = self._load()
        if not isinstance(engine, AnalyzerEngine):
            return
        for base in range(0, len(text), _NER_CHUNK):
            chunk = text[base : base + _NER_CHUNK]
            for found in engine.analyze(
                chunk, language="en", entities=list(_NER_KINDS), score_threshold=0.4
            ):
                yield Hit(
                    base + found.start, base + found.end, _NER_KINDS[found.entity_type], _P_NER
                )


class Detector:
    def __init__(self, terms: list[Term], ner: Ner | None = None) -> None:
        self._terms = {normalize(t.text): t for t in terms}
        self._ner = ner
        parts = [re.escape(t.text.strip()).replace(r"\ ", r"\s+") for t in terms if t.text.strip()]
        self._known = (
            re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)
            if parts
            else None
        )

    @property
    def ner_available(self) -> bool:
        return self._ner is not None and self._ner.available

    def find(self, text: str, *, names: bool = True) -> list[Hit]:
        """Non-overlapping hits in `text`, in order. `names` adds the NER pass (free text only)."""
        hits = list(self._patterns(text))
        if self._known:
            for m in self._known.finditer(text):
                term = self._terms[normalize(m.group())]
                hits.append(Hit(m.start(), m.end(), term.kind, _P_KNOWN, term.of))
        if names and self._ner is not None and " " in text:
            hits += self._ner.find(text)
        return _resolve(hits)

    def term_for(self, text: str) -> Term | None:
        return self._terms.get(normalize(text))

    def _patterns(self, text: str) -> Iterator[Hit]:
        for p in _SECRETS:
            yield from (Hit(m.start(), m.end(), SECRET, _P_SECRET) for m in p.finditer(text))
        for p in _SECRET_VALUES:
            yield from (_group(m, SECRET, _P_SECRET) for m in p.finditer(text))
        for url in _URL.finditer(text):
            for p in (_URL_USERINFO, _URL_SECRET_PARAM):
                for m in p.finditer(url.group()):
                    yield Hit(url.start() + m.start(1), url.start() + m.end(1), SECRET, _P_SECRET)
        for m in _EMAIL.finditer(text):
            term = self._terms.get(normalize(m.group()))
            yield Hit(m.start(), m.end(), "email", _P_EMAIL, term.of if term else None)
        yield from (Hit(m.start(), m.end(), "id", _P_ID) for m in _IDS.finditer(text))
        for p in _REPOS:
            yield from (_group(m, "repo", _P_HOST) for m in p.finditer(text))
        yield from (Hit(m.start(), m.end(), "host", _P_HOST) for m in _HOST.finditer(text))
        yield from (Hit(m.start(), m.end(), "ip", _P_HOST) for m in _IP.finditer(text))


def _resolve(hits: list[Hit]) -> list[Hit]:
    """Drop overlaps: of two hits that cross, the higher priority (then longer) one stays."""
    kept: list[Hit] = []
    for hit in sorted(hits, key=lambda h: (h.start, -h.end)):
        if kept and hit.start < kept[-1].end:
            last = kept[-1]
            if (hit.priority, hit.end - hit.start) > (last.priority, last.end - last.start):
                kept[-1] = hit
        else:
            kept.append(hit)
    return kept

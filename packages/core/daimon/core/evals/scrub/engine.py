"""`Scrubber`: replaces what `Detector` finds with consistent fakes, in text and in JSON.

One scrubber serves a whole suite (or tenant), so a real value gets one fake everywhere. It counts
replacements by kind and never keeps or prints the real text.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping

from daimon.core.evals.scrub.detect import SECRET, SECRET_KEYS, Detector, Hit, Ner
from daimon.core.evals.scrub.entities import KnownEntities, normalize, terms_of
from daimon.core.evals.scrub.fakes import FakeMap, adapt
from pydantic import JsonValue

REDACTED = "[REDACTED]"
_SHAPED = frozenset(
    {"person", "person_first", "person_last", "company", "project", "channel", "repo", "phrase"}
    | {"place", "host"}
)
_PLAIN_KEYS = frozenset({"sha256", "hash", "schema"})
_NAMES = frozenset({"person", "company", "place", "phone"})
_WORD = re.compile(r"[\w']+")


class Scrubber:
    def __init__(
        self,
        entities: KnownEntities,
        key: bytes,
        *,
        ner_model: str | None = "en_core_web_sm",
        known: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        terms = terms_of(entities)
        self.key = key
        self.detector = Detector(terms, Ner(ner_model) if ner_model else None)
        self.fakes = FakeMap(key, known=known, forbidden=[t.text for t in terms])
        self.counts: Counter[str] = Counter()
        self._fake_cache: tuple[int, set[str], set[str]] = (-1, set(), set())

    @property
    def names_checked(self) -> bool:
        """Whether names outside the dictionary are found too (NER loaded)."""
        return self.detector.ner_available

    # ---------- text ----------

    def _fake_sets(self) -> tuple[set[str], set[str]]:
        values = self.fakes.values()
        if self._fake_cache[0] != len(values):
            whole = {normalize(v) for v in values}
            words = {w for v in whole for w in _WORD.findall(v)}
            self._fake_cache = (len(values), whole, words)
        return self._fake_cache[1], self._fake_cache[2]

    def is_fake(self, text: str, kind: str) -> bool:
        """Whether `text` is one of our own fakes, which must be left alone."""
        whole, words = self._fake_sets()
        norm = normalize(text)
        return norm in whole or (kind in _NAMES and all(w in words for w in _WORD.findall(norm)))

    def _own(self, text: str, hit: Hit) -> bool:
        if text == REDACTED:
            return True
        known = hit.of is not None or self.detector.term_for(text) is not None
        return hit.kind != SECRET and not known and self.is_fake(text, hit.kind)

    def _replacement(self, matched: str, hit: Hit) -> str:
        if hit.kind == SECRET:
            return REDACTED
        fake = self.fakes.fake(hit.kind, matched, of=hit.of)
        return adapt(matched, fake) if hit.kind in _SHAPED else fake

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
        """A copy of `node` with every string scrubbed; secret-named keys lose their values."""
        if isinstance(node, str):
            if key in _PLAIN_KEYS:
                return node
            if key.casefold() in SECRET_KEYS and node:
                self.counts[SECRET] += 1
                return REDACTED
            return self.text(node, names=" " in node)
        if isinstance(node, list):
            return [self.value(v) for v in node]
        if isinstance(node, dict):
            return {self.text(k, names=False): self.value(v, key=k) for k, v in node.items()}
        return node

    def residue_of(self, node: JsonValue, *, key: str = "") -> Counter[str]:
        """`residue` over a document, keyed by `kind` only (never text)."""
        found: Counter[str] = Counter()
        if isinstance(node, str):
            if key not in _PLAIN_KEYS and not (key.casefold() in SECRET_KEYS and node == REDACTED):
                found += self.residue(node, names=" " in node)
        elif isinstance(node, list):
            for v in node:
                found += self.residue_of(v)
        elif isinstance(node, dict):
            for k, v in node.items():
                found += self.residue(k, names=False)
                found += self.residue_of(v, key=k)
        return found

"""The sweep: search finished documents and blobs for what a scrub would still replace.

A hit is reported by place and kind, never by text. Any hit stops a case from being `scrubbed`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from daimon.core.evals.scrub.engine import Scrubber
from daimon.core.evals.scrub.files import rebuild
from pydantic import JsonValue


@dataclass(frozen=True)
class Leftover:
    where: str
    kind: str
    count: int


class Probe:
    """A rewriter that changes nothing and counts what the scrubber would still replace."""

    def __init__(self, scrubber: Scrubber) -> None:
        self._scrubber = scrubber
        self.found: Counter[str] = Counter()

    def text(self, text: str, *, names: bool = True) -> str:
        self.found += self._scrubber.residue(text, names=names)
        return text

    def value(self, node: JsonValue, *, key: str = "") -> JsonValue:
        self.found += self._scrubber.residue_of(node, key=key)
        return node

    def spans(self, text: str) -> list[tuple[int, int]]:
        return []


def sweep_document(scrubber: Scrubber, document: JsonValue, where: str) -> list[Leftover]:
    return [Leftover(where, kind, n) for kind, n in sorted(scrubber.residue_of(document).items())]


def sweep_blob(
    scrubber: Scrubber, data: bytes, media_type: str, name: str | None, where: str
) -> list[Leftover]:
    """Leftovers in one blob. A type the rebuilder cannot read is itself a leftover."""
    if media_type.startswith("image/"):
        return []  # pixels cannot be searched; images are flagged for a person at rebuild time
    probe = Probe(scrubber)
    if rebuild(data, media_type, name, probe) is None:
        return [Leftover(where, "unsupported_blob", 1)]
    return [Leftover(where, kind, n) for kind, n in sorted(probe.found.items())]

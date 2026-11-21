"""The known-entities dictionary: names the deployment's own data tells us are private.

A JSON or YAML file with a list per kind. Person entries are a name or an object with
`aliases`, `emails`, `handles` and `ids`; companies, projects and channels may be a name or
`{name, aliases}`. Every other kind (groups and roles among them) is a list of strings.
`keep` lists public names (platforms, products, the suite's own ids) that are never replaced.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

TermKind = Literal[
    "person", "person_first", "person_last", "email", "handle", "company", "company_first",
    "project", "host", "repo", "channel", "phrase", "id",
]  # fmt: skip
MIN_PART: Final = 3
_LEGAL: Final = re.compile(
    r"[,\s]+(?:inc|incorporated|corp|corporation|co|company|ltd|limited|llc|llp|plc|gmbh|ag|sa"
    r"|bv|nv|oy|ab|as|pty|srl|spa|kk|group|holdings|industries|labs|technologies|systems)\.?$",
    re.IGNORECASE,
)
_COMMON_FIRST: Final = frozenset(
    {"the", "new", "first", "global", "united", "national", "international", "general", "big",
     "open", "best", "great", "north", "south", "east", "west", "american", "european"}
)  # fmt: skip


# Titles and suffixes are never a first or last name on their own ("Dr. Jane Doe").
_TITLES: Final = frozenset(
    {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "jr", "sr", "md", "phd", "dds", "dvm",
     "ii", "iii", "iv"}
)  # fmt: skip


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonEntry(_Model):
    name: str
    aliases: list[str] = Field(default_factory=list[str])
    emails: list[str] = Field(default_factory=list[str])
    handles: list[str] = Field(default_factory=list[str])
    ids: list[str] = Field(default_factory=list[str])


class NamedEntry(_Model):
    name: str
    aliases: list[str] = Field(default_factory=list[str])


class KnownEntities(_Model):
    people: list[str | PersonEntry] = Field(default_factory=list[str | PersonEntry])
    emails: list[str] = Field(default_factory=list[str])
    companies: list[str | NamedEntry] = Field(default_factory=list[str | NamedEntry])
    projects: list[str | NamedEntry] = Field(default_factory=list[str | NamedEntry])
    hostnames: list[str] = Field(default_factory=list[str])
    repos: list[str] = Field(default_factory=list[str])
    channels: list[str | NamedEntry] = Field(default_factory=list[str | NamedEntry])
    groups: list[str] = Field(default_factory=list[str])
    """Roles, user groups and teams."""
    ids: list[str] = Field(default_factory=list[str])
    phrases: list[str] = Field(default_factory=list[str])
    keep: list[str] = Field(default_factory=list[str])
    """Public names: never faked, learned or reported, even where a name of another kind holds
    one ("acme" stays private in `acme_search`)."""


@dataclass(frozen=True)
class Term:
    """One real string to replace. `of` is the entry it belongs to: its fake is derived from it."""

    kind: TermKind
    text: str
    of: str | None = None


_LETTERS: Final = {
    "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ß": "ss", "æ": "ae",
    "Æ": "AE", "œ": "oe", "Œ": "OE", "ı": "i", "þ": "th", "Þ": "Th", "ð": "d",
}  # fmt: skip


@lru_cache(maxsize=8192)
def fold_char(ch: str) -> str:
    """`ch` without accents; zero-width marks and soft hyphens become nothing."""
    if ch.isascii():
        return ch
    if unicodedata.category(ch) == "Cf":
        return ""
    if ch in _LETTERS:
        return _LETTERS[ch]
    return "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Case, accents and spacing folded: what a fake is keyed by."""
    return " ".join("".join(fold_char(c) for c in text).casefold().split())


def load_entities(path: Path) -> KnownEntities:
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    except yaml.YAMLError as failed:
        # Only where: the parser's message quotes the line, which holds private names.
        mark = getattr(failed, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ValueError(f"the entities file is not valid YAML{where}") from None
    return KnownEntities.model_validate(data or {})


def _named(entry: str | NamedEntry, kind: TermKind) -> list[Term]:
    name, aliases = (entry, []) if isinstance(entry, str) else (entry.name, entry.aliases)
    return [Term(kind, name), *(Term(kind, a, of=name) for a in aliases)]


def _company(entry: str | NamedEntry) -> list[Term]:
    """The company, its aliases, and its short forms: "Acme" and "Acme Corp" for "Acme Corp Inc"."""
    terms = _named(entry, "company")
    name = terms[0].text
    short = name
    while (cut := _LEGAL.sub("", short)) != short and cut:
        short = cut
    first = re.split(r"[\s,]+", short.strip())[0]
    if first != name and len(first) >= MIN_PART and first.casefold() not in _COMMON_FIRST:
        terms.append(Term("company_first", first, of=name))
    if short not in (name, first) and len(short) >= MIN_PART:
        terms.append(Term("company", short, of=name))
    return terms


def _person(entry: str | PersonEntry) -> list[Term]:
    person = PersonEntry(name=entry) if isinstance(entry, str) else entry
    # "Jane Roe (+3)", "Jane Roe (UTC+2)": a note in brackets and words without letters are no part.
    clean = re.sub(r"[(\[][^)\]]*[)\]]", " ", person.name).split()
    words = [
        w for w in clean if w.casefold().strip(".,") not in _TITLES and re.search(r"[^\W\d_]", w)
    ]
    terms = [Term("person", person.name)]
    if len(words) > 1:
        terms += [
            Term("person_first", words[0], of=person.name),
            Term("person_last", words[-1], of=person.name),
        ]
        terms = [t for t in terms if len(t.text) >= MIN_PART or t.kind == "person"]
    parts = {normalize(w) for w in words}
    for alias in person.aliases:
        # An alias that is just one word of the name takes that word's fake.
        terms.append(Term("person", alias, of=person.name))
        if normalize(alias) in parts and len(words) > 1:
            kind = "person_first" if normalize(alias) == normalize(words[0]) else "person_last"
            terms[-1] = Term(kind, alias, of=person.name)
    terms += [Term("email", e, of=person.name) for e in person.emails]
    terms += [Term("handle", h, of=person.name) for h in person.handles]
    terms += [Term("id", i) for i in person.ids]
    return terms


def _repo(slug: str) -> list[Term]:
    owner, _, name = slug.partition("/")
    terms = [Term("repo", slug)]
    if name:
        # The owner and name share fakes with the same words elsewhere.
        terms += [Term("company", owner), Term("project", name)]
    return [t for t in terms if t.kind == "repo" or len(t.text) >= MIN_PART]


def terms_of(entities: KnownEntities) -> list[Term]:
    """Every term, longest first; the first of equal normalized texts wins."""
    found: list[Term] = []
    for p in entities.people:
        found += _person(p)
    found += [Term("email", e) for e in entities.emails]
    for c in entities.companies:
        found += _company(c)
    for pr in entities.projects:
        found += _named(pr, "project")
    found += [Term("host", h) for h in entities.hostnames]
    for r in entities.repos:
        found += _repo(r)
    for ch in entities.channels:
        found += [Term(t.kind, re.sub(r"^#", "", t.text), t.of) for t in _named(ch, "channel")]
    found += [Term("channel", g) for g in entities.groups]
    found += [Term("id", i) for i in entities.ids]
    found += [Term("phrase", p) for p in entities.phrases]
    unique: dict[str, Term] = {}
    for term in found:
        if term.text.strip():
            unique.setdefault(normalize(term.text), term)
    return sorted(unique.values(), key=lambda t: len(t.text), reverse=True)


def merge_entities(*parts: KnownEntities) -> KnownEntities:
    """One dictionary holding every entry of `parts`."""
    merged: dict[str, list[object]] = {name: [] for name in KnownEntities.model_fields}
    for part in parts:
        for name in merged:
            merged[name] += getattr(part, name)
    return KnownEntities.model_validate(merged)

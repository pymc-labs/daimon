"""Pure text and scope helpers a run applies to each record before it is stored."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from typing import cast

CHUNK_CHARS = 1000
_BAD = re.compile("[\x00\ud800-\udfff]")


def clean[T](value: T) -> T:
    """Postgres text and jsonb reject NUL, and a lone UTF-16 surrogate (JSON may hold `\\ud83d`);
    some messages carry them."""
    out: object = value
    if isinstance(value, str):
        out = _BAD.sub("", value)
    elif isinstance(value, dict):
        out = {clean(k): clean(v) for k, v in cast("dict[object, object]", value).items()}
    elif isinstance(value, list | tuple | set):
        out = type(value)(clean(x) for x in cast("Iterable[object]", value))  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    elif is_dataclass(value) and not isinstance(value, type):
        out = replace(
            value, **{f.name: clean(getattr(value, f.name)) for f in fields(value) if f.init}
        )
    return cast("T", out)


def _glob(pattern: str) -> re.Pattern[str]:
    return re.compile("^" + "(.+)".join(map(re.escape, pattern.split("*"))) + "$", re.I)


def resolve_scope(rules: list[dict[str, str]], default: str, facets: Mapping[str, object]) -> str:
    """The first matching rule's scope, else `default`; `{1}` in a rule's scope is the first
    glob capture. A rule is `{"field": ..., "match": "<glob>", "scope": ...}`."""
    for rule in rules:
        value = facets.get(rule["field"])
        if value is None:
            continue
        found = _glob(rule["match"]).match(str(value))
        if found:
            scope = rule["scope"]
            for i, group in enumerate(found.groups(), 1):
                scope = scope.replace(f"{{{i}}}", group.lower())
            return scope
    return default


def context(
    when: datetime,
    source: str,
    container: str | None,
    container_kind: str | None,
    author: str | None,
) -> str:
    """Retrieval context put before a chunk for full-text search and embedding; a snippet
    shows the chunk's own text."""
    where = f"#{container}" if container and container_kind == "channel" else container
    parts = (when.strftime("%Y-%m-%d"), " ".join(filter(None, (source, where))), author)
    return " · ".join(x for x in parts if x)


def _cut(line: str) -> list[str]:
    """A line longer than a chunk, cut at a space past its middle where there is one."""
    out: list[str] = []
    while len(line) > CHUNK_CHARS:
        at = line.rfind(" ", CHUNK_CHARS // 2, CHUNK_CHARS)
        at = at if at > 0 else CHUNK_CHARS
        out.append(line[:at])
        line = line[at:].lstrip()
    return [*out, line]


def chunks(title: str, body: str) -> list[str]:
    """A record's text in pieces of about `CHUNK_CHARS`, each led by the title."""
    if len(body) <= CHUNK_CHARS:
        return [f"{title}\n{body}".strip()]
    out: list[str] = []
    cur = ""
    for part in (x for line in re.split(r"\n+", body) for x in _cut(line)):
        if cur and len(cur) + len(part) > CHUNK_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{part}" if cur else part
    if cur:
        out.append(cur)
    return [f"{title}\n{c}".strip() for c in out]

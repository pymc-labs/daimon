"""Skill entries from `skills/list`, checked before a byte of them is used. Pure.

An entry names its `SKILL.md`, carries the frontmatter verbatim and lists
every file with its size and SHA-256 (`resources`), or says `"dynamic"`. Each
file read is checked against that manifest, and the `SKILL.md` frontmatter
field by field against the entry's, before the files become a skill bundle.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol, cast
from urllib.parse import unquote, urlsplit

import yaml
from daimon.core.defaults.loader import RESERVED_SKILL_SUBSTRINGS
from daimon.core.skill_zip import MAX_FILES as PROVIDER_MAX_FILES
from daimon.core.skills.ingest import SkillIngestError
from pydantic import JsonValue

#: The extension's per-skill limits, files and bytes, `SKILL.md` included.
SPEC_MAX_FILES: Final = 512
SPEC_MAX_BYTES: Final = 16 * 1024 * 1024
#: A custom skill upload holds fewer files than the extension allows.
MAX_FILES: Final = min(SPEC_MAX_FILES, PROVIDER_MAX_FILES)
_NAME = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
#: Frontmatter is a few fields; anything larger is refused before it is parsed.
MAX_FRONTMATTER_BYTES: Final = 8 * 1024
_MAX_DEPTH: Final = 16


class SkillRejected(SkillIngestError):
    """An entry or a file that fails the extension's checks; the skill is not used."""


@dataclass(frozen=True, slots=True)
class SkillFile:
    uri: str
    path: str
    """Where the file sits under the skill's directory, `SKILL.md` for the root."""
    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One checked `skills/list` entry with a static manifest."""

    uri: str
    name: str
    frontmatter: Mapping[str, JsonValue]
    files: tuple[SkillFile, ...]

    @property
    def manifest_digest(self) -> str:
        """Names the skill's exact content: frontmatter, paths, sizes and digests."""
        canonical = json.dumps(
            {
                "uri": self.uri,
                "frontmatter": self.frontmatter,
                "files": [[f.path, f.digest, f.size] for f in self.files],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def _segments(uri: str) -> tuple[str, str, tuple[str, ...]]:
    parsed = urlsplit(uri)
    if not parsed.scheme or parsed.query or parsed.fragment:
        raise SkillRejected("resource URI must be a plain URI with a scheme")
    segments: list[str] = []
    for raw in parsed.path.split("/"):
        if not raw:
            continue
        segment = unquote(raw)
        if segment in (".", "..") or "/" in segment or "\\" in segment:
            raise SkillRejected("resource URI has an invalid path segment")
        segments.append(segment)
    return parsed.scheme.lower(), parsed.netloc, tuple(segments)


def _file(raw: JsonValue, *, root: tuple[str, str, tuple[str, ...]]) -> SkillFile:
    if not isinstance(raw, dict) or set(raw) != {"uri", "digest", "size"}:
        raise SkillRejected("manifest entry must have exactly uri, digest and size")
    uri, digest, size = raw["uri"], raw["digest"], raw["size"]
    if not isinstance(uri, str) or not isinstance(digest, str) or not _DIGEST.match(digest):
        raise SkillRejected("manifest entry has an invalid uri or digest")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise SkillRejected("manifest entry has an invalid size")
    scheme, authority, segments = _segments(uri)
    prefix = root[2]
    if (scheme, authority) != root[:2] or segments[: len(prefix)] != prefix:
        raise SkillRejected("manifest file lies outside the skill's directory")
    path = "/".join(segments[len(prefix) :])
    if not path or not _SAFE_PATH.match(path):
        raise SkillRejected("manifest file path cannot be uploaded")
    return SkillFile(uri=uri, path=path, digest=digest, size=size)


def parse_entry(raw: JsonValue) -> SkillEntry | None:
    """A checked entry, or None for a `"dynamic"` one, which is skipped.

    Raises `SkillRejected` for anything the extension or the upload forbids.
    """
    if not isinstance(raw, dict):
        raise SkillRejected("skill entry is not an object")
    uri, frontmatter, resources = raw.get("uri"), raw.get("frontmatter"), raw.get("resources")
    if not isinstance(uri, str) or not isinstance(frontmatter, dict):
        raise SkillRejected("skill entry needs a uri and frontmatter")
    name, description = frontmatter.get("name"), frontmatter.get("description")
    if not isinstance(name, str) or not _NAME.match(name):
        raise SkillRejected("skill name is invalid")
    if any(reserved in name for reserved in RESERVED_SKILL_SUBSTRINGS):
        raise SkillRejected("skill name holds a reserved word")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise SkillRejected("skill description is invalid")
    scheme, authority, segments = _segments(uri)
    if not segments or segments[-1] != "SKILL.md":
        raise SkillRejected("skill uri must name a SKILL.md")
    directory = segments[:-1]
    if (directory[-1] if directory else unquote(authority)) != name:
        raise SkillRejected("skill directory must match its name")
    if resources == "dynamic":
        return None
    if not isinstance(resources, list):
        raise SkillRejected("skill resources must be a manifest or dynamic")
    if len(resources) > MAX_FILES:
        raise SkillRejected("skill has too many files")
    files = tuple(_file(item, root=(scheme, authority, directory)) for item in resources)
    if len({f.uri for f in files}) != len(files) or len({f.path for f in files}) != len(files):
        raise SkillRejected("manifest lists a file twice")
    if not any(f.uri == uri and f.path == "SKILL.md" for f in files):
        raise SkillRejected("manifest must include the skill's SKILL.md")
    if sum(f.size for f in files) > SPEC_MAX_BYTES:
        raise SkillRejected("skill is larger than the extension allows")
    return SkillEntry(
        uri=uri,
        name=name,
        frontmatter=cast("dict[str, JsonValue]", frontmatter),
        files=tuple(sorted(files, key=lambda f: f.path)),
    )


def verify_file(file: SkillFile, content: bytes) -> None:
    """Raise `SkillRejected` unless `content` has the manifest's size and digest."""
    if len(content) != file.size:
        raise SkillRejected("file size does not match the manifest")
    if f"sha256:{hashlib.sha256(content).hexdigest()}" != file.digest:
        raise SkillRejected("file digest does not match the manifest")


def _json_like(value: object) -> object:
    """YAML dates read back as the ISO strings the entry's JSON carries."""
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_like(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        return {str(k): _json_like(v) for k, v in cast("dict[object, object]", value).items()}
    return value


class _EventStream(Protocol):
    def check_event(self) -> bool: ...
    def get_event(self) -> yaml.Event: ...
    def dispose(self) -> None: ...


def _refuse_references(block: str) -> None:
    """Refuse anchors, aliases and merge keys, which can expand a few bytes without bound,
    and nesting deep enough to exhaust the composer's recursion.

    Runs over parse events, before anything is built from them.
    """
    depth = 0
    # The stubs leave the loader's event methods untyped.
    loader = cast("_EventStream", yaml.SafeLoader(block))
    try:
        while loader.check_event():
            event = loader.get_event()
            if isinstance(event, yaml.CollectionStartEvent):
                depth += 1
                if depth > _MAX_DEPTH:
                    raise SkillRejected("SKILL.md frontmatter is nested too deeply")
            elif isinstance(event, yaml.CollectionEndEvent):
                depth -= 1
            if isinstance(event, yaml.AliasEvent) or (
                isinstance(event, yaml.NodeEvent) and event.anchor is not None
            ):
                raise SkillRejected("SKILL.md frontmatter uses YAML anchors or aliases")
            if isinstance(event, yaml.ScalarEvent) and event.value == "<<" and not event.style:
                raise SkillRejected("SKILL.md frontmatter uses a YAML merge key")
    finally:
        loader.dispose()


def read_frontmatter(skill_md: bytes) -> dict[str, object]:
    """The YAML frontmatter `SKILL.md` must begin with: plain YAML, at most 8 KiB.

    CPU-bound: async callers run it off the event loop."""
    try:
        text = skill_md.decode()
    except UnicodeDecodeError as err:
        raise SkillRejected("SKILL.md is not UTF-8") from err
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillRejected("SKILL.md does not begin with frontmatter")
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise SkillRejected("SKILL.md frontmatter is not closed")
    block = "\n".join(lines[1:end])
    if len(block.encode()) > MAX_FRONTMATTER_BYTES:
        raise SkillRejected("SKILL.md frontmatter is too large")
    try:
        _refuse_references(block)
        loaded: object = yaml.safe_load(block)
    # Deep nesting overflows the parser's recursion: refused like any other bad YAML.
    except (yaml.YAMLError, RecursionError) as err:
        raise SkillRejected("SKILL.md frontmatter is not plain YAML") from err
    normalized = _json_like(loaded)
    if not isinstance(normalized, dict):
        raise SkillRejected("SKILL.md frontmatter is not a mapping")
    return cast("dict[str, object]", normalized)


def verify_frontmatter(entry: SkillEntry, skill_md: bytes) -> None:
    """Raise `SkillRejected` unless `SKILL.md`'s frontmatter equals the entry's."""
    if read_frontmatter(skill_md) != dict(entry.frontmatter):
        raise SkillRejected("SKILL.md frontmatter does not match the listing")

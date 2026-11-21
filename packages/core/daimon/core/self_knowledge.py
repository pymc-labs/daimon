"""`self_knowledge`: this release's docs and source, read-only and confined to their roots.

Synchronous file reads (the MCP tools run them in a thread). No settings, network or tenant
data: the caller passes the roots, the public addresses and whether `web_awareness` is On
where the call runs. Only `.md` pages under the docs root and text files under `packages/`
are read; dotfiles, symlinks out of a root and other files are refused. `defaults/` is not
served: an operator may mount their own, holding other agents' prompts.
"""

from __future__ import annotations

import functools
import os
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Concatenate, Final

SELF_KNOWLEDGE_HINT: Final = (
    "This release's docs and source can be read: search_tools finds read_daimon_docs, "
    "search_daimon_docs, read_daimon_source and search_daimon_source. Read them before "
    "answering how Daimon works, how to set it up or why it behaves as it does."
)
"""The turn controls' note where `self_knowledge` is On: the tools sit behind `search_tools`."""


def controls_hint(*, on: bool, held: bool) -> str | None:
    """`SELF_KNOWLEDGE_HINT` for a turn's controls where the feature is On, none for a held
    turn: it refuses these tools (`mcp_hold.HELD_TOOL_ALLOWLIST`). Every renderer of a
    `<turn_controls>` element that opens a person's or a routine's turn calls this."""
    return SELF_KNOWLEDGE_HINT if on and not held else None


OVERVIEW_PAGE: Final = "how-daimon-works.md"
PAGE_INDEX: Final = "llms.txt"
SOURCE_DIR: Final = "packages"
_CORE, _MCP_TOOLS = "packages/core/daimon/core", "packages/adapters/mcp/daimon/adapters/mcp/tools"
WEB_SOURCE: Final = (
    "packages/adapters/web",
    "packages/adapters/web/*",
    f"{_CORE}/web_*.py",
    f"{_CORE}/stores/web_*.py",
    f"{_MCP_TOOLS}/web_*.py",
    f"{_MCP_TOOLS}/_web_*.py",
)
"""fnmatch patterns of the web app's own code (its adapter, core modules and stores, and its
chat tools): hidden with the web pages where `web_awareness` is Off."""
WEB_SOURCE_KEPT: Final = (f"{_CORE}/stores/web_search_usage.py",)
"""Matched above but not the web app's: the model's web search allowance."""
TEXT_SUFFIXES: Final = frozenset(
    {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".sh", ".sql",
     ".mako", ".html", ".css", ".csv", ".typ"}
)  # fmt: skip
MAX_CHARS: Final = 30_000
MAX_LINES: Final = 400
MAX_MATCHES: Final = 40
MAX_LINE_CHARS: Final = 240
MAX_FILE_BYTES: Final = 1_000_000
MIN_QUERY_CHARS, MAX_QUERY_CHARS = 2, 200
_SKIPPED_DIRS: Final = frozenset({"__pycache__", "node_modules"})
_WEB_PAGE: Final = re.compile(r"^knowledge:\s*web\s*$", re.MULTILINE)
_WEB_OPEN, _WEB_CLOSE = "<!-- knowledge: web -->", "<!-- /knowledge -->"
_FENCES: Final = ("```", "~~~")
_INDEX_ENTRY: Final = re.compile(
    r"^- \[(?P<title>[^\]]+)\]\((?P<page>[^)\s]+)\)(?::\s*(?P<about>.*))?$"
)


class SelfKnowledgeError(ValueError):
    """A request refused; its text is for the agent."""


_UNREADABLE: Final = "That can't be read here."


def _refusing[**P, T](
    method: Callable[Concatenate[Library, P], T],
) -> Callable[Concatenate[Library, P], T]:
    """A `Library` call that turns what a bad path or an odd file raises (a NUL byte, a name
    too long, a file that is gone, unreadable or not UTF-8) into `SelfKnowledgeError`, so no
    server path or raw error reaches the agent."""

    @functools.wraps(method)
    def call(self: Library, *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return method(self, *args, **kwargs)
        except SelfKnowledgeError:
            raise
        except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
            raise SelfKnowledgeError(_UNREADABLE) from exc

    return call


def visible_text(text: str, *, web: bool) -> str | None:
    """`text` as a reader here sees it: None for a web page (front matter `knowledge: web`)
    while `web` is False, else without its marker lines and, while False, the web blocks
    between them. An unclosed block hides the rest of the page; a marker inside a code fence
    is text, not a marker."""
    if not web and text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1 and _WEB_PAGE.search(text[4:end]):
            return None
    kept: list[str] = []
    hiding = fenced = False
    for line in text.splitlines():
        marker = line.strip()
        if marker.startswith(_FENCES):
            fenced = not fenced
        elif not fenced and marker in (_WEB_OPEN, _WEB_CLOSE):
            hiding = marker == _WEB_OPEN and not web
            continue
        if not hiding:
            kept.append(line)
    return "\n".join(kept)


@dataclass(frozen=True)
class IndexEntry:
    section: str
    title: str
    page: str
    about: str


def parse_page_index(text: str) -> tuple[str, tuple[IndexEntry, ...]]:
    """`llms.txt`'s summary and pages: `> summary`, `## Section`, `- [Title](page.md): about`."""
    summary, section, entries = "", "", list[IndexEntry]()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("> ") and not summary:
            summary = line[2:].strip()
        elif line.startswith("## "):
            section = line[3:].strip()
        elif (found := _INDEX_ENTRY.match(line)) is not None:
            page = found["page"].removeprefix("./")
            entries.append(IndexEntry(section, found["title"], page, found["about"] or ""))
    return summary, tuple(entries)


def _named(text: str) -> str:
    """`text` as a short quoted name for a refusal."""
    return repr(text if len(text) <= 60 else text[:60] + "...")


_HEADING: Final = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_LINK: Final = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def anchor_slug(heading: str) -> str:
    """The id the docs site gives a heading (Python-Markdown's `toc` slugify): markup
    dropped, ASCII only, lowercase, runs of spaces and hyphens one hyphen, `_` kept."""
    text = re.sub(r"[`*]", "", _LINK.sub(r"\1", heading))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", text).strip().lower())


def sections(text: str) -> dict[str, tuple[int, int]]:
    """Each heading's anchor and its lines (start, end) in `text`: the heading to the next of
    its level or above. Headings in code fences are not headings; a repeat gets `_1`, `_2`
    as the site does."""
    lines = text.splitlines()
    found: list[tuple[int, int, str]] = []
    seen: dict[str, int] = {}
    fenced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(_FENCES):
            fenced = not fenced
        elif not fenced and (heading := _HEADING.match(line)) is not None:
            slug = anchor_slug(heading[2])
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.append((index, len(heading[1]), slug if count == 0 else f"{slug}_{count}"))
    return {
        slug: (
            index,
            next((later for later, deeper, _ in found[at + 1 :] if deeper <= level), len(lines)),
        )
        for at, (index, level, slug) in enumerate(found)
    }


def _parts(relative: str) -> tuple[str, ...]:
    """`relative`'s parts; refuses an absolute path, `..` and any dot-named part."""
    path = PurePosixPath(relative.strip().replace("\\", "/"))
    if path.is_absolute() or any(part.startswith(".") for part in path.parts):
        raise SelfKnowledgeError(f"{_named(relative)} is outside what can be read.")
    return tuple(part for part in path.parts if part)


def _inside(root: Path, parts: tuple[str, ...]) -> Path:
    """`root/parts`, symlinks resolved; refuses anything that lands outside `root`."""
    base = root.resolve()
    target = base.joinpath(*parts).resolve()
    if not target.is_relative_to(base) or any(
        part.startswith(".") for part in target.relative_to(base).parts
    ):
        raise SelfKnowledgeError(f"{_named('/'.join(parts))} is outside what can be read.")
    return target


def _query(query: str) -> str:
    needle = query.strip().casefold()
    if not MIN_QUERY_CHARS <= len(needle) <= MAX_QUERY_CHARS:
        raise SelfKnowledgeError(
            f"Search for {MIN_QUERY_CHARS} to {MAX_QUERY_CHARS} characters of plain text."
        )
    return needle


def _window(text: str, start_line: int, *, numbered: bool) -> str:
    """Lines from `start_line`, at most `MAX_LINES` and `MAX_CHARS`, ending in where they came
    from and how to read on."""
    lines = text.splitlines()
    start = max(start_line, 1)
    if lines and start > len(lines):
        raise SelfKnowledgeError(f"It has {len(lines)} lines; start_line is past the end.")
    shown: list[str] = []
    size = 0
    for number in range(start, min(len(lines), start + MAX_LINES - 1) + 1):
        line = f"{number:>5}  {lines[number - 1]}" if numbered else lines[number - 1]
        if shown and size + len(line) > MAX_CHARS:
            break
        shown.append(line[:MAX_CHARS])
        size += len(line) + 1
    end = start + len(shown) - 1
    more = f" Continue with start_line={end + 1}." if end < len(lines) else ""
    return "\n".join([*shown, "", f"(Lines {start}-{end} of {len(lines)}.{more})"])


def _clip(line: str) -> str:
    line = line.strip()
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "..."


@dataclass(frozen=True)
class Library:
    """The docs and source one call may read. `web` is `web_awareness` where the call runs."""

    docs: Path
    source: Path
    docs_url: str
    repo_url: str
    release: str
    web: bool

    @property
    def provenance(self) -> str:
        return (
            f"Daimon release {self.release}. Docs site: {self.docs_url}. Source: "
            f"{self.repo_url} (clone it at {self.release} for tests and history)."
        )

    def _need_docs(self) -> None:
        if not self.docs.is_dir():
            raise SelfKnowledgeError("This deployment has no Daimon docs installed to read.")

    def _need_source(self) -> None:
        if not (self.source / SOURCE_DIR).is_dir():
            raise SelfKnowledgeError("This deployment has no Daimon source installed to read.")

    def _page(self, page: str) -> str | None:
        """A page's visible text; None when it is hidden here or missing."""
        parts = _parts(page)
        path = _inside(self.docs, parts)
        if path.suffix != ".md" or not path.is_file():
            return None
        try:
            return visible_text(path.read_text(encoding="utf-8"), web=self.web)
        except (OSError, ValueError):  # unreadable or not UTF-8: as if the page were not there
            return None

    def _visible(self, page: str) -> str | None:
        """`_page`, None too for a page outside what can be read (an index entry's)."""
        try:
            return self._page(page)
        except SelfKnowledgeError:
            return None

    def _pages(self) -> Iterator[tuple[str, str]]:
        """Every visible page under the docs root, as (relative path, text)."""
        base = self.docs.resolve()
        for path in sorted(base.rglob("*.md")):
            relative = path.relative_to(base).as_posix()
            if (text := self._visible(relative)) is not None:
                yield relative, text

    @_refusing
    def read_docs(self, page: str = "", start_line: int = 1) -> str:
        """A page, or its `#anchor` section (`page.md#anchor`), from `start_line`; with no page,
        how Daimon works and every page."""
        self._need_docs()
        if page.strip():
            name, _, anchor = page.strip().partition("#")
            text = self._page(name)
            if text is None:
                raise SelfKnowledgeError(
                    f"There is no page {_named(name)}. Read with no page for the list."
                )
            if anchor:
                found = sections(text).get(anchor)
                if found is None:
                    raise SelfKnowledgeError(f"{_named(name)} has no section {_named(anchor)}.")
                text = "\n".join(text.splitlines()[slice(*found)])
            return f"{self.provenance}\n\n{page}\n\n{_window(text, start_line, numbered=False)}"
        index = self.docs / PAGE_INDEX
        summary, entries = parse_page_index(
            index.read_text(encoding="utf-8") if index.is_file() else ""
        )
        if not entries:
            entries = tuple(
                IndexEntry("Pages", _title(text) or relative, relative, "")
                for relative, text in self._pages()
            )
        head = [self.provenance, *([summary] if summary else [])]
        overview = self._page(OVERVIEW_PAGE)
        if overview is not None:
            head.append(_window(overview, 1, numbered=False))
        listed: list[str] = ["## Pages"]
        section: str | None = None
        for entry in entries:
            if self._visible(entry.page) is None:
                continue
            if entry.section != section:
                section = entry.section
                listed.append(f"### {section}")
            about = f": {entry.about}" if entry.about else ""
            listed.append(f"- {entry.page} ({entry.title}){about}")
        return "\n\n".join([*head, "\n".join(listed)])

    @_refusing
    def search_docs(self, query: str) -> str:
        needle = _query(query)
        self._need_docs()
        hits = (
            f"{relative}:{number}: {_clip(line)}"
            for relative, text in self._pages()
            for number, line in enumerate(text.splitlines(), 1)
            if needle in line.casefold()
        )
        return self._results(hits, query)

    def _source_path(self, path: str) -> tuple[Path, tuple[str, ...]]:
        parts = _parts(path)
        if parts and parts[0] != SOURCE_DIR:
            raise SelfKnowledgeError(f"Only {SOURCE_DIR}/ can be read.")
        target = _inside(self.source, parts)
        relative = target.relative_to(self.source.resolve())
        if (relative.parts and relative.parts[0] != SOURCE_DIR) or not self._shown(
            PurePosixPath(relative.as_posix())
        ):
            raise SelfKnowledgeError(f"There is no {_named(path)} here.")
        if not target.exists():
            raise SelfKnowledgeError(f"There is no {_named(path)} here.")
        return target, relative.parts

    def _shown(self, relative: PurePosixPath) -> bool:
        path = relative.as_posix()
        return (
            self.web
            or path in WEB_SOURCE_KEPT
            or not any(fnmatchcase(path, pattern) for pattern in WEB_SOURCE)
        )

    @_refusing
    def read_source(self, path: str = "", start_line: int = 1) -> str:
        """A text file from `start_line`, or a directory's entries."""
        self._need_source()
        target, parts = self._source_path(path)
        shown = "/".join(parts) or "."
        if target.is_dir():
            entries = (
                [f"{SOURCE_DIR}/"]
                if not parts
                else [
                    f"{child.name}/" if child.is_dir() else child.name
                    for child in sorted(target.iterdir())
                    if self._listed(child, PurePosixPath(*parts, child.name))
                ]
            )
            return f"{self.provenance}\n\n{shown}/\n" + "\n".join(entries)
        text = _window(_source_text(target), start_line, numbered=True)
        return f"{self.provenance}\n\n{shown}\n\n{text}"

    def _listed(self, child: Path, relative: PurePosixPath) -> bool:
        if child.name.startswith(".") or child.name in _SKIPPED_DIRS or not self._shown(relative):
            return False
        return child.is_dir() or child.suffix in TEXT_SUFFIXES

    @_refusing
    def search_source(self, query: str, path: str = "") -> str:
        needle = _query(query)
        self._need_source()
        target, parts = self._source_path(path)
        base = self.source.resolve()
        files = [target] if target.is_file() else self._files(target, parts)
        hits = (
            f"{file.relative_to(base).as_posix()}:{number}: {_clip(line)}"
            for file in files
            for number, line in _lines(file)
            if needle in line.casefold()
        )
        return self._results(hits, query)

    def _files(self, top: Path, parts: tuple[str, ...]) -> Iterator[Path]:
        base = self.source.resolve()
        roots = [top / SOURCE_DIR] if not parts else [top]
        for root in roots:
            for folder, dirs, names in os.walk(root):
                here = Path(folder)
                dirs[:] = sorted(
                    name
                    for name in dirs
                    if self._listed(here / name, PurePosixPath((here / name).relative_to(base)))
                )
                for name in sorted(names):
                    file = here / name
                    relative = PurePosixPath(file.relative_to(base).as_posix())
                    if self._listed(file, relative) and self._confined(relative):
                        yield file

    def _confined(self, relative: PurePosixPath) -> bool:
        """Whether a found file, its symlinks resolved, stays readable under the source root."""
        try:
            target = _inside(self.source, relative.parts)
        except SelfKnowledgeError:
            return False
        real = PurePosixPath(target.relative_to(self.source.resolve()).as_posix())
        return real.parts[0] == SOURCE_DIR and self._shown(real)

    def _results(self, hits: Iterator[str], query: str) -> str:
        found: list[str] = []
        for hit in hits:
            if len(found) == MAX_MATCHES:
                found.append(f"(More than {MAX_MATCHES} matches: search for something narrower.)")
                break
            found.append(hit)
        body = "\n".join(found) if found else f"Nothing matches {query.strip()!r}."
        return f"{self.provenance}\n\n{body}"


def _title(text: str) -> str | None:
    return next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), None)


def _source_text(path: Path) -> str:
    refusal = SelfKnowledgeError(f"{_named(path.name)} is not a text file that can be read here.")
    try:
        fits = path.suffix in TEXT_SUFFIXES and path.stat().st_size <= MAX_FILE_BYTES
        text = path.read_text(encoding="utf-8") if fits else None
    except (OSError, ValueError) as exc:  # gone, unreadable or not UTF-8
        raise refusal from exc
    if text is None:
        raise refusal
    return text


def _lines(path: Path) -> Iterator[tuple[int, str]]:
    """A file's numbered lines, nothing for a large or undecodable one."""
    try:
        yield from enumerate(_source_text(path).splitlines(), 1)
    except SelfKnowledgeError:
        return

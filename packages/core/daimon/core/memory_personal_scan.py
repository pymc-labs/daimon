"""A read-only scan of shared memory stores for entries that look personal.

Shared memory written before personal memory existed may hold one person's
preferences, which every user of the agent reads. Nothing here moves or
deletes anything: `scan_shared_memory` only lists and reads memories, and the
report it builds carries counts and redacted examples, never raw content.

The heuristics are deliberately simple and will both miss and over-match:

- a line that names someone (a platform mention such as `<@123>` or `@alice`,
  or a capitalised word before a preference verb) and uses a preference word
  (`prefers`, `likes`, `wants`, `favourite` ...), or
- a line in the first person with a preference (`my`, `I want`, `I prefer` ...),
- or a path that looks per-person (`/users/`, `/people/`, `/members/`, a
  mention in the path).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic
from daimon.core.mux_compat import list_memory_entries, read_memory
from mux.contracts.ids import Scope

_MENTION = re.compile(r"<@[!&]?[A-Za-z0-9]+>|(?<![\w.])@[A-Za-z0-9_.-]{2,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PREFERENCE = re.compile(
    r"\b(prefers?|preferred|preference|likes?|dislikes?|loves?|hates?|wants?|"
    r"favou?rites?|always|never|call (?:me|him|her|them)|pronouns?)\b",
    re.IGNORECASE,
)
_FIRST_PERSON = re.compile(r"\b(my|i want|i prefer|i like|i'd like|i need|me)\b", re.IGNORECASE)
_NAME_BEFORE_VERB = re.compile(
    r"\b[A-Z][a-z]{1,20}\s+(?:prefers?|likes?|wants?|dislikes?|loves?|hates?)\b"
)
_PERSONAL_PATH = re.compile(
    r"(^|/)(users?|people|persons?|members?|individuals?|per[-_]user|profiles?)(/|$)|<@|@",
    re.IGNORECASE,
)
_KEEP_WORDS = {
    "prefers", "prefer", "preferred", "preference", "likes", "like", "dislikes", "dislike",
    "loves", "love", "hates", "hate", "wants", "want", "favourite", "favorite", "always",
    "never", "my", "i", "me", "pronoun", "pronouns", "call",
}  # fmt: skip


def line_looks_personal(line: str) -> bool:
    """Whether one memory line looks like a fact about a specific person."""
    if not _PREFERENCE.search(line) and not _FIRST_PERSON.search(line):
        return False
    names_someone = bool(_MENTION.search(line) or _NAME_BEFORE_VERB.search(line))
    first_person = bool(_FIRST_PERSON.search(line))
    return (names_someone and bool(_PREFERENCE.search(line))) or (
        first_person and bool(_PREFERENCE.search(line) or re.search(r"(?i)\bi want\b", line))
    )


def path_looks_personal(path: str) -> bool:
    return bool(_PERSONAL_PATH.search(path))


def redact_line(line: str, *, max_words: int = 12) -> str:
    """The line's shape without its content: preference words kept, all else masked.

    Mentions become `<mention>`, e-mail addresses `<email>`, capitalised words
    `<name>`, and every other word `…` (runs collapsed), so no raw personal
    content survives. No square brackets, so the text is safe to print as
    Rich markup.
    """
    text = _EMAIL.sub(" <email> ", _MENTION.sub(" <mention> ", line))
    out: list[str] = []
    for word in text.split()[: max_words * 3]:
        bare = word.strip(".,;:!?()[]\"'*_`").lower()
        if word in ("<mention>", "<email>"):
            token = word
        elif bare in _KEEP_WORDS:
            token = bare
        elif word[:1].isupper():
            token = "<name>"
        else:
            token = "…"
        if not (token == "…" and out and out[-1] == "…"):
            out.append(token)
        if len(out) >= max_words:
            break
    return " ".join(out)


def redact_path(path: str) -> str:
    """Keep a path's generic segments and extension; mask the rest."""
    parts: list[str] = []
    for segment in path.strip("/").split("/"):
        stem, dot, ext = segment.rpartition(".")
        name = stem if dot else segment
        kept = name if _PERSONAL_PATH.fullmatch(name) or name.lower() in _GENERIC else "<redacted>"
        parts.append(kept + (f".{ext}" if dot and ext.lower() in _SAFE_EXTENSIONS else ""))
    return "/" + "/".join(parts)


# Only conventional extensions survive; any other suffix may be a name or mention.
_SAFE_EXTENSIONS = {"md", "txt", "json", "yaml", "yml", "csv", "toml", "html", "py"}

_GENERIC = {
    "notes", "prefs", "preferences", "people", "users", "user", "members", "team",
    "projects", "project", "conventions", "skills", "tools", "decisions", "memory",
}  # fmt: skip


@dataclass
class StoreScan:
    """What the scan found in one shared store."""

    memory_store_id: str
    memories: int = 0
    flagged: int = 0
    examples: list[str] = field(default_factory=list[str])


async def scan_shared_memory(
    anthropic: AsyncAnthropic,
    store_id: str,
    *,
    max_examples: int = 3,
    scope: Scope | None = None,
) -> StoreScan:
    """Count a store's memories that look personal. Reads only (list + retrieve)."""
    backend_scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.memory_personal_scan:scan_shared_memory"
    )
    scan = StoreScan(memory_store_id=store_id)
    entries = list_memory_entries(anthropic, store_id, path_prefix="/", scope=backend_scope)
    items = [item async for item in entries if item.type == "memory"]
    for item in items:
        scan.memories += 1
        memory = await read_memory(anthropic, store_id, item.id, scope=backend_scope)
        lines = [line for line in (memory.content or "").splitlines() if line.strip()]
        hit = next((line for line in lines if line_looks_personal(line)), None)
        if hit is None and not path_looks_personal(item.path):
            continue
        scan.flagged += 1
        if len(scan.examples) < max_examples:
            shape = redact_line(hit) if hit is not None else "(per-person path)"
            scan.examples.append(f"{redact_path(item.path)}: {shape}")
    return scan

"""Repository memory semantics, separate from provider-managed agent memory."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from xml.sax.saxutils import escape, quoteattr

from daimon.core.stores.repo_memory import MemoryEntry, record_turn_state, turn_entries
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MEMORY_BLOCK_BYTES = 48_000
MEMORY_QUERY_ROWS = 512
MEMORY_BODY_CHARS = 12_000


@dataclass(frozen=True)
class MemoryOrigin:
    """Server-verified origin facts; never accepted from model arguments."""

    interactive: bool
    resolved: bool = True
    restricted: bool = False
    held: bool = False
    foreign: bool = False
    read_only: bool = False

    def require(self, *, write: bool) -> None:
        if not self.resolved or not self.interactive:
            raise ValueError("Repository memory tools need a verified interactive conversation.")
        if self.held or self.foreign:
            raise ValueError("Repository memory is unavailable in held or foreign conversations.")
        if write and (self.restricted or self.read_only):
            raise ValueError("Cannot change repository memory from a restricted conversation.")


def slugify(title: str) -> str:
    chars: list[str] = []
    for ch in title.lower():
        if ch.isalnum():
            chars.append(ch)
        elif chars and chars[-1] != "-":
            chars.append("-")
    return "".join(chars).strip("-")[:64]


def new_entry(
    *,
    tenant_id: uuid.UUID,
    repo_id: int,
    repo_name: str,
    account_id: uuid.UUID,
    title: str,
    body: str,
    kind: str,
    scope: str,
) -> MemoryEntry:
    name = slugify(title)
    if not name:
        raise ValueError("Title produced an empty slug; pick a different title.")
    if kind not in ("reference", "directive"):
        raise ValueError("Use kind='reference' or 'directive'.")
    if scope not in ("repo", "user") or (scope == "user" and kind != "directive"):
        raise ValueError("Use scope='repo', or scope='user' for directives only.")
    if kind == "reference" and repo_name.casefold() == "pymc-labs/daimon-memory":
        raise ValueError(
            "This repository keeps reviewed facts in Git; save references through a PR."
        )
    now = datetime.now(UTC)
    return MemoryEntry(
        tenant_id=tenant_id,
        repo_id=repo_id,
        name=name,
        title=title.strip(),
        body=body.strip(),
        kind=kind,
        scope=scope,
        created_by_account_id=account_id,
        created_at=now,
        updated_at=now,
    )


class MemoryPointer(BaseModel):
    name: str
    title: str
    kind: Literal["reference", "directive"]
    scope: Literal["repo", "user"]
    body: str


def render_memory(repo: str, entries: list[MemoryPointer]) -> str:
    """Bound UTF-8 prompt bytes, including markup, regardless of imported entry sizes.

    Directives precede reference pointers. Overflow is explicitly reported and
    retrieved with tools instead of silently turning an oversized rule into a
    different instruction. The SQL projection bounds rows and directive bodies.
    """
    header = (
        f"<repo_memory_state repo={quoteattr(repo)}>\n"
        "Current saved state supersedes earlier repository-memory blocks. "
        "Follow directives for this repo; personal directives apply only to the speaker. "
        "Use repo_memory_view for reference bodies.\n"
    )
    footer = "\n</repo_memory_state>\n"
    notice = "\n[Memory omitted or shortened to fit; call repo_memory_list and repo_memory_view.]"
    remaining = MEMORY_BLOCK_BYTES - len((header + footer + notice).encode())
    lines: list[str] = []
    omitted = len(entries) >= MEMORY_QUERY_ROWS or any(e.name == "_more" for e in entries)
    for entry in entries:
        # Bound work even for a huge imported title before XML expansion.
        title = entry.title[:1000]
        line = (
            f"<memory name={quoteattr(entry.name)} title={quoteattr(title)} "
            f"kind={quoteattr(entry.kind)} scope={quoteattr(entry.scope)}>"
        )
        if entry.kind == "directive":
            if len(entry.body) > MEMORY_BODY_CHARS:
                line += "[Read this directive in full with repo_memory_view before applying.]"
            else:
                line += escape(entry.body)
        line += "</memory>\n"
        size = len(line.encode())
        if size > remaining:
            omitted = True
            continue
        lines.append(line)
        remaining -= size
        omitted |= len(entry.title) > len(title) or (
            entry.kind == "directive" and len(entry.body) > MEMORY_BODY_CHARS
        )
    return header + "".join(lines) + (notice if omitted else "") + footer


async def turn_memory_prefix(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    allowed: bool,
    mapping_id: uuid.UUID | None = None,
    session_id: str | None = None,
) -> str:
    if not allowed:
        return ""
    async with session_factory() as session:
        repo, rows, previous_sha256 = await turn_entries(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            mapping_id=mapping_id,
            session_id=session_id,
        )
    entries = [
        MemoryPointer.model_validate(dict(name=n, title=t, kind=k, scope=s, body=b))
        for n, t, k, s, b in rows
    ]
    # Empty state is explicit, so a deletion clears a previously injected rule.
    rendered = (
        render_memory(repo, entries)
        if repo
        else (
            "<repo_memory_state>No authorized working repo; previous repo-memory blocks "
            "no longer apply.</repo_memory_state>\n"
        )
    )

    return "" if hashlib.sha256(rendered.encode()).hexdigest() == previous_sha256 else rendered


async def record_memory_prefix(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    mapping_id: uuid.UUID | None,
    session_id: str,
    prefix: str,
) -> None:
    if not prefix or mapping_id is None:
        return
    async with session_factory.begin() as session:
        await record_turn_state(
            session,
            tenant_id=tenant_id,
            mapping_id=mapping_id,
            session_id=session_id,
            sha256=hashlib.sha256(prefix.encode()).hexdigest(),
        )

"""Skills served from inside Daimon, kept by the same mirror sync as a skills server's.

A source answers the two questions `Upstream` asks (the listing entry of a mirror, and its
files) and adds `listing`, so a person can pick what to mirror. The node's HTTP transport
stays as it is; a source runs in process: a repository (`server_skills.repo`) or a local
store the data package supplies (`LocalSources`, filled at startup, empty by default).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from daimon.core.agent_editing import AgentEditDeps
from daimon.core.mcp_skills.manifest import SkillEntry
from daimon.core.mux_backend import resource_scope
from daimon.core.server_skills.anthropic import ManagedAgentsTarget
from daimon.core.server_skills.sync import SyncDeps, Upstream
from daimon.core.skill_library_files import skill_lock
from daimon.core.stores.domain import ServerSkillRow
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "LocalSources",
    "SkillSource",
    "SystemWriter",
    "source_deps",
]


class SkillSource(Upstream, Protocol):
    """An in-process place skills come from: the mirror sync asks it like a server."""

    async def listing(self, *, tenant_id: uuid.UUID) -> Sequence[SkillEntry]:
        """Every skill the source offers the workspace, for choosing what to mirror."""
        ...


type LocalSources = Mapping[str, SkillSource]
"""Local stores by the `server` name their mirrors carry (the data package's fills this)."""


@dataclass(frozen=True)
class SystemWriter:
    """The workspace itself as the author of a library skill, when no person adds it."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID | None = None

    def scope(self) -> Scope:
        return resource_scope(tenant_id=str(self.tenant_id))


def source_deps(
    *,
    server: str,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    edit: AgentEditDeps,
    now: Callable[[], datetime],
) -> SyncDeps:
    """Sync dependencies for mirrors of one in-process source, deployed to Managed Agents.

    No person is asked for the files: the source decides what the workspace may read, so a
    mirror needs no adder.
    """

    def lock(mirror: ServerSkillRow) -> AbstractAsyncContextManager[None]:
        return skill_lock(edit, tenant_id=mirror.tenant_id, skill_id=mirror.skill_id)

    return SyncDeps(
        server=server,
        sessionmaker=sessionmaker,
        upstream=lambda _mirror: upstream,
        targets=(ManagedAgentsTarget(edit.client),),
        lock=lock,
        now=now,
        asked_as_adder=False,
    )

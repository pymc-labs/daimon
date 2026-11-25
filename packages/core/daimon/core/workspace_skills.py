"""The library's skills as kept locally: recorded on publish, listed per caller by scope.

A skill's scope is a name, resolved to a caller's access by whoever owns scopes (the data
package). `SkillScopes` is that seam. An unscoped skill is visible to everyone who may use
the library; a scoped one only to callers the checker names, so without a checker a scoped
skill is hidden from all.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Collection, Mapping
from typing import Final, Protocol

import structlog
from daimon.core import ports
from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import ChangeRecorder
from daimon.core.config import Settings
from daimon.core.server_skills.gate import manifest_digest
from daimon.core.skills.ingest import SkillBundle, retained_archive_files
from daimon.core.stores import workspace_skills as store
from daimon.core.stores.domain import WorkspaceSkillRow
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "NoScopes",
    "SkillScopes",
    "files_digest",
    "list_visible_skills",
    "record_bundle",
    "record_published",
    "register_skill_scopes",
    "set_skill_scope",
    "skill_scopes",
]

log = structlog.get_logger(__name__)


class SkillScopes(Protocol):
    """Which skill scopes a caller may read; the data package's scopes answer it."""

    async def visible(
        self, *, tenant_id: uuid.UUID, account_id: uuid.UUID | None, scopes: Collection[str]
    ) -> frozenset[str]:
        """The members of `scopes` the caller may read. `account_id` None: no person known."""
        ...


class NoScopes:
    """No scopes are configured: every scoped skill stays hidden."""

    async def visible(
        self, *, tenant_id: uuid.UUID, account_id: uuid.UUID | None, scopes: Collection[str]
    ) -> frozenset[str]:
        return frozenset()


_FACTORY: Final[list[Callable[[Settings], SkillScopes]]] = []


def register_skill_scopes(factory: Callable[[Settings], SkillScopes]) -> None:
    """Fill the scope seam: the installed data package says which scopes a caller holds."""
    _FACTORY[:] = [factory]


def skill_scopes(settings: Settings) -> SkillScopes:
    """The registered scope checker, or `NoScopes` where no package fills the seam."""
    ports.load()
    return _FACTORY[0](settings) if _FACTORY else NoScopes()


def files_digest(files: Mapping[str, bytes]) -> str:
    """Names a version's files: sha256 over the sorted (path, sha256) pairs."""
    return manifest_digest({path: hashlib.sha256(body).hexdigest() for path, body in files.items()})


async def record_published(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    skill_id: str,
    name: str,
    version: str,
    files: Mapping[str, bytes],
) -> None:
    """Keep what was just published at the provider.

    The provider already holds the version, so a database failure here is logged and the
    publish stands; the next publish of the skill records its own version.
    """
    try:
        async with session_factory() as session, session.begin():
            await store.record_version(
                session,
                tenant_id=tenant_id,
                skill_id=skill_id,
                name=name,
                version=version,
                digest=files_digest(files),
                files=files,
            )
    except SQLAlchemyError:
        log.warning(
            "workspace_skill.record_failed", skill_id=skill_id, version=version, exc_info=True
        )


async def record_bundle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    skill_id: str,
    name: str,
    version: str,
    bundle: SkillBundle,
) -> None:
    """`record_published` for the files of the bundle that was just published."""
    await record_published(
        session_factory,
        tenant_id=tenant_id,
        skill_id=skill_id,
        name=name,
        version=version,
        files=retained_archive_files(bundle.zip_bytes),
    )


async def list_visible_skills(
    session: AsyncSession,
    scopes: SkillScopes,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
) -> list[WorkspaceSkillRow]:
    """The skills a caller may read: unscoped ones, and those whose scope the checker allows."""
    rows = await store.list_skills(session, tenant_id=tenant_id)
    named = {row.scope for row in rows if row.scope is not None}
    allowed = (
        await scopes.visible(tenant_id=tenant_id, account_id=account_id, scopes=named)
        if named
        else frozenset[str]()
    )
    return [row for row in rows if row.scope is None or row.scope in allowed]


async def set_skill_scope(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    skill_id: str,
    scope: str | None,
    audited: ChangeRecorder | None = None,
) -> None:
    """Set who may read a skill (None: everyone who may use the library), for an admin."""
    require_admin(actor, "The skill library")
    async with deps.session_factory() as session, session.begin():
        if not await store.set_scope(
            session, tenant_id=actor.tenant_id, skill_id=skill_id, scope=scope
        ):
            raise AgentEditError("No such skill.", kind="not_found")
        if audited is not None:
            await audited(session, skill_id)

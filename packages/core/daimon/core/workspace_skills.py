"""The library's skills as kept locally: recorded on publish, listed per caller by scope.

A skill's scope is a data scope name. An unscoped skill is visible to everyone who may use
the library; a scoped one only to callers who hold the scope under the data audience rule
(`daimon.adapters.mcp.data_access`), so a caller who may read no data sees unscoped ones.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Collection, Mapping

import structlog
from daimon.core.capability_tools import LIBRARY_SKILLS
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.server_skills import gate  # a module: `gate` imports back through `defaults`
from daimon.core.skills.ingest import retained_archive_files
from daimon.core.stores import workspace_skills as store
from daimon.core.stores.domain import WorkspaceSkillRow
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "ScopeUnsaved",
    "files_digest",
    "may_read",
    "record_published",
    "record_zip",
    "scoped_skill_note",
]


class ScopeUnsaved(Exception):
    """A new scoped skill could not be recorded, so its scope is not kept."""


log = structlog.get_logger(__name__)


def files_digest(files: Mapping[str, bytes]) -> str:
    """Names a version's files: sha256 over the sorted (path, sha256) pairs."""
    return gate.manifest_digest({p: hashlib.sha256(body).hexdigest() for p, body in files.items()})


async def _served_here(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> bool:
    """Whether the workspace serves its library (`library_skills`), so versions are kept."""
    async with session_factory() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        return False
    place = FeaturePlace(tenant_id, tenant.platform)
    found = await resolve_features(session_factory, None, place, keys=(LIBRARY_SKILLS,))
    return found.on(LIBRARY_SKILLS)


async def record_published(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    skill_id: str,
    name: str,
    version: str | None,
    files: Mapping[str, bytes],
    scope: str | None = None,
) -> None:
    """Keep what was just published at the provider, where `library_skills` is On; `scope`
    is a new skill's scope, kept wherever it is set.

    The provider already holds the version, so a failure here is logged and the publish
    stands; the next publish or the backfill records it. A new skill given a scope is the
    exception: unrecorded, it would later be recorded unscoped, so the failure is raised.
    """
    if not version or version == "latest":
        return
    try:
        if scope is None and not await _served_here(session_factory, tenant_id):
            return
        async with session_factory() as session, session.begin():
            await store.record_version(
                session,
                tenant_id=tenant_id,
                skill_id=skill_id,
                name=name,
                version=version,
                digest=files_digest(files),
                files=files,
                scope=scope,
            )
    except Exception as exc:  # named boundary: recording never fails a publish
        log.warning(
            "workspace_skill.record_failed", skill_id=skill_id, version=version, exc_info=True
        )
        if scope is not None:
            raise ScopeUnsaved from exc


async def record_zip(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    skill_id: str,
    name: str,
    version: str | None,
    zip_bytes: bytes,
    scope: str | None = None,
) -> None:
    """`record_published` for the archive that was just published."""
    try:
        files = retained_archive_files(zip_bytes)
    except Exception as exc:  # named boundary: recording never fails a publish
        log.warning("workspace_skill.record_unreadable", skill_id=skill_id, exc_info=True)
        if scope is not None:
            raise ScopeUnsaved from exc
        return
    await record_published(
        session_factory,
        tenant_id=tenant_id,
        skill_id=skill_id,
        name=name,
        version=version,
        files=files,
        scope=scope,
    )


def may_read(row: WorkspaceSkillRow, scopes: Collection[str]) -> bool:
    """Whether a caller holding `scopes` may read the skill: unscoped, or one of `scopes`."""
    return row.scope is None or row.scope in scopes


def scoped_skill_note(scopes: Collection[str]) -> str:
    """Said where a skill limited to a data scope is attached."""
    named = ", ".join(sorted(set(scopes)))
    return (
        f"A skill scoped to {named} loads only in DMs, the web chat and Discord rooms "
        "where everyone present holds that scope."
    )

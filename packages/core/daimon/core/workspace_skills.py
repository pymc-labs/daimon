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
from daimon.core.server_skills.gate import manifest_digest
from daimon.core.skills.ingest import retained_archive_files
from daimon.core.stores import workspace_skills as store
from daimon.core.stores.domain import WorkspaceSkillRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["ScopeUnsaved", "files_digest", "may_read", "record_published", "record_zip"]


class ScopeUnsaved(Exception):
    """A new scoped skill could not be recorded, so its scope is not kept."""


log = structlog.get_logger(__name__)


def files_digest(files: Mapping[str, bytes]) -> str:
    """Names a version's files: sha256 over the sorted (path, sha256) pairs."""
    return manifest_digest({path: hashlib.sha256(body).hexdigest() for path, body in files.items()})


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
    """Keep what was just published at the provider; `scope` is a new skill's scope.

    The provider already holds the version, so a failure here is logged and the publish
    stands; the next publish or the backfill records it. A new skill given a scope is the
    exception: unrecorded, it would later be recorded unscoped, so the failure is raised.
    """
    if not version or version == "latest":
        return
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

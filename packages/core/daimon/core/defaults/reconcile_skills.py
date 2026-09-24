"""Per-resource reconciliation for skills.

Three-state decision tree: find on MA by display_title → create (no match),
skip (match, content unchanged), or upload a new version (match, content
changed).

MA offers nothing to compare content against. Skills carry no metadata field,
`latest_version` is an opaque monotonic counter rather than a content hash, no
endpoint returns a version's file bytes, and the API rejects any zip whose
top-level directory differs from the SKILL.md `name:` — so the folder name
cannot smuggle a digest either. The fingerprint therefore lives in our own
`seeded_skills` table.

Updates go to a new VERSION of the existing skill rather than a
delete-and-recreate. Agents pin `version="latest"`, so they pick the new
content up with no re-attach, and the skill id never changes — deleting a
skill an agent references 400s every one of that agent's turns.

Duplicate skills sharing a display_title (a race-prone artifact, observed in
production with two cli-auth skills created 54ms apart) are deleted inline,
mirroring the env/agent reconcilers — newest by created_at is canonical, older
duplicates go through `delete_skill_and_versions`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.loader import load_skill_spec
from daimon.core.defaults.ma_index import find_skills_by_display_title
from daimon.core.defaults.metadata import strip_tenant_prefix, tenant_scoped_display_title
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.errors import DefaultsError
from daimon.core.ma import delete_skill_and_versions
from daimon.core.skill_zip import build_skill_zip
from daimon.core.stores.seeded_skills import load_seeded_skill, record_seeded_skill
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


async def reconcile_skill(
    client: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    skill_dir: Path,
    *,
    tenant_id: uuid.UUID,
    dry_run: bool,
    db_session: AsyncSession | None = None,
) -> ResourceOutcome:
    spec, _body = load_skill_spec(skill_dir)
    display_title = tenant_scoped_display_title(tenant_id=tenant_id, name=spec.name)
    # on_truncation="raise": seed is a create context — making decisions on a truncated
    # view is unsafe. A full page surfaces through _run_per_resource as FAILED.
    matches = await find_skills_by_display_title(client, display_title, on_truncation="raise")
    ma_match = matches[0] if matches else None
    duplicates = matches[1:] if len(matches) > 1 else []

    if duplicates and not dry_run:
        for dup in duplicates:
            # Namespace belt: the dedup lookup was by canonical title so a
            # duplicate MUST carry this tenant's prefix. A None here is a logic error —
            # raise instead of deleting a skill we do not own.
            dup_title = dup.display_title or ""
            if strip_tenant_prefix(tenant_id=tenant_id, display_title=dup_title) is None:
                raise DefaultsError(
                    f"reconcile_skill: dedup found skill {dup.id!r} with display_title "
                    f"{dup.display_title!r} that does not carry tenant prefix for "
                    f"{str(tenant_id)[:8]}; refusing to delete a skill we do not own"
                )
            _log.info(
                "reconcile.delete_duplicate",
                kind="skill",
                name=spec.name,
                canonical_id=ma_match.id if ma_match else None,
                duplicate_id=dup.id,
            )
            await delete_skill_and_versions(client, dup.id)

    pkg = build_skill_zip(skill_dir)
    try:
        if ma_match is not None:
            if db_session is None:
                async with session_factory() as seed_session:
                    recorded = await load_seeded_skill(
                        seed_session, tenant_id=tenant_id, name=spec.name
                    )
            else:
                async with db_session.begin_nested():
                    recorded = await load_seeded_skill(
                        db_session, tenant_id=tenant_id, name=spec.name
                    )
            # A recorded hash for a DIFFERENT skill id describes content we can
            # no longer vouch for on this one, so it does not count as a match.
            if (
                recorded is not None
                and recorded.anthropic_id == ma_match.id
                and recorded.content_hash == pkg.content_hash
            ):
                return ResourceOutcome(
                    kind="skill",
                    name=spec.name,
                    action=Action.SKIPPED,
                    anthropic_id=None if dry_run else ma_match.id,
                )
            if dry_run:
                return ResourceOutcome(kind="skill", name=spec.name, action=Action.UPDATED)
            _log.info(
                "reconcile.skill_content_changed",
                name=spec.name,
                skill_id=ma_match.id,
                had_fingerprint=recorded is not None,
            )
            with pkg.path.open("rb") as fh:
                await client.beta.skills.versions.create(
                    ma_match.id, files=[("SKILL.zip", fh, "application/zip")]
                )
            if db_session is None:
                async with session_factory() as seed_session:
                    await record_seeded_skill(
                        seed_session,
                        tenant_id=tenant_id,
                        name=spec.name,
                        content_hash=pkg.content_hash,
                        anthropic_id=ma_match.id,
                    )
                    await seed_session.commit()
            else:
                async with db_session.begin_nested():
                    await record_seeded_skill(
                        db_session,
                        tenant_id=tenant_id,
                        name=spec.name,
                        content_hash=pkg.content_hash,
                        anthropic_id=ma_match.id,
                    )
            return ResourceOutcome(
                kind="skill", name=spec.name, action=Action.UPDATED, anthropic_id=ma_match.id
            )

        if dry_run:
            return ResourceOutcome(kind="skill", name=spec.name, action=Action.CREATED)
        with pkg.path.open("rb") as fh:
            created = await client.beta.skills.create(
                display_title=display_title, files=[("SKILL.zip", fh, "application/zip")]
            )
        if db_session is None:
            async with session_factory() as seed_session:
                await record_seeded_skill(
                    seed_session,
                    tenant_id=tenant_id,
                    name=spec.name,
                    content_hash=pkg.content_hash,
                    anthropic_id=created.id,
                )
                await seed_session.commit()
        else:
            async with db_session.begin_nested():
                await record_seeded_skill(
                    db_session,
                    tenant_id=tenant_id,
                    name=spec.name,
                    content_hash=pkg.content_hash,
                    anthropic_id=created.id,
                )
    finally:
        pkg.path.unlink(missing_ok=True)
    return ResourceOutcome(
        kind="skill", name=spec.name, action=Action.CREATED, anthropic_id=created.id
    )

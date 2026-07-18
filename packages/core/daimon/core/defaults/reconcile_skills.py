"""Per-resource reconciliation for skills.

Two-state decision tree: find on MA by display_title → skip (found) or create
(not found). Skills are org-wide on MA, have no metadata field, and the
`latest_version` column is an opaque monotonic counter rather than a content
hash, so there is no carrier to drive idempotent updates from. `defaults apply`
treats skills as immutable: when an MA match exists, it is adopted as-is and no
new version is uploaded. Content updates flow through `daimon skills sync`
explicitly. See L13 smoke-matrix finding + skills_idempotency_carrier probe.

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

_log = structlog.get_logger(__name__)


async def reconcile_skill(
    client: AsyncAnthropic,
    skill_dir: Path,
    *,
    tenant_id: uuid.UUID,
    dry_run: bool,
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

    if ma_match is not None:
        return ResourceOutcome(
            kind="skill",
            name=spec.name,
            action=Action.SKIPPED,
            anthropic_id=None if dry_run else ma_match.id,
        )

    if dry_run:
        return ResourceOutcome(kind="skill", name=spec.name, action=Action.CREATED)
    pkg = build_skill_zip(skill_dir)
    try:
        with pkg.path.open("rb") as fh:
            created = await client.beta.skills.create(
                display_title=display_title, files=[("SKILL.zip", fh, "application/zip")]
            )
    finally:
        pkg.path.unlink(missing_ok=True)
    return ResourceOutcome(
        kind="skill", name=spec.name, action=Action.CREATED, anthropic_id=created.id
    )

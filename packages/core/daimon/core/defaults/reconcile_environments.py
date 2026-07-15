"""Per-resource reconciliation for environments.

Two-state decision tree: find on MA by daimon tag → update (found) or create
(not found). No spec hash, no DB row — MA is the source of truth.
"""

from __future__ import annotations

import uuid
from typing import cast

import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import find_environments_by_daimon_tag
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_SPEC_HASH,
    build_metadata,
    compute_spec_fingerprint,
)
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.specs import EnvironmentSpec

_log = structlog.get_logger(__name__)


async def reconcile_environment(
    client: AsyncAnthropic,
    spec: EnvironmentSpec,
    *,
    tenant_id: uuid.UUID,
    dry_run: bool,
) -> ResourceOutcome:
    matches = await find_environments_by_daimon_tag(client, tenant_id=tenant_id, name=spec.name)
    ma_match = matches[0] if matches else None
    duplicates = matches[1:] if len(matches) > 1 else []
    if duplicates and not dry_run:
        for dup in duplicates:
            _log.info(
                "reconcile.archive_duplicate",
                kind="environment",
                name=spec.name,
                canonical_id=ma_match.id if ma_match else None,
                duplicate_id=dup.id,
            )
            await client.beta.environments.archive(dup.id)
    spec_dump = spec.model_dump(exclude_none=True, mode="json")
    spec_hash = compute_spec_fingerprint({"spec": spec_dump})
    metadata = build_metadata(
        tenant_id=tenant_id, name=spec.name, managed=True, spec_hash=spec_hash
    )

    if ma_match is not None:
        existing_hash = ma_match.metadata.get(MA_METADATA_KEY_SPEC_HASH)
        if existing_hash == spec_hash:
            return ResourceOutcome(
                kind="environment",
                name=spec.name,
                action=Action.SKIPPED,
                anthropic_id=ma_match.id,
            )
        if dry_run:
            return ResourceOutcome(kind="environment", name=spec.name, action=Action.UPDATED)
        updated = await client.beta.environments.update(
            ma_match.id,
            **spec.model_dump(exclude_none=True, exclude={"name"}),
            metadata=cast("dict[str, str | None]", metadata),
        )
        return ResourceOutcome(
            kind="environment", name=spec.name, action=Action.UPDATED, anthropic_id=updated.id
        )

    if dry_run:
        return ResourceOutcome(kind="environment", name=spec.name, action=Action.CREATED)
    created = await client.beta.environments.create(
        **spec.model_dump(exclude_none=True),
        metadata=metadata,
    )
    return ResourceOutcome(
        kind="environment", name=spec.name, action=Action.CREATED, anthropic_id=created.id
    )

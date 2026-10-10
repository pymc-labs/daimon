"""Refresh credential guidance on existing user agents during tenant reconcile."""

from __future__ import annotations

import uuid

import structlog
from anthropic import AsyncAnthropic
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_ISOLATED,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
)

_log = structlog.get_logger(__name__)


async def backfill_credential_guidance(
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    seeded_agent_ids: set[str],
    seeded_agent_names: set[str],
    seeded_account_id: uuid.UUID | None,
) -> None:
    """Update only ``system`` on live, non-isolated user agents; never fail boot."""
    updated = skipped = failed = 0
    try:
        agents = await list_agents_by_tenant(client, tenant_id=tenant_id)
    except Exception:
        _log.exception("agents.guidance_backfill_list_failed", tenant_id=str(tenant_id))
        _log.info(
            "agents.guidance_backfill_done",
            tenant_id=str(tenant_id),
            updated=updated,
            skipped=skipped,
            failed=1,
        )
        return

    for agent in agents:
        if (
            agent.id in seeded_agent_ids
            or (
                agent.metadata.get(MA_METADATA_KEY_NAME) in seeded_agent_names
                and agent.metadata.get(MA_METADATA_KEY_ACCOUNT)
                in (None, str(seeded_account_id) if seeded_account_id is not None else None)
            )
            or agent.archived_at is not None
            or agent.metadata.get(MA_METADATA_KEY_ISOLATED) == "true"
            or agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
        ):
            skipped += 1
            continue
        system = apply_credential_guidance(agent.system or "")
        if system == agent.system:
            skipped += 1
            continue
        try:
            # A conflict is left for the next boot, so this pass never overwrites
            # a concurrent edit using stale prompt text.
            await client.beta.agents.update(agent.id, version=agent.version, system=system)
        except Exception:
            failed += 1
            _log.exception(
                "agents.guidance_backfill_failed", agent_id=agent.id, tenant_id=str(tenant_id)
            )
            continue
        updated += 1
        _log.info("agents.guidance_backfilled", agent_id=agent.id, tenant_id=str(tenant_id))

    _log.info(
        "agents.guidance_backfill_done",
        tenant_id=str(tenant_id),
        updated=updated,
        skipped=skipped,
        failed=failed,
    )

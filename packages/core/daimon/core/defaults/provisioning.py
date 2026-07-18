"""DB-only idempotent tenant provisioning saga.

Creates Tenant + Account in one transaction, keyed on the deterministic
`derive_tenant_uuid(platform, workspace_id)`. Idempotent via ON CONFLICT DO NOTHING
+ re-SELECT per resource.

This module does NOT call apply_defaults / MA reconcile — that I/O is the
on_guild_join wiring. This keeps idempotency unit-testable in isolation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import structlog
from anthropic import AsyncAnthropic
from daimon.core._models import Account, Tenant
from daimon.core.defaults._reconcile import _reconcile_core  # pyright: ignore[reportPrivateUsage]
from daimon.core.defaults.report import ApplyReport
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import Platform
from daimon.core.stores.slack_bot_tokens import delete_slack_bot_token
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

# Frozen namespace UUID for the guild-owned account identity derivation.
# Generated once via `uuid.uuid4()` on 2026-05-29.
# DO NOT CHANGE — a re-key invalidates every accounts row keyed by uuid5
# under the old namespace (that's a migration, not a code change).
_DAIMON_ACCOUNT_NS = uuid.UUID("7e2d4f1a-8b3c-4e6d-9a0f-1c2e5d7b8f3a")


def derive_guild_account_uuid(tenant_id: uuid.UUID) -> uuid.UUID:
    """Deterministic guild-owned account id for a tenant; same input always yields the same UUID.

    UUID5 derived from (tenant_id) under the frozen _DAIMON_ACCOUNT_NS namespace.
    One account per tenant — the guild bootstrap account that owns seeded agents and
    panel-created agents. Idempotent across DB resets and processes.

    Do NOT change _DAIMON_ACCOUNT_NS — a re-key invalidates every accounts row
    keyed by uuid5 under the old namespace (that's a migration, not a code change).
    """
    return uuid.uuid5(_DAIMON_ACCOUNT_NS, f"account:{tenant_id}")


# Keep the private name as a thin alias so existing callers (provisioning.py, CLI
# agents.py, tests) do not churn. New code should use derive_guild_account_uuid.
_derive_account_uuid = derive_guild_account_uuid


class ProvisionResult(BaseModel):
    """Pydantic projection returned by provision_tenant.

    Never contains ORM instances — all fields are primitives.
    """

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    platform: str
    external_id: str


async def provision_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    platform: Platform,
    workspace_id: str,
    signup_credit: Decimal = Decimal("0"),
) -> ProvisionResult:
    """Create Tenant + Account in one idempotent transaction (DB only).

    Keyed on the deterministic derive_tenant_uuid(platform, workspace_id). Idempotent:
    ON CONFLICT DO NOTHING + re-SELECT per resource. Does NOT call apply_defaults / MA
    reconcile — that I/O is the on_guild_join wiring.

    `signup_credit`: if > 0, seeds one 'trial' ledger row atomically in the same
    transaction. Idempotent on (trial:{tenant_id}) — re-provisioning never double-credits.
    signup_credit=0 (the default) inserts no ledger row.

    NOTE: kwarg name `workspace_id` is intentional — migration 0014 calls
    derive_tenant_uuid(workspace_id=...) and this signature must match (Critical Warning 4).
    """
    tenant_id = derive_tenant_uuid(platform=platform, workspace_id=workspace_id)
    account_id = _derive_account_uuid(tenant_id)

    async with session_factory() as s, s.begin():
        # Step 1: Tenant upsert — explicit id (the derived uuid5) + platform/external_id.
        await s.execute(
            pg_insert(Tenant)
            .values(id=tenant_id, platform=platform, external_id=workspace_id)
            .on_conflict_do_nothing(index_elements=[Tenant.id])
        )
        await s.flush()

        # Step 2: Account upsert — derived id makes ON CONFLICT DO NOTHING work
        # (Account has no unique constraint beyond its PK).
        await s.execute(
            pg_insert(Account)
            .values(id=account_id, tenant_id=tenant_id)
            .on_conflict_do_nothing(index_elements=[Account.id])
        )
        await s.flush()

        # Re-SELECT tenant_id to confirm the row exists (handles both new and existing).
        confirmed_tenant_id = (
            await s.execute(select(Tenant.id).where(Tenant.id == tenant_id))
        ).scalar_one()

        # Step 3: seed trial credit. Idempotent — key on the tenant so re-runs no-op.
        if signup_credit > 0:
            await tenant_ledger.insert_entry(
                s,
                tenant_id=tenant_id,
                delta_usd=signup_credit,
                reason="trial",
                idempotency_key=f"trial:{tenant_id}",
            )

    return ProvisionResult(
        tenant_id=confirmed_tenant_id,
        account_id=account_id,
        platform=platform,
        external_id=workspace_id,
    )


async def reconcile_tenant_defaults(
    client: AsyncAnthropic,
    defaults_root: Path,
    *,
    tenant_id: uuid.UUID,
    public_url: str | None = None,
) -> ApplyReport:
    """Tenant-scoped MA reconcile orchestrator.

    Takes an EXPLICIT `tenant_id` instead of running the CLI bootstrap tenant lookup.
    Background-callable (on_guild_join task invokes it). Delegates to `_reconcile_core`
    (shared with `apply_defaults`), whose per-resource error isolation records a single
    resource failure as FAILED without raising. Not dry-run-capable: hardcodes
    `dry_run=False`.

    The provision_status flip (pending -> ready/failed) is the Discord adapter's
    responsibility — this function does NOT flip it.
    """
    return await _reconcile_core(
        client,
        defaults_root,
        tenant_id=tenant_id,
        account_id=_derive_account_uuid(tenant_id),
        dry_run=False,
        run_preflight=True,
        public_url=public_url,
    )


async def archive_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    now: datetime,
) -> None:
    """Soft-archive a tenant by setting Tenant.archived_at = now.

    Idempotent and no-op-safe: if the tenant row does not exist, the UPDATE
    matches 0 rows and returns without raising.

    Takes `now` as an explicit parameter — no datetime.now() inside logic
    (architecture guideline: pure-logic functions take clocks as parameters).
    """
    async with session_factory() as s, s.begin():
        await s.execute(update(Tenant).where(Tenant.id == tenant_id).values(archived_at=now))


async def teardown_slack_install(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    team_id: str,
    now: datetime,
) -> None:
    """Soft-archive the Slack tenant and delete its bot-token row.

    1. Derives the tenant_id deterministically from (platform="slack", team_id).
    2. Calls archive_tenant to set Tenant.archived_at = now (no-op if absent).
    3. Deletes the slack_bot_tokens row via delete_slack_bot_token (idempotent —
       returns 0 rowcount when row is already absent, never raises).

    Token-existence is the liveness signal: after teardown, any
    event handler that reads the token row will see None and drop the event.
    """
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    await archive_tenant(session_factory, tenant_id=tenant_id, now=now)
    async with session_factory() as s, s.begin():
        await delete_slack_bot_token(s, team_id=team_id)

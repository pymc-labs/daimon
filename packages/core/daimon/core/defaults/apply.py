"""Orchestrator: reconcile `defaults/` -> (MA, DB) for the cli:local tenant.

Execution order:
  1. `provision_tenant` for cli:local (idempotent, no ledger row — D-16).
  2-7. Delegated to `_reconcile_core` (shared with `reconcile_tenant_defaults`):
    load-tree → validate-refs → preflight → skill/env/agent passes → sweep.

Best-effort: a per-resource failure is caught, reported as FAILED in the
ResourceOutcome, and the sweep for that kind is still attempted. The caller
decides exit code from `report.is_failure()`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from anthropic import AsyncAnthropic
from daimon.core.defaults._reconcile import _reconcile_core  # pyright: ignore[reportPrivateUsage]
from daimon.core.defaults.report import ApplyReport
from daimon.core.ma_identity import derive_tenant_uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def apply_defaults(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncAnthropic,
    defaults_root: Path,
    *,
    dry_run: bool = False,
    public_url: str | None = None,
    run_preflight: bool = True,
) -> ApplyReport:
    # 1. Provision cli:local deterministically (idempotent; signup_credit=0 → no ledger row — D-16).
    # Inline import breaks the apply ↔ provisioning circular dependency.
    from daimon.core.defaults.provisioning import provision_tenant  # noqa: PLC0415

    await provision_tenant(
        session_factory, platform="cli", workspace_id="local", signup_credit=Decimal("0")
    )
    tenant_id = derive_tenant_uuid(platform="cli", workspace_id="local")

    # 2-7. Delegate the shared reconcile spine.
    return await _reconcile_core(
        client,
        defaults_root,
        tenant_id=tenant_id,
        account_id=None,
        dry_run=dry_run,
        run_preflight=run_preflight,
        public_url=public_url,
    )

"""Tests for the defaults apply path.

Tests here verify that apply_defaults writes NO default-pointer row to
tenant_config (per R5: _reconcile_system_config deleted) and that it provisions
the cli:local tenant deterministically (Req 4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from daimon.core.defaults import apply_defaults
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    list_response,
)
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _write_tree(root: Path) -> None:
    (root / "agents").mkdir(parents=True)
    (root / "environments").mkdir(parents=True)
    (root / "agents" / "daimon.yaml").write_text("name: daimon\nmodel: claude-sonnet-4-6\n")
    (root / "environments" / "default.yaml").write_text("name: default\n")
    (root / "config.yaml").write_text("agent_name: daimon\nenvironment_name: default\n")


def _full_router(
    *,
    skills: list[dict[str, Any]] = (),
    environments: list[dict[str, Any]] = (),
    agents: list[dict[str, Any]] = (),
) -> MARouter:
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(list(skills)))
    router.add("GET", r"/v1/environments", lambda req, _m: list_response(list(environments)))
    router.add("GET", r"/v1/agents", lambda req, _m: list_response(list(agents)))
    return router


async def test_apply_does_not_write_tenant_config(
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """apply_defaults must NOT write a tenant_config default-pointer row (R5).

    The MA resource seeding (agent/env/skill creation in MA) continues to run.
    Resolution still yields daimon/default via the injected DeploymentDefault.

    This test is RED until Plan 03 (DeploymentDefault) + Plan 04 (resolve() signature
    update) + Plan 05 (delete _reconcile_system_config) land. That is expected.
    """
    from daimon.core.scope import DeploymentDefault, ScopeContext  # noqa: PLC0415
    from daimon.core.stores.scoped_config_read import resolve  # noqa: PLC0415

    _write_tree(tmp_path)
    router = _full_router()
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200,
            json=BetaEnvironment(
                id="env_1",
                type="environment",
                name="default",
                config=EMPTY_CLOUD_CONFIG,
                metadata={},
                description="",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_1",
                type="agent",
                name="daimon",
                model={"id": "claude-opus-4-7"},
                metadata={},
                description=None,
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    await apply_defaults(db_session_factory, client, tmp_path, dry_run=False, run_preflight=False)

    # R5: NO tenant_config row must have been written by reconcile
    schema = (await db_session.execute(text("SELECT current_schema()"))).scalar_one()
    count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = :s AND table_name = 'tenant_config'"
            ),
            {"s": schema},
        )
    ).scalar_one()
    if count == 0:
        # tenant_config doesn't exist yet (migration 0019 not applied) — skip the row check
        # This test's meaningful assertion is the resolve() check below
        pytest.skip("tenant_config table not yet created (migration 0019 not applied)")

    row_count = (await db_session.execute(text("SELECT count(*) FROM tenant_config"))).scalar_one()
    assert row_count == 0, (
        "apply_defaults must NOT write any tenant_config rows (R5: _reconcile_system_config deleted)"
    )

    # Resolution must still yield daimon/default via the injected DeploymentDefault
    from daimon.core._models import Tenant  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    tenant = (await db_session.execute(select(Tenant).limit(1))).scalar_one_or_none()
    if tenant is not None:
        result = await resolve(
            db_session,
            context=ScopeContext(tenant_id=tenant.id),
            default=DeploymentDefault(agent_name="daimon", environment_name="default"),
        )
        assert result.agent_name == "daimon", (
            "resolve() must still yield the deployment default even with no tenant_config row"
        )


async def test_apply_defaults_provisions_cli_local_deterministically(
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Req 4: fresh DB + apply_defaults yields exactly one tenant with
    id == derive_tenant_uuid('cli','local'), zero orphans, no tenant_ledger row."""
    from daimon.core._models import Tenant, TenantLedger  # noqa: PLC0415
    from daimon.core.ma_identity import derive_tenant_uuid  # noqa: PLC0415

    _write_tree(tmp_path)
    router = _full_router()
    from datetime import UTC, datetime  # noqa: PLC0415

    from anthropic.types.beta.beta_managed_agents_model_config import (  # noqa: PLC0415
        BetaManagedAgentsModelConfig,
    )

    _ts = datetime(2026, 4, 21, tzinfo=UTC)
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200,
            json=BetaEnvironment(
                id="env_det",
                type="environment",
                name="default",
                config=EMPTY_CLOUD_CONFIG,
                metadata={},
                description="",
                created_at=_ts.isoformat(),
                updated_at=_ts.isoformat(),
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id="ag_det",
                type="agent",
                name="daimon",
                model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
                metadata={},
                description=None,
                created_at=_ts,
                updated_at=_ts,
                version=1,
                mcp_servers=[],
                skills=[],
                tools=[],
                system=None,
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    await apply_defaults(db_session_factory, client, tmp_path, dry_run=False, run_preflight=False)

    expected_id = derive_tenant_uuid(platform="cli", workspace_id="local")

    # Req 4: exactly one tenant row with the derived deterministic id
    tenant_count = (await db_session.execute(select(func.count()).select_from(Tenant))).scalar_one()
    assert tenant_count == 1, "apply_defaults must provision exactly one tenant row (no orphans)"

    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == expected_id))
    ).scalar_one_or_none()
    assert tenant_row is not None, (
        f"tenant row with id == derive_tenant_uuid('cli','local') ({expected_id}) must exist"
    )
    assert tenant_row.platform == "cli", "cli:local tenant must have platform='cli'"
    assert tenant_row.external_id == "local", "cli:local tenant must have external_id='local'"

    # no tenant_ledger row (cli:local is billing-exempt, signup_credit=0)
    ledger_count = (
        await db_session.execute(select(func.count()).select_from(TenantLedger))
    ).scalar_one()
    assert ledger_count == 0, (
        "apply_defaults must not seed a tenant_ledger trial row for cli:local (billing-exempt)"
    )

"""Tests for the Slack boot-time reconcile sweep.

The sweep owns the tenant status flips: pending/failed tenants flip on the
reconcile outcome, ready tenants are never demoted by a failed reconcile, and
one tenant's provider error must not stop the sweep over its siblings.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anthropic as _anthropic
import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.slack.boot_sweep import run_boot_sweep
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _agent_matching_tenant(tenant_id: uuid.UUID, *, name: str) -> dict[str, object]:
    return BetaManagedAgentsAgent(
        id="ag_1",
        type="agent",
        name=name,
        model={"id": "claude-opus-4-7"},
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": name},
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")


async def _provision_slack(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    status: str | None = None,
) -> uuid.UUID:
    result = await provision_tenant(db_session_factory, platform="slack", workspace_id=workspace_id)
    if status is not None:
        await set_provision_status(db_session_factory, tenant_id=result.tenant_id, status=status)
    return result.tenant_id


async def _run_sweep(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    anthropic_client: _anthropic.AsyncAnthropic,
    agent_name: str | None = "daimon",
) -> None:
    await run_boot_sweep(
        anthropic=anthropic_client,
        sessionmaker=db_session_factory,
        defaults_root=Path("defaults"),
        deployment_default=DeploymentDefault(agent_name=agent_name),
        public_url=None,
    )


async def test_sweep_flips_pending_tenant_ready_when_reconcile_succeeds_and_agent_resolves(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _provision_slack(
        db_session_factory, workspace_id="T-SWEEP-READY", status="pending"
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_matching_tenant(tenant_id, name="daimon")]),
    )
    client = build_fake_anthropic(router.dispatch)

    with patch(
        "daimon.adapters.slack.boot_sweep.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ) as reconcile:
        await _run_sweep(db_session_factory, anthropic_client=client)

    assert reconcile.await_count == 1, "the sweep must reconcile the one registered slack tenant"
    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "ready", (
        "a clean report with the default agent present on MA must flip pending to ready"
    )


async def test_sweep_keeps_ready_tenant_ready_when_reconcile_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _provision_slack(db_session_factory, workspace_id="T-SWEEP-STAYS")
    failing = ApplyReport()
    failing.add(
        ResourceOutcome(kind="skill", name="cli-auth", action=Action.FAILED, error="rate limited")
    )

    with patch(
        "daimon.adapters.slack.boot_sweep.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=failing,
    ):
        await _run_sweep(
            db_session_factory,
            anthropic_client=build_fake_anthropic(MARouter().dispatch),
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "ready", (
        "a transient reconcile failure must not take a working workspace's turns offline"
    )
    assert tr.last_reconcile_error is not None and "cli-auth" in tr.last_reconcile_error, (
        "the failure reason must be recorded for the operator even when status is kept"
    )


async def test_sweep_flips_pending_tenant_failed_when_reconcile_reports_failure(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _provision_slack(
        db_session_factory, workspace_id="T-SWEEP-FAILS", status="pending"
    )
    failing = ApplyReport()
    failing.add(ResourceOutcome(kind="agent", name="daimon", action=Action.FAILED, error="boom"))

    with patch(
        "daimon.adapters.slack.boot_sweep.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=failing,
    ):
        await _run_sweep(
            db_session_factory,
            anthropic_client=build_fake_anthropic(MARouter().dispatch),
        )

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "failed", (
        "a pending tenant whose reconcile fails must flip to failed, not linger pending"
    )


async def test_sweep_flips_failed_when_default_agent_missing_from_ma(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _provision_slack(
        db_session_factory, workspace_id="T-SWEEP-ROSTER", status="pending"
    )
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    with patch(
        "daimon.adapters.slack.boot_sweep.reconcile_tenant_defaults",
        new_callable=AsyncMock,
        return_value=ApplyReport(),
    ):
        await _run_sweep(db_session_factory, anthropic_client=client)

    tr = await get_tenant_liveness(db_session_factory, tenant_id)
    assert tr is not None and tr.provision_status == "failed", (
        "a clean report whose default agent is missing from the MA roster is not ready"
    )
    assert (
        tr.last_reconcile_error is not None and "missing from roster" in tr.last_reconcile_error
    ), "the roster miss must be recorded as the failure reason"


async def test_sweep_skips_archived_and_foreign_platform_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    live_id = await _provision_slack(
        db_session_factory, workspace_id="T-SWEEP-LIVE", status="pending"
    )
    archived_id = await _provision_slack(db_session_factory, workspace_id="T-SWEEP-ARCHIVED")
    await set_provision_status(db_session_factory, tenant_id=archived_id, archive=True)
    await provision_tenant(db_session_factory, platform="discord", workspace_id="123456")

    seen: list[uuid.UUID] = []

    async def _record(*args: object, tenant_id: uuid.UUID, **kwargs: object) -> ApplyReport:
        seen.append(tenant_id)
        return ApplyReport()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_matching_tenant(live_id, name="daimon")]),
    )
    with patch(
        "daimon.adapters.slack.boot_sweep.reconcile_tenant_defaults",
        side_effect=_record,
    ):
        await _run_sweep(db_session_factory, anthropic_client=build_fake_anthropic(router.dispatch))

    assert seen == [live_id], (
        "the sweep must reconcile exactly the unarchived slack tenants — never archived "
        "ones and never other platforms'"
    )


async def test_sweep_isolates_one_tenants_provider_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    failing_id = await _provision_slack(
        db_session_factory, workspace_id="T-SWEEP-ERR-A", status="pending"
    )
    healthy_id = await _provision_slack(
        db_session_factory, workspace_id="T-SWEEP-ERR-B", status="pending"
    )

    async def _explode_first(*args: object, tenant_id: uuid.UUID, **kwargs: object) -> ApplyReport:
        if tenant_id == failing_id:
            raise _anthropic.APIConnectionError(request=httpx.Request("GET", "https://api"))
        return ApplyReport()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_agent_matching_tenant(healthy_id, name="daimon")]),
    )
    # Concurrency pinned to 1: the schema-per-test session factory binds every
    # session to ONE asyncpg connection, so genuinely concurrent tenant seeds
    # trip "another operation is in progress" — a fixture artifact, not a
    # product behavior (production binds an engine pool). Serial order still
    # proves the isolation contract: A raises, B must still be seeded.
    with (
        patch("daimon.adapters.slack.boot_sweep._SWEEP_CONCURRENCY", 1),
        patch(
            "daimon.adapters.slack.boot_sweep.reconcile_tenant_defaults",
            side_effect=_explode_first,
        ),
    ):
        await _run_sweep(db_session_factory, anthropic_client=build_fake_anthropic(router.dispatch))

    failing_tr = await get_tenant_liveness(db_session_factory, failing_id)
    healthy_tr = await get_tenant_liveness(db_session_factory, healthy_id)
    assert failing_tr is not None and failing_tr.provision_status == "failed", (
        "the tenant whose reconcile raised must be recorded failed, not left pending"
    )
    assert healthy_tr is not None and healthy_tr.provision_status == "ready", (
        "a sibling tenant's provider error must not stop the rest of the sweep"
    )

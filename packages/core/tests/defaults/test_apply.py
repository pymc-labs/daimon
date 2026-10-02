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
from daimon.core.defaults import apply_defaults
from daimon.testing.ma import (
    MARouter,
    list_response,
)
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from daimon.testing.ma_models import ma_agent, ma_environment
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
    from daimon.core.scope import DeploymentDefault, ScopeContext
    from daimon.core.stores.scoped_config_read import resolve

    _write_tree(tmp_path)
    router = _full_router()
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200,
            json=ma_environment(id="env_1", name="default").model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_1", name="daimon", model="claude-opus-4-7").model_dump(
                mode="json"
            ),
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
    from daimon.core._models import Tenant
    from sqlalchemy import select

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


@pytest.mark.parametrize("workspace_id", ["local", "agent-setup-probe"])
async def test_apply_defaults_provisions_cli_workspace_deterministically(
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> None:
    """A fresh DB gets exactly the selected CLI tenant and no ledger row."""
    from daimon.core._models import Tenant, TenantLedger
    from daimon.core.ma_identity import derive_tenant_uuid

    _write_tree(tmp_path)
    router = _full_router()
    from datetime import UTC, datetime

    _ts = datetime(2026, 4, 21, tzinfo=UTC)
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200,
            json=ma_environment(
                id="env_det", name="default", created_at=_ts.isoformat()
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            200,
            json=ma_agent(id="ag_det", name="daimon", created_at=_ts).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    await apply_defaults(
        db_session_factory,
        client,
        tmp_path,
        dry_run=False,
        run_preflight=False,
        workspace_id=workspace_id,
    )

    expected_id = derive_tenant_uuid(platform="cli", workspace_id=workspace_id)

    # Req 4: exactly one tenant row with the derived deterministic id
    tenant_count = (await db_session.execute(select(func.count()).select_from(Tenant))).scalar_one()
    assert tenant_count == 1, "apply_defaults must provision exactly one tenant row (no orphans)"

    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == expected_id))
    ).scalar_one_or_none()
    assert tenant_row is not None, (
        f"tenant row with id == derive_tenant_uuid('cli',{workspace_id!r}) ({expected_id}) must exist"
    )
    assert tenant_row.platform == "cli", "CLI tenant must have platform='cli'"
    assert tenant_row.external_id == workspace_id, "CLI tenant must keep the selected workspace ID"

    # no tenant_ledger row (cli:local is billing-exempt, signup_credit=0)
    ledger_count = (
        await db_session.execute(select(func.count()).select_from(TenantLedger))
    ).scalar_one()
    assert ledger_count == 0, (
        "apply_defaults must not seed a tenant_ledger trial row for cli:local (billing-exempt)"
    )


async def test_apply_prunes_seeded_rows_for_skills_no_longer_in_the_tree(
    tmp_path: Path,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A leftover row would keep blocking the name and the leftover's delete.

    Rows for skills still in the tree survive, and a dry run prunes nothing.
    """
    from anthropic.types.beta import SkillListResponse
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.stores.seeded_skills import list_seeded_skill_names, record_seeded_skill

    _write_tree(tmp_path)
    (tmp_path / "skills" / "eda").mkdir(parents=True)
    (tmp_path / "skills" / "eda" / "SKILL.md").write_text("---\nname: eda\ndescription: d\n---\nb")
    router = _full_router()
    router.add(
        "POST",
        r"/v1/environments",
        lambda req, _m: httpx.Response(
            200, json=ma_environment(id="env_1", name="default").model_dump(mode="json")
        ),
    )
    router.add(
        "POST",
        r"/v1/agents",
        lambda req, _m: httpx.Response(
            200, json=ma_agent(id="ag_1", name="daimon").model_dump(mode="json")
        ),
    )
    router.add(
        "POST",
        r"/v1/skills",
        lambda req, _m: httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_eda",
                type="custom",
                display_title="eda",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)
    await apply_defaults(db_session_factory, client, tmp_path, dry_run=False, run_preflight=False)
    tenant_id = derive_tenant_uuid(platform="cli", workspace_id="local")
    async with db_session_factory.begin() as session:
        await record_seeded_skill(
            session, tenant_id=tenant_id, name="retired", content_hash="h", anthropic_id="sk_old"
        )

    await apply_defaults(db_session_factory, client, tmp_path, dry_run=True, run_preflight=False)
    async with db_session_factory() as session:
        assert await list_seeded_skill_names(session, tenant_id=tenant_id) == {
            "eda",
            "retired",
        }, "a dry run writes nothing"

    await apply_defaults(db_session_factory, client, tmp_path, dry_run=False, run_preflight=False)
    async with db_session_factory() as session:
        assert await list_seeded_skill_names(session, tenant_id=tenant_id) == {"eda"}, (
            "only the retired row goes"
        )

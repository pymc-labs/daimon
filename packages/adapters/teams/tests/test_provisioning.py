"""Boot provisioning of the configured Teams tenant."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.provisioning import provision_configured_tenant
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec
from mux.profiles.selfhosted import LOOP
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import ENTRA_TENANT_ID, FakeSender, bot_token, build_teams_runtime

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
DEFAULTS = DeploymentDefault(agent_name="daimon", environment_name="default")


async def _provision(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    failed: bool,
    admins: tuple[str, ...] | None = (),
) -> Any:
    report = MagicMock()
    report.is_failure.return_value = failed
    with (
        patch(
            "daimon.adapters.teams.provisioning.reconcile_tenant_defaults",
            AsyncMock(return_value=report),
        ),
        patch(
            "daimon.adapters.teams.provisioning.compose_failure_reason",
            return_value="agent 'daimon': boom",
        ),
        patch(
            "daimon.adapters.teams.provisioning.find_agent_by_daimon_tag",
            AsyncMock(return_value=object()),
        ),
    ):
        await provision_configured_tenant(
            anthropic=AsyncMock(),
            sessionmaker=db_factory,
            defaults_root=Path("defaults"),
            deployment_default=DeploymentDefault(agent_name="daimon"),
            public_url=None,
            entra_tenant_id=ENTRA_TENANT_ID,
            signup_credit=Decimal("0"),
            admin_ids=admins,
        )
    return await get_tenant_liveness(db_factory, TENANT)


async def _provision_without_anthropic(
    db_factory: async_sessionmaker[AsyncSession], default: DeploymentDefault = DEFAULTS
) -> Any:
    reconcile = AsyncMock()
    with patch("daimon.adapters.teams.provisioning.reconcile_tenant_defaults", reconcile):
        await provision_configured_tenant(
            anthropic=None,
            sessionmaker=db_factory,
            defaults_root=Path("defaults"),
            deployment_default=default,
            public_url=None,
            entra_tenant_id=ENTRA_TENANT_ID,
            signup_credit=Decimal("0"),
            admin_ids=(),
        )
    reconcile.assert_not_awaited()
    return await get_tenant_liveness(db_factory, TENANT)


async def _register_selfhosted(db_factory: async_sessionmaker[AsyncSession]) -> None:
    """The default agent and environment `daimon selfhosted init` writes for a granted tenant."""
    operator = Scope(
        tenant_id=str(TENANT),
        account_id="operator",
        principal_id="cli",
        authorization_id="cli-deployment-operator",
    )
    harness = {
        "harness_url": "https://harness.example",
        "auth_ref": "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY",
    }
    await SelfHostedAgents(db_factory).create(
        operator,
        AgentSpec(name="daimon", model=ModelRef(provider=LOOP.provider, id="gpt-5.4-mini")),
        key="agent",
    )
    await SelfHostedEnvironments(db_factory).create(
        operator,
        EnvironmentSpec(
            name="default",
            execution="self_hosted",
            native_config=ExtensionConfig(
                namespace="selfhosted.environment", version=1, value=harness
            ),
        ),
        key="environment",
    )


async def test_first_boot_provisions_a_ready_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _provision(db_session_factory, failed=False)
    assert tenant is not None and tenant.platform == "teams"
    assert tenant.external_id == ENTRA_TENANT_ID and tenant.provision_status == "ready"


async def test_a_failed_first_reconcile_leaves_the_tenant_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _provision(db_session_factory, failed=True)
    assert tenant is not None and tenant.provision_status == "failed"
    assert tenant.last_reconcile_error == "agent 'daimon': boom"


async def test_a_failed_reconcile_keeps_a_ready_tenant_ready(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _provision(db_session_factory, failed=False)
    tenant = await _provision(db_session_factory, failed=True)
    assert tenant is not None and tenant.provision_status == "ready"
    assert tenant.last_reconcile_error == "agent 'daimon': boom"


async def test_without_an_anthropic_key_the_tenant_is_ready_once_registered_for_selfhosted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Install boots before `register` writes the selfhosted records, so it is refused till then."""
    tenant = await _provision_without_anthropic(db_session_factory)
    assert tenant is not None and tenant.provision_status == "failed"
    assert tenant.last_reconcile_error == "agent 'daimon': missing from the selfhosted records"

    await _register_selfhosted(db_session_factory)
    tenant = await _provision_without_anthropic(db_session_factory)
    assert tenant is not None and tenant.provision_status == "ready"
    assert tenant.last_reconcile_error is None


async def test_without_an_anthropic_key_or_default_names_the_tenant_stays_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _provision_without_anthropic(db_session_factory)
    await _register_selfhosted(db_session_factory)
    tenant = await _provision_without_anthropic(db_session_factory, DeploymentDefault())
    assert tenant is not None and tenant.provision_status == "failed"
    assert tenant.last_reconcile_error == "no default agent and environment configured"


@pytest.mark.parametrize("keyed", [True, False], ids=["anthropic-key", "no-anthropic-key"])
async def test_boot_reconciles_with_anthropic_only_when_the_deployment_holds_its_key(
    db_session_factory: async_sessionmaker[AsyncSession],
    no_boot_provisioning: AsyncMock,
    keyed: bool,
) -> None:
    runtime = build_teams_runtime(db_session_factory)
    runtime.settings.anthropic.api_key = SecretStr("sk-test") if keyed else None
    teams = TeamsApp(runtime=runtime, sender=FakeSender(), commands={}, bot_token=bot_token)
    await teams.start()
    await teams.drain(timeout=1.0)
    expected = runtime.anthropic if keyed else None
    assert no_boot_provisioning.call_args.kwargs["anthropic"] is expected


async def test_an_archived_tenant_is_left_alone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _provision(db_session_factory, failed=False)
    await set_provision_status(db_session_factory, tenant_id=TENANT, archive=True)
    tenant = await _provision(db_session_factory, failed=True)
    assert tenant is not None and tenant.archived_at is not None
    assert tenant.last_reconcile_error is None


async def test_boot_demotes_an_admin_removed_from_the_list(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Routines and MCP clients read the stored role, so a removed admin loses it at boot."""
    await _provision(db_session_factory, failed=False)
    other = await provision_tenant(db_session_factory, platform="slack", workspace_id="T-OTHER")
    accounts = {}
    async with db_session_factory.begin() as session:
        for key, tenant, platform, user in (
            ("kept", TENANT, "teams", "kept-admin"),
            ("removed", TENANT, "teams", "removed-admin"),
            ("elsewhere", other.tenant_id, "slack", "removed-admin"),
        ):
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant, platform=platform, external_id=user
            )
            await set_role(session, principal.account_id, Role.ADMIN)
            accounts[key] = principal.account_id

    await _provision(db_session_factory, failed=False, admins=("kept-admin",))

    async with db_session_factory() as session:
        roles = {
            k: getattr(await get_account(session, a), "role", None) for k, a in accounts.items()
        }
    assert roles == {"kept": Role.ADMIN, "removed": Role.USER, "elsewhere": Role.ADMIN}, (
        "only this organisation's Teams admins who are not owners are demoted"
    )


async def test_boot_demotes_no_one_without_the_admins(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """None: the admin team's owners could not be read, so their stored roles stand."""
    demote = AsyncMock(return_value=0)
    with patch("daimon.adapters.teams.provisioning.demote_unlisted_admins", demote):
        await _provision(db_session_factory, failed=False, admins=None)
    demote.assert_not_awaited()


async def test_a_failed_demotion_never_blocks_provisioning(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with patch(
        "daimon.adapters.teams.provisioning.demote_unlisted_admins",
        AsyncMock(side_effect=SQLAlchemyError("boom")),
    ):
        tenant = await _provision(db_session_factory, failed=False)
    assert tenant is not None and tenant.provision_status == "ready"

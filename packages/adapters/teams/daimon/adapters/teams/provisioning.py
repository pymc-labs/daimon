"""Boot provisioning of the deployment's one Teams tenant.

Teams sends no install event a single-tenant bot can act on, so the tenant
for the configured Entra organisation is provisioned and reconciled at every
boot instead. Same status rules as the Slack boot sweep: success (clean
reconcile and the default agent on the roster) flips to `ready`; a failure
keeps an already-ready tenant ready and records the reason; otherwise the
tenant flips to `failed`. A deployment holding no Anthropic key has nothing
to reconcile there: success is selfhosted admission being on and a selfhosted
turn finding the default agent and environment, which `daimon selfhosted init`
writes. An archived tenant is left alone. Anyone no longer an owner of
`DAIMON_TEAMS__ADMIN_TEAM_ID`'s team loses the stored admin role here; a
failed owner read demotes no one.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from decimal import Decimal
from pathlib import Path

import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.config import load_selfhosted_settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.provisioning import provision_tenant, reconcile_tenant_defaults
from daimon.core.defaults.report import compose_failure_reason
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.mux_backend import resource_scope
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import demote_unlisted_admins
from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status
from daimon.core.turn.selfhosted_records import resolve_records
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def demote_former_admins(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    admin_ids: Collection[str],
) -> None:
    """Demote stored Teams admins not in `admin_ids`; a database failure is logged, not raised."""
    try:
        async with sessionmaker.begin() as session:
            demoted = await demote_unlisted_admins(
                session, tenant_id=tenant_id, platform="teams", admin_ids=admin_ids
            )
    except SQLAlchemyError:
        log.exception("teams.admin_demotion_failed", tenant_id=str(tenant_id))
    else:
        if demoted:
            log.info("teams.admins_demoted", tenant_id=str(tenant_id), count=demoted)


async def _selfhosted_failure_reason(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    deployment_default: DeploymentDefault,
) -> str | None:
    """Why selfhosted turns could not answer with the default agent and environment, or None."""
    if not load_selfhosted_settings().admission_enabled:
        return "selfhosted admission is off (DAIMON_SELFHOSTED__ADMISSION_ENABLED)"
    if deployment_default.agent_name is None or deployment_default.environment_name is None:
        return "no default agent and environment configured"
    try:
        await resolve_records(
            sessionmaker,
            resource_scope(tenant_id=str(tenant_id)),
            agent_name=deployment_default.agent_name,
            environment_name=deployment_default.environment_name,
            responder_id=None,
        )
    except MAResolverMissError as miss:
        return f"{miss.kind} {miss.daimon_tag!r}: missing from the selfhosted records"
    return None


async def provision_configured_tenant(
    *,
    anthropic: AsyncAnthropic | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    defaults_root: Path,
    deployment_default: DeploymentDefault,
    public_url: str | None,
    entra_tenant_id: str,
    signup_credit: Decimal,
    admin_ids: Collection[str] | None,
) -> uuid.UUID | None:
    """Provision and reconcile the configured tenant. Returns its id unless archived.

    `anthropic` None (the deployment holds no Anthropic key) reconciles nothing
    with Anthropic. `admin_ids` None (the admin team's owners could not be read)
    demotes no one.
    """
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=entra_tenant_id)
    existing = await get_tenant_liveness(sessionmaker, tenant_id)
    if existing is not None and existing.archived_at is not None:
        log.warning("teams.tenant_archived", tenant_id=str(tenant_id))
        return None
    # First, so the reconcile below neither delays nor skips it; a failure never blocks it.
    if admin_ids is not None:
        await demote_former_admins(sessionmaker, tenant_id=tenant_id, admin_ids=admin_ids)
    await provision_tenant(
        sessionmaker, platform="teams", workspace_id=entra_tenant_id, signup_credit=signup_credit
    )
    # A new row defaults to ready; it is not until the reconcile below passes.
    was_ready = existing is not None and existing.provision_status == "ready"
    if not was_ready:
        await set_provision_status(sessionmaker, tenant_id=tenant_id, status="pending")
    reason: str | None = None
    try:
        if anthropic is None:
            reason = await _selfhosted_failure_reason(sessionmaker, tenant_id, deployment_default)
        else:
            report = await reconcile_tenant_defaults(
                anthropic, sessionmaker, defaults_root, tenant_id=tenant_id, public_url=public_url
            )
            if report.is_failure():
                reason = compose_failure_reason(report)
            elif deployment_default.agent_name is not None:
                agent = await find_agent_by_daimon_tag(
                    anthropic, tenant_id=tenant_id, name=deployment_default.agent_name
                )
                if agent is None:
                    reason = (
                        f"agent {deployment_default.agent_name!r}: "
                        "default agent missing from roster after reconcile"
                    )
    except (DaimonError, _anthropic.APIError, SQLAlchemyError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
    if reason is None:
        await set_provision_status(
            sessionmaker, tenant_id=tenant_id, status="ready", clear_reason=True
        )
        log.info("teams.tenant_ready", tenant_id=str(tenant_id))
    else:
        log.warning("teams.tenant_reconcile_failed", tenant_id=str(tenant_id), reason=reason)
        await set_provision_status(
            sessionmaker,
            tenant_id=tenant_id,
            status=None if was_ready else "failed",
            reason=reason,
        )
    return tenant_id

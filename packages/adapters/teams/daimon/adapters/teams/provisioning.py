"""Boot provisioning of the deployment's one Teams tenant.

Teams sends no install event a single-tenant bot can act on, so the tenant
for the configured Entra organisation is provisioned and reconciled at every
boot instead. Same status rules as the Slack boot sweep: success (clean
reconcile and the default agent on the roster) flips to `ready`; a failure
keeps an already-ready tenant ready and records the reason; otherwise the
tenant flips to `failed`. An archived tenant is left alone. Anyone no
longer in `DAIMON_TEAMS__ADMIN_USER_IDS` loses the stored admin role here.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.provisioning import provision_tenant, reconcile_tenant_defaults
from daimon.core.defaults.report import compose_failure_reason
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import demote_unlisted_admins
from daimon.core.stores.tenants import get_tenant_liveness, set_provision_status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def provision_configured_tenant(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    defaults_root: Path,
    deployment_default: DeploymentDefault,
    public_url: str | None,
    entra_tenant_id: str,
    signup_credit: Decimal,
    admin_user_ids: tuple[str, ...],
) -> uuid.UUID | None:
    """Provision and reconcile the configured tenant. Returns its id unless archived."""
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=entra_tenant_id)
    existing = await get_tenant_liveness(sessionmaker, tenant_id)
    if existing is not None and existing.archived_at is not None:
        log.warning("teams.tenant_archived", tenant_id=str(tenant_id))
        return None
    await provision_tenant(
        sessionmaker, platform="teams", workspace_id=entra_tenant_id, signup_credit=signup_credit
    )
    # A new row defaults to ready; it is not until the reconcile below passes.
    was_ready = existing is not None and existing.provision_status == "ready"
    if not was_ready:
        await set_provision_status(sessionmaker, tenant_id=tenant_id, status="pending")
    reason: str | None = None
    try:
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
    # Last, so a failure here never holds the tenant's status back.
    async with sessionmaker.begin() as session:
        demoted = await demote_unlisted_admins(
            session, tenant_id=tenant_id, platform="teams", admin_ids=admin_user_ids
        )
    if demoted:
        log.info("teams.admins_demoted", tenant_id=str(tenant_id), count=demoted)
    return tenant_id

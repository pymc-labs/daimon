"""Real-Postgres tests for a workspace's web settings, roles and audit trail."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.features.service import set_workspace_feature
from daimon.core.stores import web_audit
from daimon.core.stores import web_roles as roles
from daimon.core.stores import web_workspace_settings as settings
from daimon.core.stores.domain import WebRoleGrant
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 5, 1, tzinfo=UTC)


async def test_settings_default_to_everything_on_and_modules_come_from_features(
    db_session: AsyncSession,
) -> None:
    """The saved row holds the default agent; which modules are off is the `web.*` features."""
    tenant = await make_tenant(db_session)
    fresh = await settings.get_settings(db_session, tenant_id=tenant.id)
    for module in ("agents", "credits"):
        await set_workspace_feature(
            db_session,
            None,
            tenant_id=tenant.id,
            platform="web",
            key=f"web.{module}",
            value="off",
            account_id=None,
            surface="web",
        )
    saved = await settings.save_settings(
        db_session, tenant_id=tenant.id, default_agent="a1", now=_NOW
    )

    assert (fresh.modules_off, fresh.default_agent) == ((), None), (
        "a workspace that chose nothing has everything on"
    )
    assert saved.modules_off == ("agents", "credits"), "the modules switched off are listed"
    assert saved.default_agent == "a1", "the default agent is saved"
    assert await settings.get_settings(db_session, tenant_id=tenant.id) == saved, "it is kept"


async def test_workspace_roles_are_listed_by_account(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    admin = await make_account(db_session, tenant=tenant)
    grants: tuple[tuple[uuid.UUID, WebRoleGrant], ...] = ((owner.id, "owner"), (admin.id, "admin"))
    for account_id, role in grants:
        await roles.set_workspace_role(
            db_session,
            tenant_id=tenant.id,
            account_id=account_id,
            role=role,
            granted_by_account_id=None,
        )

    listed = await roles.list_workspace_roles(db_session, tenant_id=tenant.id)

    assert listed == {owner.id: "owner", admin.id: "admin"}, "every grant, by account"


async def test_audit_lists_newest_first(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    for minutes, action in ((0, "Created invite"), (5, "Revoked invite")):
        await web_audit.record(
            db_session,
            tenant_id=tenant.id,
            actor_account_id=None,
            action=action,
            target="guest@example.com",
            at=_NOW + timedelta(minutes=minutes),
        )

    trail = await web_audit.list_recent(db_session, tenant_id=tenant.id)

    assert [a.action for a in trail] == ["Revoked invite", "Created invite"], "newest first"

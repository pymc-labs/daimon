"""Tests for archive_tenant + teardown_slack_install helpers (Phase 79, SINST-02).

Covers:
- archive_tenant sets Tenant.archived_at in one transaction (no-op when tenant absent)
- teardown_slack_install soft-archives the tenant AND deletes the slack_bot_tokens row
- teardown is idempotent when the token row is already absent (D-14)
"""

from __future__ import annotations

from datetime import UTC, datetime

from daimon.core._models import Tenant
from daimon.core.defaults.provisioning import (
    archive_tenant,
    provision_tenant,
    teardown_slack_install,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token, upsert_slack_bot_token
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)


async def test_teardown_slack_install_archives_tenant_and_deletes_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """teardown_slack_install: after teardown, archived_at is non-null AND token row is gone."""
    team_id = "T_TEARDOWN_01"

    # Provision the tenant
    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)

    # Upsert a token row
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=b"encrypted-xoxb-token",
    )
    await db_session.flush()

    # Verify setup: token row exists before teardown
    pre_teardown_token = await get_slack_bot_token(db_session, team_id=team_id)
    assert pre_teardown_token is not None, "token row must exist before teardown"

    # Run teardown
    await teardown_slack_install(db_session_factory, team_id=team_id, now=_NOW)

    # Assert token row is gone (re-read via same session)
    post_token = await get_slack_bot_token(db_session, team_id=team_id)
    assert post_token is None, "teardown_slack_install must delete the slack_bot_tokens row (D-14)"

    # Assert tenant is soft-archived (re-SELECT via shared session)
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    assert tenant_row.archived_at == _NOW, (
        "teardown_slack_install must set Tenant.archived_at = now (D-13)"
    )


async def test_teardown_slack_install_idempotent_when_no_token_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """teardown_slack_install is idempotent when the token row is already absent."""
    team_id = "T_TEARDOWN_IDEM"

    # Provision the tenant but do NOT upsert a token
    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)

    # Verify there's no token row
    no_token = await get_slack_bot_token(db_session, team_id=team_id)
    assert no_token is None, "precondition: no token row before teardown"

    # Run teardown — must not raise
    await teardown_slack_install(db_session_factory, team_id=team_id, now=_NOW)

    # Tenant is still archived
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    assert tenant_row.archived_at == _NOW, (
        "teardown_slack_install must archive the tenant even when token row was absent"
    )


async def test_archive_tenant_sets_archived_at_for_existing_tenant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """archive_tenant updates archived_at for a known tenant id."""
    await provision_tenant(db_session_factory, platform="slack", workspace_id="T_ARCHIVE_01")
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id="T_ARCHIVE_01")

    await archive_tenant(db_session_factory, tenant_id=tenant_id, now=_NOW)

    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    assert tenant_row.archived_at == _NOW, (
        "archive_tenant must set archived_at to the injected now parameter"
    )


async def test_archive_tenant_is_noop_for_unknown_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """archive_tenant is a no-op (0 rows) when the tenant does not exist, never raises."""
    import uuid

    unknown_id = uuid.uuid4()
    # Should complete without raising
    await archive_tenant(db_session_factory, tenant_id=unknown_id, now=_NOW)

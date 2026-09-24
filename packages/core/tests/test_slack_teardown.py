"""Tests for archive_tenant + teardown_slack_install helpers.

Covers:
- archive_tenant sets Tenant.archived_at in one transaction (no-op when tenant absent)
- teardown_slack_install soft-archives the tenant AND deletes the slack_bot_tokens row
- teardown is idempotent when the token row is already absent
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core._models import Tenant
from daimon.core.defaults.provisioning import (
    archive_tenant,
    provision_tenant,
    teardown_slack_install,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token, upsert_slack_bot_token
from daimon.core.stores.slack_connect_prompts import mark_connect_prompted, was_connect_prompted
from daimon.core.stores.slack_event_dedup import insert_if_new
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
    assert post_token is None, "teardown_slack_install must delete the slack_bot_tokens row"

    # Assert tenant is soft-archived (re-SELECT via shared session)
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    assert tenant_row.archived_at == _NOW, (
        "teardown_slack_install must set Tenant.archived_at = now"
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


async def test_teardown_slack_install_deletes_connect_prompts_and_event_dedup(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """teardown_slack_install must also clear slack_connect_prompts and slack_event_dedup rows."""
    team_id = "T_TEARDOWN_02"

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)

    await mark_connect_prompted(db_session, team_id=team_id, slack_user_id="U1", now=_NOW)
    was_new = await insert_if_new(db_session, team_id=team_id, channel="C1", event_ts="1.1")
    assert was_new, "precondition: dedup row must be a genuine first insert"
    await db_session.flush()

    assert await was_connect_prompted(db_session, team_id=team_id, slack_user_id="U1"), (
        "precondition: connect prompt row must exist before teardown"
    )

    await teardown_slack_install(db_session_factory, team_id=team_id, now=_NOW)

    assert not await was_connect_prompted(db_session, team_id=team_id, slack_user_id="U1"), (
        "teardown_slack_install must delete the slack_connect_prompts row"
    )
    # Re-inserting the same triple after teardown must succeed as a genuine
    # first insert — proof the slack_event_dedup row is really gone.
    was_new_after = await insert_if_new(db_session, team_id=team_id, channel="C1", event_ts="1.1")
    assert was_new_after, "teardown_slack_install must delete the slack_event_dedup row"


async def test_archive_tenant_is_noop_for_unknown_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """archive_tenant is a no-op (0 rows) when the tenant does not exist, never raises."""
    import uuid

    unknown_id = uuid.uuid4()
    # Should complete without raising
    await archive_tenant(db_session_factory, tenant_id=unknown_id, now=_NOW)


async def test_teardown_older_than_the_stored_token_leaves_the_reinstall_alone(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A teardown for an uninstall that happened before the current token was
    stored (a delayed or retried app_uninstalled / tokens_revoked arriving after
    a reinstall) must not delete the fresh token or re-archive the tenant."""
    team_id = "T_TEARDOWN_LATE"
    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(db_session, team_id=team_id, encrypted_token=b"reinstall-token")
    await db_session.commit()
    stored = await get_slack_bot_token(db_session, team_id=team_id)
    assert stored is not None
    uninstalled_at = stored.updated_at - timedelta(minutes=2)

    await teardown_slack_install(
        db_session_factory, team_id=team_id, now=_NOW, event_time=uninstalled_at
    )

    db_session.expire_all()
    assert await get_slack_bot_token(db_session, team_id=team_id) is not None, (
        "a teardown older than the stored token must not delete the reinstall's token"
    )
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    assert tenant_row.archived_at is None, (
        "a teardown older than the stored token must not re-archive the reinstalled tenant"
    )


async def test_teardown_newer_than_the_stored_token_tears_down(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ordinary uninstall: the event postdates the token, so teardown runs."""
    team_id = "T_TEARDOWN_CURRENT"
    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(db_session, team_id=team_id, encrypted_token=b"install-token")
    await db_session.commit()
    stored = await get_slack_bot_token(db_session, team_id=team_id)
    assert stored is not None

    await teardown_slack_install(
        db_session_factory,
        team_id=team_id,
        now=_NOW,
        event_time=stored.updated_at + timedelta(minutes=2),
    )

    db_session.expire_all()
    assert await get_slack_bot_token(db_session, team_id=team_id) is None
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    assert tenant_row.archived_at is not None

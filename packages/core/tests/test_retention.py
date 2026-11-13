"""The retention sweep deletes what is past its lifetime and keeps the rest."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core._models import Tenant, ThreadSession, WebAudit
from daimon.core.config import Settings, load_settings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.retention import sweep
from daimon.testing.factories import make_tenant
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_FEATURES__CONVERSATION_RETENTION", "30")
    return load_settings(_env_file=None)


def _mapping(
    tenant_id: uuid.UUID, thread: str, *, last_turn: datetime, active: bool
) -> ThreadSession:
    return ThreadSession(
        tenant_id=tenant_id,
        platform="discord",
        thread_id=thread,
        ma_session_id=f"sesn_{thread}",
        last_turn_at=last_turn,
        active_turn_message_id="m1" if active else None,
    )


async def test_sweep_deletes_aged_records_and_keeps_fresh_ones(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    tenant = await make_tenant(db_session)
    db_session.add_all(
        [
            WebAudit(tenant_id=tenant.id, at=NOW - timedelta(days=91), action="a", target="old"),
            WebAudit(tenant_id=tenant.id, at=NOW - timedelta(days=1), action="a", target="new"),
            _mapping(tenant.id, "idle", last_turn=NOW - timedelta(days=31), active=False),
            _mapping(tenant.id, "busy", last_turn=NOW - timedelta(days=31), active=True),
            _mapping(tenant.id, "recent", last_turn=NOW - timedelta(days=2), active=False),
        ]
    )
    await db_session.flush()

    await sweep(db_session_factory, settings, now=NOW)

    targets = set(await db_session.scalars(select(WebAudit.target)))
    assert targets == {"new"}, "admin history past 90 days goes, newer stays"
    threads = set(await db_session.scalars(select(ThreadSession.thread_id)))
    assert threads == {"busy", "recent"}, "only an idle mapping with no running turn goes"


async def test_zero_keeps_records_forever(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    tenant = await make_tenant(db_session)
    db_session.add(
        WebAudit(tenant_id=tenant.id, at=NOW - timedelta(days=900), action="a", target="t")
    )
    await db_session.flush()
    kept = settings.model_copy(
        update={"retention": settings.retention.model_copy(update={"admin_history_days": 0})}
    )

    await sweep(db_session_factory, kept, now=NOW)

    assert await db_session.scalar(select(WebAudit.target)) == "t", "0 turns the rule off"


async def test_archived_tenant_is_retired_unless_installed_again(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    gone = await make_tenant(db_session, workspace_id="gone")
    back_id = derive_tenant_uuid(platform="discord", workspace_id="back")
    back = await make_tenant(db_session, workspace_id="back", id=back_id)
    await db_session.execute(
        update(Tenant)
        .where(Tenant.id.in_([gone.id, back.id]))
        .values(archived_at=datetime.now(UTC) - timedelta(days=40))
    )
    await db_session.flush()
    # A reinstall restarts the archive clock before it clears the archive.
    await provision_tenant(db_session_factory, platform="discord", workspace_id="back")

    await sweep(db_session_factory, settings, now=datetime.now(UTC))

    retired = dict((await db_session.execute(select(Tenant.id, Tenant.retired_at))).tuples().all())
    assert retired[gone.id] is not None, "a tenant archived past the window is retired, not deleted"
    assert retired[back.id] is None, "a tenant being installed again is kept"

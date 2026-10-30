"""Permanent watches: a NULL expiry is live everywhere, and one paused 30 days expires."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.stores import watches
from daimon.core.stores.domain import WatchRow
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

#: What the `permanent_watch` fixture yields.
PermanentWatch = tuple[async_sessionmaker[AsyncSession], WatchRow, ContinuationRequest]

_REPO = Path(__file__).resolve().parents[3]
_MIGRATION = _REPO / "packages/core/alembic/versions/0164_watch_lifetime_caps.py"
#: A comparison on any `expires_at` but a lease's or a signing key's (`k.`, never NULL).
_COMPARED = re.compile(
    r"(?<![\w.])(?:(?!k\.)\w+\.)?(?<!lease_)expires_at\s*(?:<=|>=|<|>)"
    r"|(?:<=|>=|<|>)\s*(?:(?!k\.)\w+\.)?(?<!lease_)expires_at\b"
)
_NULL_ARM = re.compile(r"IS (?:NOT )?NULL|is (?:not )?None|is_\(None\)")
#: Python that reads a watch's expiry; each comparison goes through `is_expired` or
#: keeps the None arm on its line.
_PYTHON = (
    "packages/core/daimon/core/stores/watches.py",
    "packages/core/daimon/core/continuity/watch_turn.py",
    "packages/core/daimon/core/continuity/watch_notices.py",
    "packages/core/daimon/core/continuity/watch_reports.py",
    "packages/adapters/mcp/daimon/adapters/mcp/watch_polling.py",
    "packages/adapters/mcp/daimon/adapters/mcp/tools/watches.py",
    "packages/adapters/cli/daimon/adapters/cli/commands/watches.py",
    "packages/adapters/discord/daimon/adapters/discord/watch_access.py",
    "packages/adapters/discord/daimon/adapters/discord/watch_delivery.py",
)


def _unarmed(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if _COMPARED.search(line)
        and not _NULL_ARM.search(line)
        and "WatchCapability.expires_at" not in line
    ]


def test_every_sql_expiry_comparison_keeps_the_null_arm():
    sql = _MIGRATION.read_text()
    assert _COMPARED.search(sql), "the grep must see the migration's comparisons"
    assert _unarmed(sql) == []


@pytest.mark.parametrize("path", _PYTHON)
def test_every_python_expiry_comparison_keeps_the_none_arm(path):
    assert _unarmed((_REPO / path).read_text()) == []


def test_the_grep_catches_a_bare_comparison():
    assert _unarmed("WHERE v.expires_at>t AND x") == ["WHERE v.expires_at>t AND x"]
    assert _unarmed("or watch.expires_at <= now") == ["or watch.expires_at <= now"]
    assert _unarmed("OR w.lease_expires_at <= t") == []
    assert _unarmed("AND k.expires_at > t") == []


def test_is_expired_treats_none_as_permanent():
    now = datetime(2026, 10, 29, tzinfo=UTC)
    assert not watches.is_expired(None, now)
    assert watches.is_expired(now, now)
    assert not watches.is_expired(now + timedelta(seconds=1), now)


async def _owner_binding(db: AsyncSession) -> ContinuationRequest:
    """A tenant, an owner and the owner's live conversation the watches bind to."""
    from daimon.core.stores.identity import get_or_create_platform_principal
    from daimon.core.stores.thread_sessions import create_thread_session
    from daimon.testing.factories import make_tenant

    tenant = await make_tenant(db)
    principal = await get_or_create_platform_principal(
        db, tenant_id=tenant.id, platform="discord", external_id="123"
    )
    await create_thread_session(
        db,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="4242",
        channel_id="4241",
        account_id=principal.account_id,
        ma_session_id="session-bound",
        ma_agent_id="agent-bound",
    )
    return ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="4241",
        thread_id="4242",
        requester_account_id=principal.account_id,
        requester_external_user_id="123",
        target_ma_agent_id="agent-bound",
        target_name="analyst",
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )


@pytest.fixture
async def permanent_watch(db_session_factory, monkeypatch):
    from daimon.testing.watch_fixtures import install_poll_ports

    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        await install_poll_ports(db)
        await watches.set_watch_control(db, scope="global", enabled=True)
        binding = await _owner_binding(db)
        watch = await watches.register_poll_watch(
            db,
            binding,
            instructions="Report changes.",
            tool="read_thread",
            arguments={"thread_id": "4343"},
            projection=".rows",
            rule="change",
            interval=60,
            expires_at=None,
            now=now,
            permanent_approved_by=binding.requester_account_id,
        )
    return db_session_factory, watch, binding


async def _claim(factory, watch_id):
    from sqlalchemy import text

    async with factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET next_check_at=now()-interval '1 second' WHERE id=:id"),
            {"id": watch_id},
        )
        return [
            row for row in await watches.claim_poll_watches(db) if row["watch_id"] == str(watch_id)
        ]


@pytest.mark.fresh_schema
async def test_a_permanent_watch_is_claimed_and_fires(permanent_watch):
    factory, watch, _ = permanent_watch
    assert watch.expires_at is None and watch.permanent_approved_by is not None
    (claimed,) = await _claim(factory, watch.id)
    async with factory.begin() as db:
        leased = await watches.leased_poll(db, watch.id, claimed["lease"], now=datetime.now(UTC))
        assert leased is not None
        await watches.finish_poll(db, leased, claimed["lease"], value={"v": 1}, payload=None)
    (claimed,) = await _claim(factory, watch.id)
    async with factory.begin() as db:
        leased = await watches.leased_poll(db, watch.id, claimed["lease"], now=datetime.now(UTC))
        assert leased is not None
        wake = await watches.finish_poll(db, leased, claimed["lease"], value={"v": 2}, payload="x")
    assert wake is not None
    async with factory() as db:
        from daimon.core.stores.task_continuations import get_continuation

        queued = await get_continuation(db, idempotency_key=wake)
        assert queued is not None
        assert await watches.watch_seed(db, queued, now=datetime.now(UTC)) is not None


@pytest.mark.fresh_schema
async def test_a_paused_permanent_watch_expires_after_30_days(permanent_watch):
    from sqlalchemy import text

    factory, watch, _ = permanent_watch
    async with factory.begin() as db:
        await watches.pause_watch(db, watch.id)
    async with factory() as db:
        paused = await watches.get_watch(db, watch.id)
    assert paused is not None and paused.paused_at is not None
    # Paused 29 days: still paused and still resumable.
    async with factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET paused_at=now()-interval '29 days' WHERE id=:id"),
            {"id": watch.id},
        )
    await _claim(factory, watch.id)
    async with factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "paused"  # type: ignore[union-attr]
    async with factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET paused_at=now()-interval '30 days 1 second' WHERE id=:id"),
            {"id": watch.id},
        )
    await _claim(factory, watch.id)
    async with factory.begin() as db:
        expired = await watches.get_watch(db, watch.id)
        assert expired is not None and expired.status == "expired"
        with pytest.raises(ValueError, match="cannot resume"):
            await watches.resume_watch(db, watch.id, now=datetime.now(UTC))
        # The owner hears it expired, like any watch that runs out.
        claimed = await watches.claim_import_notice(db, now=datetime.now(UTC))
        assert claimed is not None and claimed[0].id == watch.id
        assert watches.notice_kind(claimed[0].status, claimed[0].source_state) == "expired"


@pytest.mark.fresh_schema
async def test_an_active_permanent_watch_never_expires(permanent_watch):
    from sqlalchemy import text

    factory, watch, _ = permanent_watch
    async with factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET created_at=now()-interval '400 days' WHERE id=:id"),
            {"id": watch.id},
        )
    assert await _claim(factory, watch.id)
    async with factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "active"  # type: ignore[union-attr]


@pytest.mark.fresh_schema
async def test_permanent_needs_an_approver_and_owners_get_five(permanent_watch):
    factory, _, binding = permanent_watch
    now = datetime.now(UTC)

    async def register(**kwargs):
        async with factory.begin() as db:
            return await watches.register_poll_watch(
                db,
                binding,
                instructions="Report changes.",
                tool="read_thread",
                arguments={"thread_id": "4343"},
                projection=".rows",
                rule="change",
                interval=60,
                expires_at=None,
                now=now,
                **kwargs,
            )

    with pytest.raises(ValueError, match="Only an admin"):
        await register()
    for _ in range(watches.MAX_PERMANENT_PER_OWNER - 1):
        await register(permanent_approved_by=binding.requester_account_id)
    with pytest.raises(ValueError, match="already have 5 permanent"):
        await register(permanent_approved_by=binding.requester_account_id)


@pytest.mark.fresh_schema
async def test_ceilings_read_tenant_then_global_then_defaults(permanent_watch):
    factory, watch, _ = permanent_watch
    async with factory.begin() as db:
        assert await watches.watch_ceilings(db, watch.tenant_id) == watches.WatchCeilings(24, 100)
        await watches.set_watch_limits(db, scope="global", per_watch=48, per_owner=None)
        assert await watches.watch_ceilings(db, watch.tenant_id) == watches.WatchCeilings(48, 100)
        await watches.set_watch_limits(
            db, scope=f"tenant:{watch.tenant_id}", per_watch=2, per_owner=10
        )
        assert await watches.watch_ceilings(db, watch.tenant_id) == watches.WatchCeilings(2, 10)
        # Setting limits never switches a gate.
        assert await watches.is_watch_enabled(db, watch)
        with pytest.raises(ValueError):
            await watches.set_watch_limits(
                db, scope=f"owner:{watch.owner_account_id}", per_watch=2, per_owner=None
            )
        with pytest.raises(ValueError):
            await watches.set_watch_limits(db, scope="global", per_watch=241, per_owner=None)


async def _register(
    factory: async_sessionmaker[AsyncSession],
    binding: ContinuationRequest,
    *,
    expires_at: datetime | None,
    approved_by: uuid.UUID | None = None,
) -> WatchRow:
    async with factory.begin() as db:
        return await watches.register_poll_watch(
            db,
            binding,
            instructions="Report changes.",
            tool="read_thread",
            arguments={"thread_id": "4343"},
            projection=".rows",
            rule="change",
            interval=60,
            expires_at=expires_at,
            now=datetime.now(UTC),
            permanent_approved_by=approved_by,
        )


async def _make_permanent(db: AsyncSession, watch: WatchRow, *, now: datetime) -> WatchRow:
    return await watches.update_watch(
        db,
        watch.id,
        tenant_id=watch.tenant_id,
        owner_account_id=watch.owner_account_id,
        expected_revision=watch.revision,
        edit=watches.WatchEdit(lifetime="permanent"),
        now=now,
        admin_account_id=watch.owner_account_id,
    )


async def test_an_edit_and_a_create_cannot_both_take_the_last_permanent_place(
    db_committing_engine: AsyncEngine, db_clean: None, monkeypatch: pytest.MonkeyPatch
):
    """Two real transactions: the edit holds the owner lock until it commits."""
    import asyncio

    from daimon.core._models import Watch
    from sqlalchemy import func, select, text

    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with factory.begin() as db:
        binding = await _owner_binding(db)
    owner = binding.requester_account_id
    for _ in range(watches.MAX_PERMANENT_PER_OWNER - 1):
        await _register(factory, binding, expires_at=None, approved_by=owner)
    dated = await _register(factory, binding, expires_at=datetime.now(UTC) + timedelta(days=5))
    async with factory.begin() as db:
        await _make_permanent(db, dated, now=datetime.now(UTC))
        # The create starts while the edit is still uncommitted.
        create = asyncio.create_task(
            _register(factory, binding, expires_at=None, approved_by=owner)
        )
        # Commit only once the create has finished or waits on a lock.
        for _ in range(200):
            async with factory() as probe:
                waiting = await probe.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                        "WHERE d.datname = current_database() "
                        "AND l.locktype = 'advisory' AND NOT l.granted"
                    )
                )
            if create.done() or waiting:
                break
            await asyncio.sleep(0.05)
    with pytest.raises(ValueError, match="already have 5 permanent"):
        await create
    async with factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(Watch)
            .where(Watch.owner_account_id == owner, Watch.expires_at.is_(None))
        )
    assert count == watches.MAX_PERMANENT_PER_OWNER


@pytest.mark.fresh_schema
async def test_a_long_paused_permanent_watch_cannot_resume_or_change_while_gated_off(
    permanent_watch: PermanentWatch,
):
    from sqlalchemy import text

    factory, watch, _ = permanent_watch
    async with factory.begin() as db:
        await watches.pause_watch(db, watch.id)
        # With the gate off the claim function never expires it.
        await watches.set_watch_control(db, scope="global", enabled=False)
        await db.execute(
            text("UPDATE watches SET paused_at=now()-interval '30 days 1 second' WHERE id=:id"),
            {"id": watch.id},
        )
    async with factory.begin() as db:
        with pytest.raises(ValueError, match="cannot resume"):
            await watches.resume_watch(db, watch.id, now=datetime.now(UTC))
    async with factory.begin() as db:
        paused = await watches.get_watch(db, watch.id)
        assert paused is not None
        with pytest.raises(ValueError, match="cannot be changed"):
            await watches.update_watch(
                db,
                watch.id,
                tenant_id=watch.tenant_id,
                owner_account_id=watch.owner_account_id,
                expected_revision=paused.revision,
                edit=watches.WatchEdit(instructions="Report other changes."),
                now=datetime.now(UTC),
                admin_account_id=watch.owner_account_id,
            )


@pytest.mark.fresh_schema
async def test_making_a_long_paused_watch_permanent_restarts_its_paused_clock(
    permanent_watch: PermanentWatch,
):
    from sqlalchemy import text

    factory, _, binding = permanent_watch
    dated = await _register(factory, binding, expires_at=datetime.now(UTC) + timedelta(days=5))
    async with factory.begin() as db:
        await watches.pause_watch(db, dated.id)
        await db.execute(
            text("UPDATE watches SET paused_at=now()-interval '29 days' WHERE id=:id"),
            {"id": dated.id},
        )
    now = datetime.now(UTC)
    async with factory.begin() as db:
        paused = await watches.get_watch(db, dated.id)
        assert paused is not None
        edited = await _make_permanent(db, paused, now=now)
    assert edited.expires_at is None and edited.status == "paused"
    assert edited.paused_at == now
    async with factory.begin() as db:
        await watches.resume_watch(db, dated.id, now=datetime.now(UTC))

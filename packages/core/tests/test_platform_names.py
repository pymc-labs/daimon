"""Last-known names: the store, the background recorder and the panel's resolution order."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core import platform_names
from daimon.core.platform_names import (
    KnownName,
    remember_channel_names,
    remember_user_name,
    remember_user_names,
    resolve_names,
    settle,
)
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_principal
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.platform_names import (
    count_user_names_for_platform_user,
    delete_user_names_for_platform_user,
    get_channel_names,
    get_user_names,
    upsert_channel_names,
    upsert_user_names,
)
from daimon.core.stores.tenants import delete_tenant
from daimon.testing.factories import make_tenant
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def _fresh_memo() -> Iterator[None]:
    platform_names.forget_all()
    yield
    platform_names.forget_all()


# ---- the store ----


async def test_upsert_keeps_the_stored_part_a_later_sighting_leaves_out(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    names = {"U1": KnownName(display_name="Maya Chen", handle="maya")}
    await upsert_user_names(db_session, tenant_id=tenant.id, platform="slack", names=names)
    await upsert_user_names(
        db_session, tenant_id=tenant.id, platform="slack", names={"U1": KnownName(handle="maya2")}
    )

    known = await get_user_names(db_session, tenant_id=tenant.id, platform="slack", user_ids=["U1"])

    assert known == {"U1": KnownName(display_name="Maya Chen", handle="maya2")}, (
        "a handle-only sighting updates the handle and keeps the display name"
    )


async def test_upsert_flattens_names_and_skips_blank_ones(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    await upsert_user_names(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        names={"1": KnownName(display_name="  Ann\nLee "), "2": KnownName("   ", None)},
    )

    known = await get_user_names(
        db_session, tenant_id=tenant.id, platform="discord", user_ids=["1", "2"]
    )

    assert known == {"1": KnownName(display_name="Ann Lee")}, "one line; a blank name is skipped"


async def test_a_name_is_kept_per_tenant_and_platform(db_session: AsyncSession) -> None:
    one = await make_tenant(db_session, platform="slack")
    two = await make_tenant(db_session, platform="slack")
    await upsert_user_names(
        db_session, tenant_id=one.id, platform="slack", names={"U1": KnownName("One")}
    )

    assert (
        await get_user_names(db_session, tenant_id=two.id, platform="slack", user_ids=["U1"]) == {}
    )
    assert (
        await get_user_names(db_session, tenant_id=one.id, platform="teams", user_ids=["U1"]) == {}
    )


async def test_delete_and_count_reach_one_persons_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await upsert_user_names(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        names={"U1": KnownName("A"), "U2": KnownName("B")},
    )
    key = {"tenant_id": tenant.id, "platform": "slack", "platform_user_id": "U1"}

    assert await count_user_names_for_platform_user(db_session, **key) == 1
    assert await delete_user_names_for_platform_user(db_session, **key) == 1
    assert await delete_user_names_for_platform_user(db_session, **key) == 0, "idempotent"
    left = await get_user_names(
        db_session, tenant_id=tenant.id, platform="slack", user_ids=["U1", "U2"]
    )
    assert left == {"U2": KnownName("B")}, "only that person's row goes"


async def test_the_table_needs_some_name(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO platform_user_names (tenant_id, platform, platform_user_id) "
                "VALUES (:tenant, 'slack', 'U1')"
            ),
            {"tenant": tenant.id},
        )


async def test_channel_names_are_replaced_by_the_latest(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="teams")
    for name in ("Research", "Research & Dev"):
        await upsert_channel_names(
            db_session, tenant_id=tenant.id, platform="teams", names={"19:a": name, "19:b": ""}
        )

    names = await get_channel_names(
        db_session, tenant_id=tenant.id, platform="teams", channel_ids=["19:a", "19:b"]
    )

    assert names == {"19:a": "Research & Dev"}, "the latest name wins; a blank one is skipped"


async def test_deleting_a_tenant_deletes_its_names(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="teams")
    await upsert_user_names(
        db_session, tenant_id=tenant.id, platform="teams", names={"u": KnownName("A")}
    )
    await upsert_channel_names(db_session, tenant_id=tenant.id, platform="teams", names={"c": "C"})

    await delete_tenant(db_session, tenant_id=tenant.id)

    for table in ("platform_user_names", "platform_channel_names"):
        count = await db_session.scalar(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),
            {"t": tenant.id},
        )
        assert count == 0, f"{table} cascades from the tenant"


# ---- privacy erasure ----


async def test_a_privacy_purge_previews_and_deletes_the_persons_name(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="111"
    )
    await upsert_user_names(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        names={"111": KnownName("Ann", "ann"), "222": KnownName("Bo")},
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=principal.account_id)
    report = await purge_principal(
        sm=db_session_factory, principal_id=principal.id, kind="platform"
    )

    assert preview.platform_user_names.count == 1, "the preview counts their stored name"
    assert report.db.platform_user_names == 1, "the purge deletes it"
    left = await get_user_names(
        db_session, tenant_id=tenant.id, platform="discord", user_ids=["111", "222"]
    )
    assert left == {"222": KnownName("Bo")}, "someone else's name stays"


# ---- the background recorder ----


async def test_remember_writes_in_the_background_and_skips_a_repeat(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    await db_session.commit()
    opened: list[int] = []

    def counting() -> Any:
        opened.append(1)
        return db_session_factory()

    for _ in range(3):
        remember_user_name(
            counting,  # type: ignore[arg-type]
            tenant_id=tenant.id,
            platform="discord",
            user_id="1",
            display_name="Ann",
            handle="ann",
        )
    await settle()

    assert len(opened) == 1, "the same name is written once per process"
    known = await get_user_names(
        db_session, tenant_id=tenant.id, platform="discord", user_ids=["1"]
    )
    assert known == {"1": KnownName("Ann", "ann")}

    remember_user_name(
        counting,  # type: ignore[arg-type]
        tenant_id=tenant.id,
        platform="discord",
        user_id="1",
        handle="ann",
    )
    await settle()
    assert len(opened) == 1, "a sighting that changes nothing is not written"

    remember_user_name(
        counting,  # type: ignore[arg-type]
        tenant_id=tenant.id,
        platform="discord",
        user_id="1",
        display_name="Ann L.",
    )
    await settle()
    assert len(opened) == 2, "a new name is written"


async def test_outside_tests_a_write_starts_at_once_without_being_awaited(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    await db_session.commit()
    platform_names.defer_writes(False)
    try:
        remember_user_name(
            db_session_factory,
            tenant_id=tenant.id,
            platform="discord",
            user_id="1",
            display_name="Ann",
        )
        assert platform_names._pending, "a task is running; the caller did not wait"  # pyright: ignore[reportPrivateUsage]
        await settle()
    finally:
        platform_names.defer_writes()
    known = await get_user_names(
        db_session, tenant_id=tenant.id, platform="discord", user_ids=["1"]
    )
    assert known == {"1": KnownName("Ann")}


async def test_remember_channel_names_writes_in_the_background(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="teams")
    await db_session.commit()

    remember_channel_names(
        db_session_factory, tenant_id=tenant.id, platform="teams", names={"19:a": "General"}
    )
    await settle()

    names = await get_channel_names(
        db_session, tenant_id=tenant.id, platform="teams", channel_ids=["19:a"]
    )
    assert names == {"19:a": "General"}


async def test_a_failed_write_is_logged_and_tried_again_next_time(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await db_session.commit()

    @asynccontextmanager
    async def broken() -> Any:
        raise OperationalError("INSERT", {}, Exception("connection refused"))
        yield

    remember_user_name(
        broken,  # type: ignore[arg-type]
        tenant_id=tenant.id,
        platform="slack",
        user_id="U1",
        display_name="Ann",
    )
    await settle()  # no raise: the failure stays in the background task

    remember_user_name(
        db_session_factory,
        tenant_id=tenant.id,
        platform="slack",
        user_id="U1",
        display_name="Ann",
    )
    await settle()
    known = await get_user_names(db_session, tenant_id=tenant.id, platform="slack", user_ids=["U1"])
    assert known == {"U1": KnownName("Ann")}, "the failed name is written on the next sighting"


async def test_remember_ignores_what_is_not_a_name() -> None:
    def never() -> Any:
        raise AssertionError("nothing to write")

    remember_user_names(
        never,  # type: ignore[arg-type]
        tenant_id=uuid.uuid4(),
        platform="discord",
        names={"1": KnownName(None, None), "": KnownName("A"), "2": KnownName(object(), " ")},  # type: ignore[arg-type]
    )
    await settle()


# ---- the panel's resolution order ----


def _lookup(answers: dict[str, KnownName | None], *, slow: frozenset[str] = frozenset()) -> Any:
    calls: list[str] = []

    async def lookup(user_id: str) -> KnownName | None:
        calls.append(user_id)
        if user_id in slow:
            await asyncio.sleep(10)
        return answers.get(user_id)

    lookup.calls = calls  # type: ignore[attr-defined]
    return lookup


async def test_a_live_name_wins_over_the_stored_one() -> None:
    live = _lookup({"a": KnownName("Ann now", "ann")})
    labels, found = await resolve_names(["a"], live=live, stored={"a": "Ann then"}, timeout_s=1)
    assert labels == {"a": "Ann now"} and found == {"a": KnownName("Ann now", "ann")}


async def test_someone_who_left_gets_their_stored_name_and_no_handle_lookup() -> None:
    handle = _lookup({"a": KnownName(None, "ann")})
    labels, found = await resolve_names(
        ["a"], live=_lookup({}), stored={"a": "Ann"}, handle=handle, timeout_s=1
    )
    assert labels == {"a": "Ann"} and found == {}
    assert handle.calls == [], "a stored name needs no handle lookup"


async def test_someone_never_seen_gets_their_handle_and_it_is_returned_to_remember() -> None:
    handle = _lookup({"a": KnownName("Ann G.", "ann")})
    labels, found = await resolve_names(
        ["a"], live=_lookup({}), stored={}, handle=handle, timeout_s=1
    )
    assert labels == {"a": "Ann G."} and found == {"a": KnownName("Ann G.", "ann")}


async def test_a_lookup_still_waiting_at_the_timeout_gets_the_stored_name() -> None:
    live = _lookup({"a": KnownName("A"), "b": KnownName("B")}, slow=frozenset({"b"}))
    async with asyncio.timeout(2):
        labels, _ = await resolve_names(
            ["a", "b", "c"], live=live, stored={"b": "Bo (stored)"}, timeout_s=0.1
        )
    assert labels == {"a": "A", "b": "Bo (stored)"}, (
        "the slow lookup falls back to the stored name; `c`, never named, is left out"
    )


async def test_a_lookup_that_raises_falls_back_to_the_stored_name() -> None:
    async def broken(_user_id: str) -> KnownName | None:
        raise RuntimeError("boom")

    labels, found = await resolve_names(["a"], live=broken, stored={"a": "Ann"}, timeout_s=1)
    assert labels == {"a": "Ann"} and found == {}


# ---- the migration ----


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0060_platform_names.py"
    spec = importlib.util.spec_from_file_location("migration_platform_names", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.fresh_schema
async def test_the_migration_round_trips(db_session: AsyncSession) -> None:
    migration = _migration()
    conn = await db_session.connection()

    def tables(sync_conn: Connection) -> set[str]:
        rows = sync_conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name LIKE 'platform_%_names'"
            )
        )
        return {row[0] for row in rows}

    def step(name: str) -> Any:
        def run(sync_conn: Connection) -> set[str]:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, name)()
            return tables(sync_conn)

        return run

    assert await conn.run_sync(step("downgrade")) == set(), "downgrade drops both tables"
    assert await conn.run_sync(step("upgrade")) == {
        "platform_user_names",
        "platform_channel_names",
    }, "upgrade creates both"
    tenant = await make_tenant(db_session, platform="slack")
    await upsert_user_names(
        db_session, tenant_id=tenant.id, platform="slack", names={"U1": KnownName("Ann")}
    )

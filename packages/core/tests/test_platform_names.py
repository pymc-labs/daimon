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
from unittest.mock import MagicMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core import platform_names
from daimon.core._models import PlatformPrincipal
from daimon.core.platform_names import (
    KnownName,
    remember_channel_names,
    remember_user_name,
    remember_user_names,
    resolve_names,
    settle,
)
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account, purge_principal
from daimon.core.stores.identity import find_platform_principal, get_or_create_platform_principal
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


async def _people(session: AsyncSession, tenant_id: uuid.UUID, platform: str, *ids: str) -> None:
    """Give each id a principal: a name is only stored for someone who has one."""
    for external_id in ids:
        await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=external_id
        )


async def test_upsert_keeps_the_stored_part_a_later_sighting_leaves_out(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _people(db_session, tenant.id, "slack", "U1")
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
    await _people(db_session, tenant.id, "discord", "1", "2")
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


async def test_no_name_is_stored_for_someone_without_a_principal(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    await _people(db_session, tenant.id, "discord", "1")
    other = await make_tenant(db_session, platform="discord")
    await _people(db_session, other.id, "discord", "2")

    stored = await upsert_user_names(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        names={"1": KnownName("Ann"), "2": KnownName("Bo"), "3": KnownName("Cy")},
    )

    assert stored == 1
    known = await get_user_names(
        db_session, tenant_id=tenant.id, platform="discord", user_ids=["1", "2", "3"]
    )
    assert known == {"1": KnownName("Ann")}, (
        "no principal here (none at all, or only in another tenant): nothing stored"
    )


async def test_a_name_is_kept_per_tenant_and_platform(db_session: AsyncSession) -> None:
    one = await make_tenant(db_session, platform="slack")
    two = await make_tenant(db_session, platform="slack")
    for tenant in (one, two):
        await _people(db_session, tenant.id, "slack", "U1")
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
    await _people(db_session, tenant.id, "slack", "U1", "U2")
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
    await _people(db_session, tenant.id, "teams", "u")
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
    await _people(db_session, tenant.id, "discord", "222")
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


async def test_an_account_purge_deletes_its_names_in_every_tenant(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    one = await make_tenant(db_session, platform="discord")
    two = await make_tenant(db_session, platform="discord")
    first = await get_or_create_platform_principal(
        db_session, tenant_id=one.id, platform="discord", external_id="111"
    )
    # The same account holding a principal in a second tenant.
    db_session.add(
        PlatformPrincipal(
            tenant_id=two.id, platform="discord", external_id="111", account_id=first.account_id
        )
    )
    await db_session.flush()
    for tenant in (one, two):
        await upsert_user_names(
            db_session, tenant_id=tenant.id, platform="discord", names={"111": KnownName("Ann")}
        )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=first.account_id)
    report = await purge_account(sm=db_session_factory, account_id=first.account_id)

    assert preview.platform_user_names.count == 2 and report.db.platform_user_names == 2
    for tenant in (one, two):
        known = await get_user_names(
            db_session, tenant_id=tenant.id, platform="discord", user_ids=["111"]
        )
        assert known == {}, "the name goes in every tenant the account has a principal in"


async def test_someone_with_no_principal_has_no_name_and_privacy_says_so(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A first-time `/privacy` user: the click is remembered, but nothing is stored."""
    tenant = await make_tenant(db_session, platform="discord")
    await db_session.commit()

    remember_user_name(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        user_id="999",
        display_name="First Timer",
    )
    await settle()

    async with db_session_factory() as session:
        principal = await find_platform_principal(
            session, tenant_id=tenant.id, platform="discord", external_id="999"
        )
        count = await count_user_names_for_platform_user(
            session, tenant_id=tenant.id, platform="discord", platform_user_id="999"
        )
    assert principal is None, "remembering a name never mints a principal"
    assert count == 0, "so `/privacy`'s 'no data on file' is true: no name was stored"


async def test_a_purge_drops_a_queued_name_so_it_is_not_written_after(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="111"
    )
    await db_session.commit()
    remember_user_name(
        db_session_factory, tenant_id=tenant.id, platform="discord", user_id="111", display_name="A"
    )
    assert platform_names.pending_count() == 1

    await purge_principal(sm=db_session_factory, principal_id=principal.id, kind="platform")
    await settle()

    assert platform_names.pending_count() == 0, "the purge dropped the queued name"
    count = await count_user_names_for_platform_user(
        db_session, tenant_id=tenant.id, platform="discord", platform_user_id="111"
    )
    assert count == 0


async def test_a_write_racing_a_purge_cannot_put_the_name_back(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Even from another process: with the principal gone, the write stores nothing."""
    tenant = await make_tenant(db_session, platform="discord")
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="111"
    )
    await db_session.commit()
    await purge_principal(sm=db_session_factory, principal_id=principal.id, kind="platform")

    async with db_session_factory() as session, session.begin():
        stored = await upsert_user_names(
            session, tenant_id=tenant.id, platform="discord", names={"111": KnownName("A")}
        )

    assert stored == 0


# ---- the background writer ----


async def test_remember_writes_once_and_skips_a_repeat(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    await _people(db_session, tenant.id, "discord", "1")
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

    assert len(opened) == 1, "the same name is written once"
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


async def test_a_later_sighting_replaces_a_queued_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    await _people(db_session, tenant.id, "discord", "1")
    await db_session.commit()

    for name in ("A", "B"):
        remember_user_name(
            db_session_factory,
            tenant_id=tenant.id,
            platform="discord",
            user_id="1",
            display_name=name,
        )
    assert platform_names.pending_count() == 1, "one key, latest value"
    await settle()

    known = await get_user_names(
        db_session, tenant_id=tenant.id, platform="discord", user_ids=["1"]
    )
    assert known == {"1": KnownName("B")}, "the latest name is the one written"


async def test_a_burst_of_people_is_bounded_and_written_by_one_consumer_in_batches(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, platform="discord")
    ids = [str(n) for n in range(130)]
    await _people(db_session, tenant.id, "discord", *ids)
    await db_session.commit()
    monkeypatch.setattr(platform_names, "MAX_PENDING", 120)
    sessions: list[int] = []
    rows_per_batch: list[int] = []
    real = platform_names.upsert_user_names

    async def counting_upsert(session: AsyncSession, **kwargs: Any) -> int:
        rows_per_batch.append(len(kwargs["names"]))
        return await real(session, **kwargs)

    monkeypatch.setattr(platform_names, "upsert_user_names", counting_upsert)

    def counting() -> Any:
        sessions.append(1)
        return db_session_factory()

    platform_names.defer_writes(False)
    try:
        for user_id in ids:
            remember_user_name(
                counting,  # type: ignore[arg-type]
                tenant_id=tenant.id,
                platform="discord",
                user_id=user_id,
                display_name=f"p{user_id}",
            )
        assert platform_names.pending_count() == 120, "keys past the cap are dropped"
        consumers = [t for t in asyncio.all_tasks() if t.get_name() == "platform_names.writer"]
        assert len(consumers) == 1, "one consumer, however many people"
        await settle()
    finally:
        platform_names.defer_writes()

    assert rows_per_batch == [50, 50, 20], "batches of at most 50 rows"
    assert len(sessions) == 3, "one session per batch"
    known = await get_user_names(db_session, tenant_id=tenant.id, platform="discord", user_ids=ids)
    assert len(known) == 120


async def test_remember_channel_names_are_written(
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


async def test_a_failed_write_is_not_remembered_so_the_next_sighting_retries(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _people(db_session, tenant.id, "slack", "U1")
    await db_session.commit()

    @asynccontextmanager
    async def broken() -> Any:
        raise OperationalError("INSERT", {}, Exception("connection refused"))
        yield

    for kind in ("user", "channel"):
        if kind == "user":
            remember_user_name(
                broken,  # type: ignore[arg-type]
                tenant_id=tenant.id,
                platform="slack",
                user_id="U1",
                display_name="Ann",
            )
        else:
            remember_channel_names(
                broken,  # type: ignore[arg-type]
                tenant_id=tenant.id,
                platform="slack",
                names={"C1": "general"},
            )
    await settle()  # no raise: the failure stays in the writer

    remember_user_name(
        db_session_factory,
        tenant_id=tenant.id,
        platform="slack",
        user_id="U1",
        display_name="Ann",
    )
    remember_channel_names(
        db_session_factory, tenant_id=tenant.id, platform="slack", names={"C1": "general"}
    )
    assert platform_names.pending_count() == 2, "neither was remembered as written"
    await settle()
    known = await get_user_names(db_session, tenant_id=tenant.id, platform="slack", user_ids=["U1"])
    assert known == {"U1": KnownName("Ann")}, "the failed name is written on the next sighting"
    channels = await get_channel_names(
        db_session, tenant_id=tenant.id, platform="slack", channel_ids=["C1"]
    )
    assert channels == {"C1": "general"}


async def test_remember_ignores_what_is_not_a_name() -> None:
    remember_user_names(
        MagicMock(side_effect=AssertionError("nothing to write")),
        tenant_id=uuid.uuid4(),
        platform="discord",
        names={"1": KnownName(None, None), "": KnownName("A"), "2": KnownName(object(), " ")},  # type: ignore[arg-type]
    )
    assert platform_names.pending_count() == 0


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
    await _people(db_session, tenant.id, "slack", "U1")
    stored = await upsert_user_names(
        db_session, tenant_id=tenant.id, platform="slack", names={"U1": KnownName("Ann")}
    )
    assert stored == 1, "the upgraded table takes a name"

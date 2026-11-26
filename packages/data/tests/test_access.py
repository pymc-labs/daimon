"""Row access: tenancy, the reader role, the security definers and the access lists."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from daimon.data import acl, reads
from daimon.data.access import ACL_SCOPE, READER_CAP, ReadContext, person, scoped, subject
from daimon.data.schema import Schemas
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow

DEFINERS = {
    "visible_sources",
    "scopes",
    "audience_key",
    "visible_acls",
    "lapsed_record_ids",
    "visible_fraction",
}
CALLER_SQL = "caller_sql"
"""The one definer that takes arguments: the text of a caller's query (tested in test_sync)."""
PARITY = "parity_visible"
"""Takes a source and a person; only the parity role may run it (`0005_grants.sql`)."""


def now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    schemas: Schemas
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.schemas.data

    async def rows(self, sql: str, params: Sequence[object] = ()) -> list[DictRow]:
        return await (await self.conn.execute(sql.encode(), params)).fetchall()

    async def one(self, sql: str, params: Sequence[object] = ()) -> object:
        found = await self.rows(sql, params)
        return next(iter(found[0].values())) if found else None

    async def source(
        self,
        tenant: uuid.UUID | None = None,
        *,
        publication: str = "published",
        group: str | None = None,
    ) -> uuid.UUID:
        found = await self.one(
            f"insert into {self.d}.sources (tenant_id, kind, name, provider, default_scope, publication,"
            " source_group) values (%s, 'docs', 'docs', 'acme', %s, %s, %s) returning id",
            (tenant or self.tenant, ACL_SCOPE, publication, group),
        )
        assert isinstance(found, uuid.UUID), "a source insert returns its id"
        return found

    async def put(
        self,
        src: uuid.UUID,
        ext: str,
        acl_id: int | None = None,
        *,
        scope: str = ACL_SCOPE,
        until: datetime | None = None,
    ) -> uuid.UUID:
        """A record and one chunk; with `acl_id`, given that list through the one writer."""
        async with self.conn.transaction():
            rec = await self.one(
                f"insert into {self.d}.records (source_id, external_id, kind, title, body, occurred_at, scope)"
                " values (%s, %s, 'doc', 'fixture', 'fixture', now(), %s) returning id",
                (src, ext, scope),
            )
            assert isinstance(rec, uuid.UUID), "a record insert returns its id"
            await self.conn.execute(
                f"insert into {self.d}.chunks (record_id, source_id, ord, text, scope)"
                " values (%s, %s, 0, 'fixture', %s)".encode(),
                (rec, src, scope),
            )
            if acl_id is not None:
                await acl.set_records(
                    self.conn,
                    self.schemas,
                    src,
                    [{"ext": ext, "acl": acl_id, "until": until}],
                    now(),
                )
        return rec

    async def intern(
        self,
        src: uuid.UUID,
        *refs: acl.Ref,
        all_of: list[int] | None = None,
        personal: bool = False,
    ) -> int:
        async with self.conn.transaction():
            return await acl.intern(
                self.conn, self.schemas, src, refs, all_of=all_of, personal=personal
            )

    def who(
        self, *readers: str, personal_ok: bool = True, limits: tuple[int, ...] | None = None
    ) -> ReadContext:
        return ReadContext((self.tenant,), readers=readers, personal_ok=personal_ok, limits=limits)

    async def seen(self, ctx: ReadContext, table: str = "records") -> set[uuid.UUID]:
        col = "record_id" if table == "chunks" else "id"
        async with scoped(self.conn, self.schemas, ctx) as c:
            got = await (
                await c.execute(f"select {col} as id from {self.d}.{table}".encode())
            ).fetchall()
        return {r["id"] for r in got}

    async def reads(self, ctx: ReadContext, record: uuid.UUID) -> bool:
        return record in await self.seen(ctx)

    async def epoch(self) -> int:
        n = await self.one(f"select n from {self.d}.acl_epoch")
        assert isinstance(n, int), "the epoch is one row"
        return n


@pytest.fixture
async def store(data_db: DataDb) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        yield Store(conn, data_db.schemas, uuid.uuid4())


def someone() -> str:
    return person(uuid.uuid4())


# ---- tenancy and the reader role ---------------------------------------------------------------


async def test_the_reader_sees_nothing_without_a_context_of_this_transaction(store: Store) -> None:
    src = await store.source()
    rec = await store.put(src, "doc:public", scope="public")
    ctx = ReadContext((store.tenant,), scopes=("public",))
    assert await store.reads(ctx, rec), "a context with the scope reads the row"
    async with store.conn.transaction():  # the row the last context left applies to it only
        await store.conn.execute(b"set local role daimon_data_reader")
        for table in ("sources", "records", "chunks"):
            n = await store.one(f"select count(*) from {store.d}.{table}")
            assert n == 0, f"{table} shows rows without a context"
        for fn in DEFINERS:
            got = await store.rows(f"select * from {store.d}.{fn}()")
            assert all(v in (None, 0, []) for r in got for v in r.values()), (
                f"{fn} answers without a context"
            )


async def test_another_tenants_source_is_never_visible_in_rows_or_through_any_definer(
    store: Store,
) -> None:
    """Both tenants list the same subject: evidence and lists of one never reach the other."""
    reader = subject("acme", "w", "user", "u1")
    other = uuid.uuid4()
    ids: dict[uuid.UUID, set[object]] = {}
    for tenant in (store.tenant, other):
        src = await store.source(tenant)
        leaf = await store.intern(src, acl.ref(reader))
        unit = await store.one(
            f"insert into {store.d}.evidence_units (source_id, kind, ref) values (%s, 'pass', 'g') returning id",
            (src,),
        )
        async with store.conn.transaction():
            await acl.publish(
                store.conn, store.schemas, int(str(unit)), 1, {"acme/w:group:g": [reader]}
            )
        group = await store.intern(src, acl.ref("acme/w:group:g", group=True))
        recs = {
            await store.put(src, "doc:public", scope="public"),
            await store.put(src, "doc:leaf", leaf),
            await store.put(src, "doc:group", group),
        }
        held = await store.put(src, "doc:held", leaf)
        async with store.conn.transaction():
            await acl.set_records(
                store.conn, store.schemas, src, [{"ext": "doc:held", "acl": 0, "held": True}], now()
            )
        ids[tenant] = {src, leaf, group, held, *recs}
    ctx = ReadContext((other,), scopes=("public",), readers=(reader,))
    for table in ("sources", "records", "chunks"):
        seen = await store.seen(ctx, table)
        assert seen and not seen & ids[store.tenant], f"{table} shows another tenant's rows"
    assert len(await store.seen(ctx)) == 3, "the context tenant's public, leaf and group rows show"
    definers = await store.rows(
        "select p.proname as name, p.prorettype::regtype::text as type, p.pronargs as args"
        " from pg_proc p where p.pronamespace = %s::regnamespace and p.prosecdef",
        (store.d,),
    )
    assert {r["name"] for r in definers} == DEFINERS | {CALLER_SQL, PARITY}, (
        "a new definer needs a case here"
    )
    async with scoped(store.conn, store.schemas, ctx) as c:
        for fn in (f for f in definers if f["name"] not in (CALLER_SQL, PARITY)):
            assert fn["args"] == 0, f"{fn['name']} takes arguments: call it here with hostile ones"
            got = [
                v
                for r in await (
                    await c.execute(f"select * from {store.d}.{fn['name']}()".encode())
                ).fetchall()
                for v in r.values()
            ]
            if fn["type"] in ("uuid", "integer"):
                assert not set(got) & ids[store.tenant], (
                    f"{fn['name']} returns another tenant's ids"
                )
                assert set(got) <= ids[other] | {0}, f"{fn['name']} returns ids outside the context"
            elif fn["type"] == "text[]":
                assert got == [["public"]], f"{fn['name']} returns more than the context's scopes"
            else:
                assert fn["type"] in ("text", "double precision"), f"{fn['name']}: unchecked type"


async def test_caller_sql_never_reads_a_records_payload(store: Store) -> None:
    """Ingest redaction never touches the payload, so the reader views leave it out."""
    cols = await store.rows(
        "select column_name from information_schema.columns"
        " where table_schema = %s and table_name = 'records'",
        (store.schemas.views,),
    )
    assert cols and "payload" not in {c["column_name"] for c in cols}


async def test_a_shadow_retired_or_disabled_source_shows_nothing(store: Store) -> None:
    ctx = ReadContext((store.tenant,), scopes=("public",))
    shadow = await store.put(await store.source(publication="shadow"), "doc:1", scope="public")
    retired = await store.put(await store.source(publication="retired"), "doc:1", scope="public")
    disabled_src = await store.source()
    disabled = await store.put(disabled_src, "doc:1", scope="public")
    await store.conn.execute(
        f"update {store.d}.sources set status = 'disabled' where id = %s".encode(), (disabled_src,)
    )
    assert not await store.seen(ctx) & {shadow, retired, disabled}, "an unpublished source shows"


async def test_the_reader_runs_only_its_definers_and_never_reads_lists_or_credentials(
    store: Store,
) -> None:
    fns = await store.rows(
        "select p.oid::regprocedure::text as fn, p.proname as name,"
        " has_function_privilege('daimon_data_reader', p.oid, 'execute') as ok"
        " from pg_proc p where p.pronamespace = %s::regnamespace",
        (store.d,),
    )
    assert {r["name"] for r in fns if r["ok"]} == DEFINERS | {CALLER_SQL}, (
        "the reader runs an owner's function"
    )
    async with scoped(store.conn, store.schemas, store.who(someone())) as c:
        for q in (
            "select publish(1, 1)",
            "select intern_acl(gen_random_uuid(), '[]')",
            "select fill_audience()",
            "select set_record_acl(gen_random_uuid(), 'x', 0, now())",
            "select credential from sources",
            "select config from sources",
            *(
                f"select * from {t}"
                for t in ("audience_sets", "audience_acls", "acl_grants", "holder_sets", "acls")
                + ("read_contexts", "record_access", "group_members", "grants", "audit")
            ),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with c.transaction():
                    await c.execute(f"set local search_path = {store.d}; {q}".encode())


# ---- access lists (ported) -------------------------------------------------------------------------


async def test_publish_replaces_a_units_batch_and_discards_an_older_pass(store: Store) -> None:
    src = await store.source()
    holders = f"select holder from {store.d}.acl_grants where acl_id = %s order by 1"
    async with store.conn.transaction():
        u = await acl.unit(store.conn, store.schemas, src, "pass", "groups")
        leaf = await acl.intern(
            store.conn, store.schemas, src, [acl.ref("a/w:group:a", group=True)]
        )
        assert await acl.publish(
            store.conn, store.schemas, u, 5, {"a/w:group:a": ["a/w:user:1", "a/w:user:2"]}
        )
        assert [r["holder"] for r in await store.rows(holders, (leaf,))] == [
            "a/w:user:1",
            "a/w:user:2",
        ]
    before = await store.epoch()
    async with store.conn.transaction():
        assert not await acl.publish(store.conn, store.schemas, u, 5, {}), (
            "the same seq is discarded"
        )
        assert not await acl.publish(store.conn, store.schemas, u, 4, {}), (
            "an older seq is discarded"
        )
        assert len(await store.rows(holders, (leaf,))) == 2, "a discarded pass changed grants"
        assert await acl.publish(
            store.conn, store.schemas, u, 6, {"a/w:group:a": ["a/w:user:1", "a/w:user:2"]}
        )
    assert await store.epoch() == before, "an unchanged pass only renews"
    async with store.conn.transaction():
        assert await acl.publish(store.conn, store.schemas, u, 7, {})
        assert await store.rows(holders, (leaf,)) == [], (
            "a group missing from the batch keeps members"
        )
    assert await store.epoch() > before, "a change moves the epoch at commit"


async def test_a_new_list_gets_existing_members_and_a_group_of_another_source_grants_nothing(
    store: Store,
) -> None:
    me, you = someone(), someone()
    src, elsewhere = await store.source(), await store.source()
    async with store.conn.transaction():
        u = await acl.unit(store.conn, store.schemas, src, "pass", "groups")
        await acl.publish(store.conn, store.schemas, u, 1, {"a/w:group:b": [me]})
    assert not await store.seen(store.who(me)), "nothing listed yet"
    before = await store.epoch()
    rec = await store.put(src, "doc:1", await store.intern(src, acl.ref("a/w:group:b", group=True)))
    foreign = await store.put(
        elsewhere, "doc:1", await store.intern(elsewhere, acl.ref("a/w:group:b", group=True))
    )
    assert await store.epoch() > before, "a list that grants a known holder moves the epoch"
    assert await store.seen(store.who(me)) == {rec}, "only the unit's own source's list grants"
    assert not await store.reads(store.who(you), rec), "a non-member reads the record"
    assert foreign


async def test_group_membership_counts_within_a_source_group_and_never_outside_it(
    store: Store,
) -> None:
    me = someone()
    reader, partner = await store.source(group="g"), await store.source(group="g")
    outsider = await store.source(group="h")
    async with store.conn.transaction():
        u = await acl.unit(store.conn, store.schemas, reader, "pass", "groups")
        await acl.publish(store.conn, store.schemas, u, 1, {"a/w:group:b": [me]})
    grp = acl.ref("a/w:group:b", group=True)
    shared = await store.put(partner, "doc:1", await store.intern(partner, grp))
    foreign = await store.put(outsider, "doc:1", await store.intern(outsider, grp))
    assert await store.seen(store.who(me)) == {shared}, "only a source of the unit's group grants"
    await store.conn.execute(
        f"update {store.d}.sources set source_group = null where id = %s".encode(), (partner,)
    )
    await store.conn.execute(
        f"update {store.d}.sources set source_group = 'g' where id = %s".encode(), (outsider,)
    )
    assert await store.seen(store.who(me)) == {foreign}, "leaving or joining a group counts at once"
    async with store.conn.transaction():
        assert set((await acl.nightly(store.conn, store.schemas)).values()) == {0}, "drift"


async def test_two_paths_to_one_list_keep_the_valid_one_and_expiry_needs_no_job(
    store: Store,
) -> None:
    me = someone()
    src = await store.source()
    async with store.conn.transaction():
        a = await acl.unit(store.conn, store.schemas, src, "person", "a")
        b = await acl.unit(store.conn, store.schemas, src, "person", "b")
        await acl.publish(store.conn, store.schemas, a, 1)
        await acl.publish(store.conn, store.schemas, b, 1)
    rec = await store.put(
        src, "doc:two", await store.intern(src, acl.ref(me, unit_id=a), acl.ref(me, unit_id=b))
    )
    expire = f"update {store.d}.evidence_units set expires_at = now() + %s where id = %s"
    assert await store.reads(store.who(me), rec)
    await store.conn.execute(expire.encode(), (timedelta(seconds=-1), a))
    assert await store.reads(store.who(me), rec), "the other path lives"
    await store.conn.execute(expire.encode(), (timedelta(seconds=-1), b))
    assert not await store.reads(store.who(me), rec), "both paths expired"
    await store.conn.execute(expire.encode(), (timedelta(hours=1), b))
    assert await store.reads(store.who(me), rec), "a renewal shows at the next read"


async def test_a_ref_on_another_sources_unit_is_refused(store: Store) -> None:
    src, elsewhere = await store.source(), await store.source()
    async with store.conn.transaction():
        u = await acl.unit(store.conn, store.schemas, elsewhere, "person", "a")
    with pytest.raises(psycopg.errors.RaiseException):
        await store.intern(src, acl.ref(someone(), unit_id=u))


async def test_a_records_own_expiry_and_a_newer_read_renews_it(store: Store) -> None:
    me = someone()
    src = await store.source()
    leaf = await store.intern(src, acl.ref(me))
    rec = await store.put(src, "doc:until", leaf, until=now() - timedelta(seconds=1))
    assert not await store.reads(store.who(me), rec), "a lapsed lease shows"
    async with store.conn.transaction():
        await acl.set_records(
            store.conn,
            store.schemas,
            src,
            [{"ext": "doc:until", "acl": leaf, "until": now() + timedelta(hours=1)}],
            now(),
        )
    assert await store.reads(store.who(me), rec), "a renewed lease hides"


async def test_a_stale_read_does_not_repoint_and_a_hold_is_released_only_by_a_later_read(
    store: Store,
) -> None:
    me = someone()
    src = await store.source()
    mine = await store.intern(src, acl.ref(me))
    nobody = await store.intern(src, acl.ref(someone()))
    rec = await store.put(src, "doc:order", mine)

    async def write(acl_id: int, at: datetime, held: bool = False) -> None:
        async with store.conn.transaction():
            await acl.set_records(
                store.conn,
                store.schemas,
                src,
                [{"ext": "doc:order", "acl": acl_id, "held": held}],
                at,
            )

    await write(nobody, now() - timedelta(hours=1))
    assert await store.reads(store.who(me), rec), "an older read repointed the list"
    held_at = now()
    await write(mine, held_at, held=True)
    assert not await store.reads(store.who(me), rec), "a held record shows"
    await write(mine, held_at)
    assert not await store.reads(store.who(me), rec), "an equal timestamp released the hold"
    await write(mine, held_at + timedelta(seconds=1))
    assert await store.reads(store.who(me), rec), "a later read keeps the hold"
    assert set((await acl.copies_differ(store.conn, store.schemas)).values()) == {0}


async def test_a_personal_list_needs_personal_ok_and_a_conjunction_fails_without_it(
    store: Store,
) -> None:
    me, you = someone(), someone()
    src = await store.source()
    private = await store.intern(src, acl.ref(me), personal=True)
    team = await store.intern(src, acl.ref(me), acl.ref(you))
    narrowed = await store.intern(src, all_of=[private, team])
    recs = {
        await store.put(src, "doc:private", private),
        await store.put(src, "doc:narrowed", narrowed),
    }
    assert await store.seen(store.who(me)) == recs
    assert not await store.seen(store.who(me, personal_ok=False)), (
        "an agent for others reads a private list"
    )


async def test_a_conjunction_of_another_sources_leaves_is_refused(store: Store) -> None:
    src, elsewhere = await store.source(), await store.source()
    mine = await store.intern(src, acl.ref(someone()))
    theirs = await store.intern(elsewhere, acl.ref(someone()))
    with pytest.raises(psycopg.errors.RaiseException):
        await store.intern(src, all_of=[mine, theirs])
    with pytest.raises(psycopg.errors.RaiseException):
        await store.put(src, "doc:x", theirs)


async def test_empty_unknown_malformed_and_over_cap_audiences_see_nothing(store: Store) -> None:
    me = someone()
    src = await store.source()
    public = await store.put(src, "doc:public", await store.intern(src, acl.ref("*")))
    crowd = tuple(subject("s", "w", "user", str(n)) for n in range(READER_CAP + 1))
    assert await store.reads(store.who(me), public)
    assert not await store.reads(store.who(), public), "no reader reads a list"
    assert not await store.reads(store.who(*crowd), public), "an audience over the cap reads"
    assert await store.reads(store.who(*crowd[:READER_CAP]), public), "an audience at the cap reads"
    for bad in (
        "someone",
        "account:not-a-uuid",
        "*",
        "provider/ws:user",
        "provider:user:x",
        "a/b:c:d e",
    ):
        assert not await store.reads(store.who(bad), public), bad
        assert not await store.reads(store.who(me, bad), public), f"{bad} next to a person"


async def test_an_audience_is_the_intersection_and_limits_narrow_it(store: Store) -> None:
    me, you, third = someone(), someone(), someone()
    src = await store.source()
    both = await store.intern(src, acl.ref(me), acl.ref(you))
    one = await store.intern(src, acl.ref(me))
    r_both, r_one = await store.put(src, "doc:both", both), await store.put(src, "doc:one", one)
    assert await store.seen(store.who(me)) == {r_both, r_one}
    assert await store.seen(store.who(me, you)) == {r_both}, "two readers see what both hold"
    assert not await store.seen(store.who(me, third)), "a reader holding nothing empties it"
    assert await store.seen(store.who(me, limits=(both,))) == {r_both}, "limits do not narrow"


async def test_the_guard_keeps_lists_to_their_writer_and_chunks_follow_their_record(
    store: Store,
) -> None:
    src = await store.source()
    leaf = await store.intern(src, acl.ref(someone()))
    rec = await store.put(src, "doc:guard", leaf)
    for sql in (
        f"update {store.d}.records set acl = null where id = %s",
        f"update {store.d}.records set acl = 0 where id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            await store.conn.execute(sql.encode(), (rec,))
    with pytest.raises(psycopg.errors.RaiseException):
        await store.conn.execute(
            f"insert into {store.d}.records (source_id, external_id, kind, occurred_at, scope, acl)"
            " values (%s, 'doc:direct', 'doc', now(), 'internal', %s)".encode(),
            (src, leaf),
        )
    await store.conn.execute(
        f"update {store.d}.chunks set acl = null, scope = 'public', source_id = source_id where record_id = %s".encode(),
        (rec,),
    )
    await store.conn.execute(
        f"update {store.d}.records set title = 'renamed' where id = %s".encode(), (rec,)
    )
    chunk = await store.rows(
        f"select scope, acl from {store.d}.chunks where record_id = %s", (rec,)
    )
    assert chunk == [{"scope": ACL_SCOPE, "acl": leaf}], "a chunk left its record's list"


async def test_the_nightly_rebuild_finds_nothing_then_finds_and_repairs_a_corrupted_grant(
    store: Store,
) -> None:
    src = await store.source()
    async with store.conn.transaction():
        u = await acl.unit(store.conn, store.schemas, src, "pass", "n")
        await acl.publish(
            store.conn, store.schemas, u, 1, {"a/w:group:n": ["a/w:user:n1", "a/w:user:n2"]}
        )
    leaf = await store.intern(src, acl.ref("a/w:group:n", group=True), acl.ref("a/w:user:direct"))
    await store.put(src, "doc:nightly", leaf)
    async with store.conn.transaction():
        clean = await acl.nightly(store.conn, store.schemas)
        assert set(clean.values()) == {0}, f"a clean store has drift: {clean}"
        await store.conn.execute(
            f"set local daimon_data.acl_maint = on; delete from {store.d}.acl_grants where holder = 'a/w:user:n1';"
            f" insert into {store.d}.acl_grants (holder, acl_id) values ('a/w:user:extra', {leaf})".encode()
        )
        found = await acl.nightly(store.conn, store.schemas)
        assert (found["grant_missing"], found["grant_extra"]) == (1, 1), f"drift missed: {found}"
        assert (await acl.nightly(store.conn, store.schemas))["grant_missing"] == 0, "not repaired"
    async with store.conn.transaction(force_rollback=True):  # the worker's later tests share it
        await store.conn.execute(
            f"set local daimon_data.acl_writer = on; update {store.d}.records set acl = 0"
            f" where source_id = '{src}' and external_id = 'doc:nightly'".encode()
        )
        assert (await acl.nightly(store.conn, store.schemas))["copies:records"] == 1, (
            "a drifted copy"
        )


async def test_deleting_a_source_ends_access_at_the_next_read_with_no_job(store: Store) -> None:
    me = someone()
    src, kept = await store.source(), await store.source()
    rec = await store.put(src, "doc:1", await store.intern(src, acl.ref(me)))
    other = await store.put(kept, "doc:1", await store.intern(kept, acl.ref(me)))
    assert await store.seen(store.who(me)) == {rec, other}
    await store.conn.execute(f"delete from {store.d}.sources where id = %s".encode(), (src,))
    assert await store.seen(store.who(me)) == {other}, "a deleted source's grants outlived it"
    grants = await store.one(f"select count(*) from {store.d}.acl_grants where holder = %s", (me,))
    assert grants == 1, "the deleted source's grants stay"


async def test_the_set_helpers_sort_dedupe_and_intersect(store: Store) -> None:
    got = await store.rows(
        f"select {store.d}.arr_sort('{{5,1,3,1,5}}') as s, {store.d}.arr_and('{{1,2,3,4}}', '{{3,4,5}}') as a,"
        f" {store.d}.arr_and('{{1}}', '{{}}') as e, {store.d}.arr_sort('{{}}') as z"
    )
    assert got == [{"s": [1, 3, 5], "a": [3, 4], "e": [], "z": []}]


async def test_a_memorys_data_is_readable_only_if_every_item_is(store: Store) -> None:
    """A memory recorded these items (`daimon.core.stores.data_memory`); fail closed."""
    me, you = someone(), someone()
    src = await store.source()
    mine = await store.put(src, "doc:me", await store.intern(src, acl.ref(me)))
    both = await store.put(src, "doc:both", await store.intern(src, acl.ref(me), acl.ref(you)))
    refs = [f"record:{mine}", f"record:{both}"]

    async def readable(ctx: ReadContext, items: list[str]) -> bool:
        return await reads.readable(store.conn, store.schemas, ctx, items)

    assert await readable(store.who(me), refs), "every item is readable"
    assert not await readable(store.who(me, you), refs), "one item is hidden from someone"
    for unknown in ("live:" + str(src), "sql", "record:nope"):
        assert not await readable(store.who(me), [unknown]), f"{unknown} cannot be told"
    own = ["person:" + me.removeprefix("account:"), *refs]
    assert await readable(store.who(me), own), "a person's own data: by them alone"
    assert not await readable(store.who(me, you), own), "never with anyone else"
    await store.conn.execute(f"delete from {store.d}.records where id = %s".encode(), (mine,))
    assert not await readable(store.who(me), refs), "a deleted item hides the memory"

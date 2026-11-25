"""What the store registers with core: erasure statements and the `data` audit source."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from daimon.core import ports
from daimon.core.ports import erasure as erasure_port
from daimon.core.ports.audit import AuditFilter
from daimon.data import audit
from daimon.data.audit import AUDIT_SOURCE, record_audit
from daimon.data.ports import erasure
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


async def _count(conn: AsyncConnection[DictRow], sql: str, params: Sequence[object]) -> int:
    row = await (await conn.execute(f"select count(*) as n from {sql}".encode(), params)).fetchone()
    return int(row["n"]) if row else -1


async def test_erasure_removes_a_persons_data_and_a_tenant_purge_removes_the_tenants(
    data_db: DataDb, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports.load()
    monkeypatch.setattr(erasure_port, "_REGISTERED", {s.key: s for s in erasure(data_db.schemas)})
    d, tenant, me, you = data_db.schemas.data, uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with data_db.aconnect() as conn:
        for owner in (me, None):
            row = await (
                await conn.execute(
                    f"insert into {d}.sources (tenant_id, kind, name, default_scope, owner_account_id, created_by)"
                    " values (%s, 'docs', 'x', 'internal', %s, %s) returning id".encode(),
                    (tenant, owner, me),
                )
            ).fetchone()
            assert row, "a source insert returns its id"
            await conn.execute(
                f"insert into {d}.records (source_id, external_id, kind, occurred_at, scope)"
                " values (%s, 'doc:1', 'doc', now(), 'internal')".encode(),
                (row["id"],),
            )
        await conn.execute(
            f"insert into {d}.scopes (tenant_id, id, kind, name) values (%s, 'internal', 'internal', 'x')".encode(),
            (tenant,),
        )
        await conn.execute(
            f"insert into {d}.grants (tenant_id, scope, holder, granted_by) values"
            " (%s, 'internal', %s, %s), (%s, 'internal', %s, %s)".encode(),
            (tenant, f"account:{me}", you, tenant, f"account:{you}", me),
        )
        for actor in (me, you):
            await record_audit(
                conn,
                data_db.schemas,
                tenant_id=tenant,
                kind="read",
                account_id=actor,
                target=f"account:{me}",
                detail={"q": "x"},
            )
        async with AsyncSession(db_engine) as session, session.begin():
            assert await erasure_port.erase_account(session, account_id=me) > 0, "nothing erased"
        assert await _count(conn, f"{d}.sources where owner_account_id = %s", (me,)) == 0
        assert (
            await _count(
                conn,
                f"{d}.records r join {d}.sources s on s.id = r.source_id where s.tenant_id = %s",
                (tenant,),
            )
            == 1
        ), "a shared source's records go"
        assert await _count(conn, f"{d}.sources where created_by = %s", (me,)) == 0
        assert (
            await _count(
                conn, f"{d}.grants where holder = %s or granted_by = %s", (f"account:{me}", me)
            )
            == 0
        )
        assert await _count(conn, f"{d}.grants where tenant_id = %s", (tenant,)) == 1, (
            "another's grant goes"
        )
        trail = f"{d}.audit where tenant_id = %s and (account_id = %s or target is not null or detail <> '{{}}')"
        assert await _count(conn, trail, (tenant, me)) == 0, "the trail keeps the person"
        assert await _count(conn, f"{d}.audit where account_id = %s", (you,)) == 1, (
            "another's trail goes"
        )
        async with AsyncSession(db_engine) as session, session.begin():
            await erasure_port.erase_tenant(session, tenant_id=tenant)
        for table in ("sources", "scopes", "grants", "audit"):
            assert await _count(conn, f"{d}.{table} where tenant_id = %s", (tenant,)) == 0, table


async def test_the_audit_source_reads_the_tenants_trail_newest_first_and_filters(
    data_db: DataDb, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit, "load_data_settings", lambda: data_db.settings)
    tenant, me = uuid.uuid4(), uuid.uuid4()
    async with data_db.aconnect() as conn:
        for kind in ("connect", "read", "read"):
            await record_audit(conn, data_db.schemas, tenant_id=tenant, kind=kind, account_id=me)
        await record_audit(conn, data_db.schemas, tenant_id=uuid.uuid4(), kind="other")
    async with AsyncSession(db_engine) as session:
        rows = await AUDIT_SOURCE.read(session, tenant, AuditFilter(), None, 10)
        assert [r.kind for r in rows] == ["read", "read", "connect"], (
            "newest first, this tenant only"
        )
        assert {r.source for r in rows} == {"data"} and rows[0].actor_account_id == me
        page = await AUDIT_SOURCE.read(
            session, tenant, AuditFilter(kind="read"), rows[0].cursor, 10
        )
        assert [r.id for r in page] == [rows[1].id], "the cursor and the kind filter apply"
        assert await AUDIT_SOURCE.kinds(session, tenant) == ["connect", "read"]
    monkeypatch.setattr(
        audit,
        "load_data_settings",
        lambda: data_db.settings.model_copy(update={"schema_name": "nowhere"}),
    )
    async with AsyncSession(db_engine) as session:
        assert await AUDIT_SOURCE.read(session, tenant, AuditFilter(), None, 10) == [], (
            "no store, no rows"
        )

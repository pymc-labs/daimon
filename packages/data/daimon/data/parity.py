"""Parity: what Daimon would show each linked person of a source, against the source itself.

A source whose connector implements `can_open` has an oracle. `compare` samples records and,
for each linked person, asks the source whether they may open each one, then sets that
against `parity_visible`, which evaluates Daimon's own access rule with the source counted as
published (so a source in shadow can be compared before anyone reads it). It runs as role
`daimon_data_parity` in a read-only transaction and writes nothing but its run row.

- wrong-more: Daimon shows a record the source hides from that person. It blocks publishing.
- wrong-less: Daimon hides a record the source shows. It only warns.
- A check the source could not answer is no verdict, never a pass: the run is not clean.

`publish` moves a source from shadow to published only on a clean, recent run at its current
generation (a source whose kind has no oracle needs a complete sync instead). A published
source is compared again daily; a wrong-more there is audited and logged as an error.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import psycopg
import structlog
from daimon.data import audit
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS, Source
from daimon.data.sync.engine import Env, build_source, with_people
from psycopg import AsyncConnection
from psycopg.rows import DictRow

log = structlog.get_logger()

PEOPLE: Final = 25
"""Linked people compared in one run, drawn at random."""
SAMPLE: Final = 40
"""Records checked per person: that many of what Daimon shows them, and as many drawn at random."""
CHECKS: Final = 8
"""Questions to the source in flight at once."""
MAX_AGE: Final = timedelta(hours=36)
"""The newest clean run must be younger than this to publish on."""
AGAIN_CLEAN: Final = timedelta(hours=23)
AGAIN_FAILED: Final = timedelta(hours=1)


@dataclass(frozen=True)
class Run:
    people: int = 0
    checked: int = 0
    wrong_more: int = 0
    wrong_less: int = 0
    unresolved: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.unresolved is None
            and self.people > 0
            and self.checked > 0
            and self.wrong_more == 0
        )


@dataclass(frozen=True)
class Gate:
    """Whether a source may be published, and if not why (counts only, no names or ids)."""

    met: bool
    reason: str | None = None


_LOAD: Final = (
    "select s.id, s.tenant_id, s.kind, s.name, s.provider, s.config, s.credential,"
    " s.default_scope, s.generation, s.mode, s.publication, s.status, c.state, c.last_sync_at,"
    " c.last_status from {d}.sources s left join {d}.source_cursors c on c.source_id = s.id"
    " where s.id = %s"
)


async def _load(conn: AsyncConnection[DictRow], schemas: Schemas, source_id: uuid.UUID) -> DictRow:
    cur = await conn.execute(_LOAD.format(d=schemas.data).encode(), (source_id,))
    row = await cur.fetchone()
    if row is None:
        raise LookupError("no such source")
    return row


async def _people(
    conn: AsyncConnection[DictRow], schemas: Schemas, source: Source
) -> dict[uuid.UUID, list[str]]:
    """Linked accounts of the source's namespace with their subjects there."""
    cur = await conn.execute(
        f"select account_id, array_agg(subject) as subjects from {schemas.data}.identities"
        " where tenant_id = %s and provider = %s and workspace = %s and account_id is not null"
        " and (active or proof = 'oauth') group by account_id order by random() limit %s".encode(),
        (
            source.tenant_id,
            source.provider,
            CONNECTORS[source.kind].workspace_of(source),
            PEOPLE,
        ),
    )
    return {r["account_id"]: list(r["subjects"]) for r in await cur.fetchall()}


async def compare(
    conn: AsyncConnection[DictRow], schemas: Schemas, env: Env, source_id: uuid.UUID
) -> Run:
    """Compare one source with itself. `LookupError`: unknown source; `ValueError`: no oracle."""
    d = schemas.data
    row = await _load(conn, schemas, source_id)
    connector = CONNECTORS.get(row["kind"])
    if connector is None or not connector.has_can_open() or row["mode"] != "stored":
        raise ValueError("this source has no oracle to compare with")
    source = await with_people(conn, env, build_source(row, env, None))
    people = await _people(conn, schemas, source)
    cur = await conn.execute(
        f"select external_id from {d}.records where source_id = %s and acl is not null"
        " order by random() limit %s".encode(),
        (source_id, SAMPLE),
    )
    pool = [r["external_id"] for r in await cur.fetchall()]
    shown: dict[uuid.UUID, tuple[set[str], set[str]]] = {}
    async with conn.transaction():
        await conn.execute(b"set transaction read only")
        await conn.execute(b"set local role daimon_data_parity")
        for account in people:
            args = (source_id, account)
            cur = await conn.execute(
                f"select p as id from {d}.parity_visible(%s, %s) p"
                f" order by random() limit {SAMPLE}".encode(),
                args,
            )
            mine = {r["id"] for r in await cur.fetchall()}
            cur = await conn.execute(
                f"select p as id from {d}.parity_visible(%s, %s) p"
                " where p = any(%s::text[])".encode(),
                (*args, pool),
            )
            shown[account] = (mine, {r["id"] for r in await cur.fetchall()})
    gate = asyncio.Semaphore(CHECKS)

    async def ask(subjects: list[str], ext: str) -> bool | None:
        async with gate:
            try:
                return await connector.can_open(source, ext, subjects)
            except Exception as e:
                log.warning("data.parity_unanswered", kind=source.kind, error=type(e).__name__)
                return None

    more = less = unanswered = checked = 0
    for account, subjects in people.items():
        mine, in_pool = shown[account]
        ids = sorted(mine | set(pool))
        answers = await asyncio.gather(*(ask(subjects, i) for i in ids))
        for ext, said in zip(ids, answers, strict=True):
            checked += 1
            ours = ext in mine or ext in in_pool
            if said is None:
                unanswered += 1
            elif ours and not said:
                more += 1
            elif said and not ours:
                less += 1
    note = None
    if not people:
        note = "no linked people"
    elif not checked:
        note = "no records to check"
    elif unanswered:
        note = f"{unanswered} checks not answered"
    return Run(len(people), checked, more, less, note)


async def record(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    source_id: uuid.UUID,
    run: Run,
    generation: int,
    started_at: datetime,
) -> None:
    """Keep a run as evidence. One that finished after the source's generation moved proves
    nothing and is not clean."""
    d = schemas.data
    async with conn.transaction():
        cur = await conn.execute(
            f"select tenant_id, generation from {d}.sources where id = %s for share".encode(),
            (source_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return
        note = run.unresolved
        if row["generation"] != generation:
            note = "changed during compare"
        ok = run.ok and note is None
        await conn.execute(
            f"insert into {d}.parity_runs (source_id, generation, started_at, finished_at, people,"
            " checked, wrong_more, wrong_less, unresolved, ok)"
            " values (%s, %s, %s, now(), %s, %s, %s, %s, %s, %s)".encode(),
            (
                source_id,
                generation,
                started_at,
                run.people,
                run.checked,
                run.wrong_more,
                run.wrong_less,
                note,
                ok,
            ),
        )
        if run.wrong_more:
            await audit.record_audit(
                conn, schemas, tenant_id=row["tenant_id"], kind="parity.wrong_more",
                source_id=source_id, detail={"people": run.people, "wrong_more": run.wrong_more},
            )  # fmt: skip


async def gate(conn: AsyncConnection[DictRow], schemas: Schemas, source_id: uuid.UUID) -> Gate:
    """Whether the source has the evidence to be published."""
    d = schemas.data
    row = await _load(conn, schemas, source_id)
    connector = CONNECTORS.get(row["kind"])
    if row["status"] == "disabled" or row["publication"] == "retired":
        return Gate(False, "the source is closed")
    if connector is None or not connector.has_can_open():
        if row["mode"] == "federated" or row["last_status"] == "ok":
            return Gate(True)
        return Gate(False, "no complete sync yet")
    cur = await conn.execute(
        f"select ok, now() - finished_at as age from {d}.parity_runs where source_id = %s"
        " and generation = %s order by started_at desc, id desc limit 1".encode(),
        (source_id, row["generation"]),
    )
    run = await cur.fetchone()
    if run is None:
        return Gate(False, "no parity run since the source last changed")
    if not run["ok"]:
        return Gate(False, "the newest parity run is not clean")
    if run["age"] > MAX_AGE:
        return Gate(
            False, f"the newest clean run is over {int(MAX_AGE.total_seconds() // 3600)} hours old"
        )
    return Gate(True)


async def publish(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    source_id: uuid.UUID,
    by: uuid.UUID | None = None,
) -> Gate:
    """Shadow to published, when the gate is met. A source already published is left as it is."""
    row = await _load(conn, schemas, source_id)
    if row["publication"] == "published":
        return Gate(True)
    verdict = await gate(conn, schemas, source_id)
    if not verdict.met:
        return verdict
    try:
        async with conn.transaction():
            await conn.execute(
                f"update {schemas.data}.sources set publication = 'published'"
                " where id = %s".encode(),
                (source_id,),
            )
            await audit.record_audit(
                conn, schemas, tenant_id=row["tenant_id"], kind="source.published",
                account_id=by, source_id=source_id,
            )  # fmt: skip
    except psycopg.errors.InsufficientPrivilege:
        return Gate(
            False, "the newest parity run found the source showing more than its source does"
        )
    return Gate(True)


async def unpublish(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    source_id: uuid.UUID,
    by: uuid.UUID | None = None,
) -> None:
    """Published back to shadow; a retired source stays retired."""
    async with conn.transaction():
        cur = await conn.execute(
            f"update {schemas.data}.sources set publication = 'shadow' where id = %s"
            " and publication = 'published' returning tenant_id".encode(),
            (source_id,),
        )
        row = await cur.fetchone()
        if row:
            await audit.record_audit(
                conn, schemas, tenant_id=row["tenant_id"], kind="source.unpublished",
                account_id=by, source_id=source_id,
            )  # fmt: skip


async def due(conn: AsyncConnection[DictRow], schemas: Schemas) -> list[uuid.UUID]:
    """Sources with an oracle that need a run: none at their generation, a clean one over
    23 hours old, or a failed one over an hour old. The one compared longest ago first."""
    d = schemas.data
    cur = await conn.execute(
        f"select s.id, s.kind, r.ok, r.started_at from {d}.sources s left join lateral ("
        f"select ok, started_at from {d}.parity_runs where source_id = s.id"
        " and generation = s.generation"
        " order by started_at desc, id desc limit 1) r on true"
        " where s.mode = 'stored' and s.status = 'active' and s.publication <> 'retired'"
        " order by r.started_at nulls first, s.id".encode()
    )
    now = datetime.now(UTC)
    out: list[uuid.UUID] = []
    for r in await cur.fetchall():
        connector = CONNECTORS.get(r["kind"])
        if connector is None or not connector.has_can_open():
            continue
        wait = None if r["started_at"] is None else AGAIN_CLEAN if r["ok"] else AGAIN_FAILED
        if wait is None or now - r["started_at"] >= wait:
            out.append(r["id"])
    return out


async def run_due(
    conn: AsyncConnection[DictRow], schemas: Schemas, env: Env, budget: float = 600.0
) -> dict[uuid.UUID, bool]:
    """A recorded run for each due source, the whole bounded by `budget` seconds (what is
    left waits for the next tick). Returns clean or not by source."""
    done: dict[uuid.UUID, bool] = {}
    end = asyncio.get_running_loop().time() + budget
    for source_id in await due(conn, schemas):
        left = end - asyncio.get_running_loop().time()
        if left <= 0:
            break
        started = datetime.now(UTC)
        generation = int((await _load(conn, schemas, source_id))["generation"])
        try:
            async with asyncio.timeout(left):
                run = await compare(conn, schemas, env, source_id)
        except Exception as e:
            run = Run(unresolved=f"failed ({type(e).__name__})")
        await record(conn, schemas, source_id, run, generation, started)
        done[source_id] = run.ok
        if run.wrong_more:
            log.error("data.parity_wrong_more", source=str(source_id), people=run.people)
    return done

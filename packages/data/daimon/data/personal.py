"""A person's personal parts: what Daimon reads as them, which each person can turn off.

`drive`: their own Drive (`daimon.data.my_drive`); `calendar`: their calendar, read live as
them (`daimon.data.federated`). Both are on in every workspace until an admin turns a part off
for it (the `data_personal_drive` and `data_personal_calendar` features) or the person turns
it off for themselves; the `data_drive_reads` and `data_calendar_reads` tools and the profile
page change the latter.

Both read through the workspace's Google connection (`google_connection`), which any active
Google source (Drive or Calendar) provides; with none, no part is offered.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from typing import Final, get_args

from daimon.data.audit import record_audit
from daimon.data.recheck import reader_subjects
from daimon.data.schema import Schemas
from daimon.data.sync.connector import Part
from daimon.data.sync.engine import Env, build_source
from psycopg import AsyncConnection
from psycopg.rows import DictRow

__all__ = [
    "GOOGLE_KINDS", "PARTS", "Part", "google_connection", "has_google", "parts", "set_part",
    "turned_off",
]  # fmt: skip
PARTS: Final[tuple[Part, ...]] = get_args(Part.__value__)
GOOGLE_KINDS: Final = ("gdrive", "gcal")
"""The kinds of source that carry a Google credential: any active one serves both parts."""
_GOOGLE: Final = (
    "select s.id, s.tenant_id, s.kind, s.name, s.provider, s.config, s.credential,"
    " s.default_scope, s.redaction_set, null::jsonb as state, null::timestamptz as last_sync_at"
    " from {d}.sources s where s.kind = any(%s) and s.status = 'active'"
    " and s.publication = 'published' and s.tenant_id = any(%s) order by s.name, s.id"
)


async def has_google(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_ids: Sequence[uuid.UUID]
) -> bool:
    cur = await conn.execute(
        _GOOGLE.format(d=schemas.data).encode() + b" limit 1",
        (list(GOOGLE_KINDS), list(tenant_ids)),
    )
    return await cur.fetchone() is not None


async def google_connection(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    env: Env,
    tenant_ids: Sequence[uuid.UUID],
    readers: Sequence[str],
) -> tuple[DictRow, list[tuple[str, ...]]] | None:
    """The Google source a personal read of `readers` goes through, with the subjects it acts
    as: the first active, published Drive or Calendar source, by name then id, whose domain
    every reader is linked to. None when no source can act as all of them."""
    cur = await conn.execute(
        _GOOGLE.format(d=schemas.data).encode(), (list(GOOGLE_KINDS), list(tenant_ids))
    )
    for row in await cur.fetchall():
        who = await reader_subjects(conn, schemas, build_source(row, env, None), readers)
        if who and all(who):
            return row, who
    return None


async def parts(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    allowed: Collection[Part] = PARTS,
) -> dict[Part, bool]:
    """Each part the workspace allows (`allowed`), and whether Daimon may read it as this
    person; none without a Google source."""
    if not await has_google(conn, schemas, [tenant_id]):
        return {}
    off = await _off(conn, schemas, [tenant_id], [account_id])
    return {p: p not in off for p in PARTS if p in allowed}


async def set_part(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    part: Part,
    on: bool,
) -> None:
    d = schemas.data
    if on:
        sql = f"delete from {d}.personal_off where tenant_id = %s and account_id = %s and part = %s"
    else:
        sql = (
            f"insert into {d}.personal_off (tenant_id, account_id, part) values (%s, %s, %s)"
            " on conflict do nothing"
        )
    await conn.execute(sql.encode(), (tenant_id, account_id, part))
    await record_audit(
        conn, schemas, tenant_id=tenant_id, kind="person.personal_part", account_id=account_id,
        detail={"part": part, "on": on},
    )  # fmt: skip


async def turned_off(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_ids: Sequence[uuid.UUID],
    readers: Sequence[str],
    part: Part,
) -> bool:
    """Whether any of `readers` (`account:<uuid>` subjects) turned `part` off."""
    accounts = [r.removeprefix("account:") for r in readers if r.startswith("account:")]
    return bool(accounts) and part in await _off(conn, schemas, tenant_ids, accounts)


async def _off(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_ids: Sequence[uuid.UUID],
    accounts: Sequence[uuid.UUID | str],
) -> set[str]:
    cur = await conn.execute(
        f"select distinct part from {schemas.data}.personal_off where tenant_id = any(%s)"
        " and account_id::text = any(%s)".encode(),
        (list(tenant_ids), [str(a) for a in accounts]),
    )
    return {r["part"] for r in await cur.fetchall()}

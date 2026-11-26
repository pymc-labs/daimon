"""A person's personal parts: what Daimon reads as them, which each person can turn off.

`drive`: their own Drive (`daimon.data.my_drive`); `calendar`: their calendar, read live as
them (`daimon.data.federated`). Both are on until the person turns them off in a workspace;
an admin allows each part per workspace through its source (`personal_drive`, a Calendar
source). The `data_personal_reads` tool and the profile page change them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final, get_args

from daimon.data.audit import record_audit
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS, Json, Part
from daimon.data.sync.connectors.chat import switch
from psycopg import AsyncConnection
from psycopg.rows import DictRow

__all__ = ["PARTS", "Part", "parts", "set_part", "turned_off"]
PARTS: Final[tuple[Part, ...]] = get_args(Part.__value__)


async def parts(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> dict[Part, bool]:
    """Each part a source of the workspace reads, and whether Daimon may read it as this
    person."""
    cur = await conn.execute(
        f"select kind, config from {schemas.data}.sources where tenant_id = %s"
        " and status = 'active' and publication = 'published'".encode(),
        (tenant_id,),
    )
    offered = {p for r in await cur.fetchall() if (p := _offered(r["kind"], r["config"]))}
    off = await _off(conn, schemas, [tenant_id], [account_id])
    return {p: p not in off for p in PARTS if p in offered}


def _offered(kind: str, config: Json) -> Part | None:
    connector = CONNECTORS.get(kind)
    if connector is None or (kind == "gdrive" and not switch(config, "personal_drive")):
        return None
    return connector.personal_part


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

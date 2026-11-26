"""A person's own Google Drive, read as them at their own request (the `data_my_drive` tool,
which asks them on a card each time): through each Drive source of their workspace that
allows it (`personal_drive`), only as the directory person they are linked to, and never once
they turned their `drive` part off (`daimon.data.personal`). Nothing is stored; a source's
ingest redaction rules apply here, the answer's own redaction in the tool."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Final, cast

from daimon.data import personal, redaction
from daimon.data.recheck import reader_subjects
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS, LiveHit
from daimon.data.sync.connectors.gdrive import GDriveConnector
from daimon.data.sync.engine import Env, build_source, ready
from psycopg import AsyncConnection
from psycopg.rows import DictRow

ANSWER_TEXT: Final = 60_000
"""Characters of file text one search returns in all; later files keep their titles only."""


async def search(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    env: Env,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    query: str,
    limit: int = 10,
) -> list[LiveHit]:
    if await personal.turned_off(conn, schemas, [tenant_id], [f"account:{account_id}"], "drive"):
        return []
    cur = await conn.execute(
        "select s.id, s.tenant_id, s.kind, s.name, s.provider, s.config, s.credential,"
        " s.default_scope, s.redaction_set, null::jsonb as state, null::timestamptz as last_sync_at"
        f" from {schemas.data}.sources s where s.kind = 'gdrive' and s.status = 'active'"
        " and s.publication = 'published' and s.tenant_id = %s order by s.id".encode(),
        (tenant_id,),
    )
    drive = cast("GDriveConnector", CONNECTORS["gdrive"])
    hits: list[LiveHit] = []
    for row in await cur.fetchall():
        try:
            rules = (
                await redaction.ingest_redactor(conn, schemas, tenant_id, row["redaction_set"])
                if row["redaction_set"]
                else None
            )
        except redaction.NotApproved:
            continue
        source = await ready(conn, env, build_source(row, env, None))
        [subjects] = await reader_subjects(conn, schemas, source, [f"account:{account_id}"])
        for user_id in subjects:
            found = await drive.my_drive_search(source, user_id, query, limit)
            hits += (
                [
                    replace(h, title=rules.text(h.title), text=rules.text(h.text))
                    for h in found
                    if not rules.excludes(h.ref)
                ]
                if rules
                else found
            )
    left, out = ANSWER_TEXT, list[LiveHit]()
    for h in hits[:limit]:
        out.append(replace(h, text=h.text[:left]))
        left = max(left - len(h.text), 0)
    return out

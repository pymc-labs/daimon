"""A person's own Google Drive, read as them at their own request (the `data_my_drive` tool,
which asks them on a card each time): through the workspace's Google connection
(`personal.google_connection`: any active Drive or Calendar source), only as the directory
person they are linked to, and never once they turned their `drive` part off
(`daimon.data.personal`). Nothing is stored; the source's ingest redaction rules apply here, the
answer's own redaction in the tool."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Final, cast

from daimon.data import personal, redaction
from daimon.data.schema import Schemas
from daimon.data.sync.connector import CONNECTORS, LiveHit
from daimon.data.sync.connectors.gdrive import GDriveConnector
from daimon.data.sync.engine import Env, build_source, ready
from psycopg import AsyncConnection
from psycopg.rows import DictRow

ANSWER_TEXT: Final = 60_000
"""Characters of file text one answer holds in all, cut after the answer's redaction; later
files keep their titles only."""


async def search(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    env: Env,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    query: str,
    limit: int = 10,
) -> list[tuple[uuid.UUID, LiveHit]]:
    """What the person's own Drive finds for `query`, each with the source it was read through
    (whose answer-time rules it goes through)."""
    if await personal.turned_off(conn, schemas, [tenant_id], [f"account:{account_id}"], "drive"):
        return []
    found = await personal.google_connection(
        conn, schemas, env, [tenant_id], [f"account:{account_id}"]
    )
    if found is None:
        return []
    row, [subjects] = found
    try:
        rules = (
            await redaction.ingest_redactor(conn, schemas, tenant_id, row["redaction_set"])
            if row["redaction_set"]
            else None
        )
    except redaction.NotApproved:
        return []
    source = await ready(conn, env, build_source(row, env, None))
    drive = cast("GDriveConnector", CONNECTORS["gdrive"])
    hits: list[tuple[uuid.UUID, LiveHit]] = []
    for user_id in subjects:
        found_files = await drive.my_drive_search(source, user_id, query, limit)
        hits += [
            (source.id, replace(h, title=rules.text(h.title), text=rules.text(h.text)))
            if rules
            else (source.id, h)
            for h in found_files
            if not (rules and rules.excludes(h.ref))
        ]
    return hits[:limit]

"""The `data_*` tools: search and read the workspace's data sources, behind `data_reads`.

Each call resolves who its answer is for (`data_audience_for`, signed facts only), turns that
audience into the store's `ReadContext` and reads through `access.scoped`; the model passes
no reader. A query's model spend bills the asker. Every result goes out through
`mark_data_result`, so a transcript read elsewhere hides it.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.data_access import data_audience_for, ledgers
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.data_tools import mark_data_result
from daimon.core.ports.data_metering import SessionMeter
from daimon.core.stores.data_memory import note_reads
from daimon.data import caller_sql, federated, pool, reads
from daimon.data.caller_sql import SqlRefused
from daimon.data.metering import Payer
from daimon.data.reading import Reading, open_read
from daimon.data.schema import Schemas
from daimon.data.search.arms import Filters
from daimon.data.search.pipeline import search
from daimon.data.sync.engine import Env
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from psycopg import AsyncConnection
from psycopg.rows import DictRow

PAGE: Final = 12_000
"""Characters of a record's text one `data_get` returns."""
NOT_FOUND: Final = "Not found, or not visible to everyone this answer is for."
REDACTED: Final = "Someone this answer is for may not see everything here, so this is not shown."


def _text(value: object) -> str:
    return mark_data_result(json.dumps(value, default=str, ensure_ascii=False))


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        raise ToolError(NOT_FOUND) from None


def register_data_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    """Register the `data_*` tools (`DATA_TOOLS`); the capability gate offers them only where
    `data_reads` is On."""
    settings = runtime.settings
    schemas = Schemas.of(settings.data)
    env = Env.of(settings)

    @asynccontextmanager
    async def reading(
        auth: AuthIdentity,
    ) -> AsyncIterator[tuple[AsyncConnection[DictRow], Reading]]:
        """The connection and what the answer may read (`daimon.data.reading`)."""
        audience = await data_audience_for(runtime, auth)
        async with pool.connection(settings) as conn:
            salt = f"agent:{auth.chat_agent_id or auth.agent_id}"
            yield conn, await open_read(conn, schemas, auth.tenant_id, audience, salt=salt)

    async def noted(auth: AuthIdentity, refs: list[str]) -> None:
        """Note what the session read, and an asking agent's session for a helper, so a
        memory either saves records it."""
        if auth.chat_session_id is None or not refs:
            return
        sessions = await ledgers(runtime, auth)
        async with runtime.session_factory.begin() as session:
            for chat_session_id in sessions:
                await note_reads(
                    session, tenant_id=auth.tenant_id, chat_session_id=chat_session_id, refs=refs
                )

    @mcp.tool()
    async def data_sources(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        """The data sources you can read here, with their ids and how many records each holds,
        and the datasets `data_sql` can query."""
        auth = await _auth(ctx)
        async with reading(auth) as (conn, r):
            found = await reads.sources(conn, schemas, r.ctx)
            sets = await reads.datasets(conn, schemas, r.ctx)
        return _text(r.show({"sources": [dataclasses.asdict(s) for s in found],
                           "datasets": [dataclasses.asdict(d) for d in sets]}))  # fmt: skip

    @mcp.tool()
    async def data_search(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        query: str,
        source_ids: list[str] | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> str:
        """Search the data sources for `query`; only what everyone who will see the answer may
        read. Filter by source ids (`data_sources`), record kind and date range. Each hit has a
        `record_id` to open with `data_get`; `live` holds what live sources answered now. No
        hit is not proof that nothing exists."""
        auth = await _auth(ctx)
        filters = Filters(
            sources=tuple(_uuid(s) for s in source_ids or ()),
            kinds=(kind,) if kind else (),
            since=since,
            until=until,
        )
        async with reading(auth) as (conn, r):
            if r.names_hidden(query):
                return _text({"hits": [], "live": [], "notes": []})
            result = await search(
                conn,
                schemas,
                r.ctx,
                query,
                settings=settings.data,
                meter=SessionMeter(runtime.session_factory, settings.billing.markup),
                payer=Payer(auth.tenant_id, auth.platform_user_id),
                limit=min(max(limit, 1), 25),
                filters=filters,
                env=env,
                live_meter=federated.audit_meter(conn, schemas, auth.tenant_id, auth.account_id),
            )
        hits = [
            {k: v for k, v in dataclasses.asdict(h).items() if k not in ("text", "arms")}
            for h in result.hits
        ]
        live = [dataclasses.asdict(r) for r in result.live]
        await noted(auth, [f"record:{h.record_id}" for h in result.hits]
                    + [f"live:{r.source_id}" for r in result.live])  # fmt: skip
        return _text(r.show({"hits": hits, "live": live, "notes": result.notes}))

    @mcp.tool()
    async def data_get(ctx: Context, record_id: str, start: int = 0) -> str:  # pyright: ignore[reportUnusedFunction]
        """Open one record by its `record_id`: its text (12,000 characters from `start`; a
        `next_start` says where the rest continues) and its attached files for `data_file`."""
        auth = await _auth(ctx)
        async with reading(auth) as (conn, r):
            record = await reads.get(conn, schemas, r.ctx, _uuid(record_id), env)
        if record is None:
            raise ToolError(NOT_FOUND)
        await noted(auth, [f"record:{record.item.id}"])
        start = max(start, 0)
        end = start + PAGE
        body = r.show(record.body, record.item.source_id)  # whole: no name straddles a page
        return _text(r.show({
            **dataclasses.asdict(record.item),
            "text": body[start:end],
            "next_start": end if end < len(body) else None,
            "files": [dataclasses.asdict(a) for a in record.attachments],
        }))  # fmt: skip

    @mcp.tool()
    async def data_file(ctx: Context, record_id: str, index: int) -> str:  # pyright: ignore[reportUnusedFunction]
        """Read a file attached to a record (`index` from `data_get`'s files), fetched now from
        its source with the source's permissions and never stored. Text files return their
        text; other files only their type and size. Refused when names are hidden from someone
        the answer is for."""
        auth = await _auth(ctx)
        async with reading(auth) as (conn, r):
            if r.redacted:
                raise ToolError(REDACTED)
            got = await reads.read_file(conn, schemas, r.ctx, env, _uuid(record_id), index)
        if got is None:
            raise ToolError(NOT_FOUND)
        await noted(auth, [f"record:{_uuid(record_id)}"])
        data, content_type = got
        textual = content_type.startswith("text/") or content_type.endswith(("json", "csv", "xml"))
        text = data.decode("utf-8", "replace")[:PAGE] if textual else None
        return _text({"content_type": content_type, "size": len(data), "text": text})

    @mcp.tool()
    async def data_sql(ctx: Context, query: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Read-only SQL over the data views and datasets (`data_sources` lists them), by plain
        names. One statement, at most 500 rows, five seconds. Refused when names are hidden
        from someone the answer is for."""
        auth = await _auth(ctx)
        async with reading(auth) as (conn, r):
            if r.redacted:
                raise ToolError(REDACTED)
            try:
                result = await caller_sql.data_sql(conn, schemas, r.ctx, query)
            except SqlRefused as e:
                raise ToolError(str(e)) from None
        await noted(auth, ["sql"])
        return _text(dataclasses.asdict(result))

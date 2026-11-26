"""`/api/data/*`: search, browse and open the workspace's data sources, as the signed-in person.

Every read goes through `access.scoped` with what this person may read (`data_common`), so a
hit, an item or a file is there only if they could read it. A person marked redacted sees the
workspace's redaction lists applied, as an agent answering them would, and gets no file
bytes. Files download as attachments, never rendered by the browser.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final
from urllib.parse import quote

from daimon.adapters.web.data_common import (
    NOT_FOUND,
    Viewer,
    reading,
    schemas_of,
    shown,
    uuid_of,
)
from daimon.adapters.web.paging import NEXT_HEADER
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.ports.data_metering import SessionMeter
from daimon.data import federated, freshness, reads
from daimon.data.metering import Payer
from daimon.data.search.arms import Filters
from daimon.data.search.pipeline import search
from daimon.data.sync.engine import Env
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

TEXT_CHARS: Final = 12_000
"""Characters of a record's text one answer holds."""
QUERY_CHARS: Final = 500
REDACTED: Final = "Files aren't shown while some names are hidden from you."
RULED: Final = "This file isn't shown: its source's redaction rules reach only text."


class SourceOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    status: str
    records: int
    last_sync_at: datetime | None
    fresh: bool


class ItemOut(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    kind: str
    title: str
    url: str | None
    author: str | None
    occurred_at: datetime
    scope: str


class HitOut(BaseModel):
    record_id: uuid.UUID
    source_id: uuid.UUID
    kind: str
    title: str
    url: str | None
    occurred_at: datetime
    scope: str
    snippet: str


class LiveHitOut(BaseModel):
    title: str
    kind: str
    url: str | None
    text: str


class LiveOut(BaseModel):
    source_id: uuid.UUID
    source_name: str
    hits: list[LiveHitOut]
    skipped: str | None


class SearchOut(BaseModel):
    hits: list[HitOut]
    live: list[LiveOut]
    notes: list[str]


class FileOut(BaseModel):
    ord: int
    name: str
    content_type: str
    size: int


class RecordOut(BaseModel):
    item: ItemOut
    text: str
    next_start: int | None
    files: list[FileOut]


def data_pages_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/data")
    schemas = schemas_of(runtime)
    settings = runtime.settings
    env = Env.of(settings)

    @router.get("/sources")
    async def list_sources(caller: Viewer) -> list[SourceOut]:  # pyright: ignore[reportUnusedFunction]
        async with reading(runtime, caller) as (conn, r):
            found = await reads.sources(conn, schemas, r.ctx)
            fresh = {s.id: await freshness.state(conn, schemas, s.id) for s in found}
        out = [
            SourceOut(
                id=s.id, name=s.name, kind=s.kind, status=s.status, records=s.records,
                last_sync_at=f.last_sync_at if f else None, fresh=bool(f and f.fresh),
            )
            for s in found
            for f in [fresh[s.id]]
        ]  # fmt: skip
        return [shown(r, o) for o in out]

    @router.get("/search")
    async def find(  # pyright: ignore[reportUnusedFunction]
        caller: Viewer,
        q: Annotated[str, Query(min_length=1, max_length=QUERY_CHARS)],
        source: Annotated[list[uuid.UUID] | None, Query()] = None,
        kind: Annotated[str | None, Query(max_length=100)] = None,
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> SearchOut:
        person = caller.person
        tenant = person.tenant_id
        async with runtime.sessionmaker.begin() as db:
            principal = await caller.principal(db)
        async with reading(runtime, caller) as (conn, r):
            if r.names_hidden(q):
                return SearchOut(hits=[], live=[], notes=[])
            result = await search(
                conn, schemas, r.ctx, q,
                settings=settings.data,
                meter=SessionMeter(runtime.sessionmaker, settings.billing.markup),
                payer=Payer(tenant, principal.external_id),
                limit=limit,
                filters=Filters(sources=tuple(source or ()), kinds=(kind,) if kind else ()),
                env=env,
                live_meter=federated.audit_meter(conn, schemas, tenant, person.account_id),
            )  # fmt: skip
        out = SearchOut(
            hits=[HitOut.model_validate(h, from_attributes=True) for h in result.hits],
            live=[
                LiveOut(
                    source_id=r.source_id, source_name=r.source_name, skipped=r.skipped,
                    hits=[LiveHitOut.model_validate(h, from_attributes=True) for h in r.hits],
                )
                for r in result.live
            ],
            notes=result.notes,
        )  # fmt: skip
        return shown(r, out)

    @router.get("/sources/{source_id}/items")
    async def browse(  # pyright: ignore[reportUnusedFunction]
        caller: Viewer,
        response: Response,
        source_id: str,
        kind: Annotated[str | None, Query(max_length=100)] = None,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> list[ItemOut]:
        async with reading(runtime, caller) as (conn, r):
            try:
                page = await reads.browse(
                    conn,
                    schemas,
                    r.ctx,
                    uuid_of(source_id),
                    kind=kind,
                    after=cursor,
                    limit=limit,
                    env=env,
                )
            except ValueError:
                raise HTTPException(status_code=422, detail="That page doesn't exist.") from None
        if page.next:
            response.headers[NEXT_HEADER] = page.next
        return [shown(r, ItemOut.model_validate(i, from_attributes=True)) for i in page.items]

    @router.get("/records/{record_id}")
    async def record(  # pyright: ignore[reportUnusedFunction]
        caller: Viewer,
        record_id: str,
        start: Annotated[int, Query(ge=0)] = 0,
    ) -> RecordOut:
        async with reading(runtime, caller) as (conn, r):
            found = await reads.get(conn, schemas, r.ctx, uuid_of(record_id), env)
        if found is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND)
        body = r.show(found.body, found.item.source_id)
        end = start + TEXT_CHARS
        out = RecordOut(
            item=ItemOut.model_validate(found.item, from_attributes=True),
            text=body[start:end],
            next_start=end if end < len(body) else None,
            files=[FileOut.model_validate(a, from_attributes=True) for a in found.attachments],
        )
        return shown(r, out, found.item.source_id)

    @router.get("/records/{record_id}/files/{index}")
    async def file(caller: Viewer, record_id: str, index: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        async with reading(runtime, caller) as (conn, r):
            if r.redacted:
                raise HTTPException(status_code=403, detail=REDACTED)
            rid = uuid_of(record_id)
            found = await reads.get(conn, schemas, r.ctx, rid, env)
            name = (
                next((a.name for a in found.attachments if a.ord == index), None) if found else None
            )
            got = (
                await reads.read_file(conn, schemas, r.ctx, env, rid, index, r.answer)
                if name
                else None
            )
        if got is None or name is None or found is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND)
        data, content_type = got
        if found.item.source_id in r.answer and not reads.textual(content_type):
            raise HTTPException(status_code=403, detail=RULED)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
                "X-Content-Type-Options": "nosniff",
                "X-Original-Type": content_type,
            },
        )

    return router

"""`/api/integrations/data/*`: a workspace's data sources, for admins.

Reads and writes go through `daimon.data.manage`, which touches only this workspace's rows,
audits every write and refuses a declared (bound) source. No answer holds a source's config or
credential: only its state, its runs and its parity result.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from daimon.adapters.web.data_common import (
    ERROR_CHARS,
    Manager,
    keys_of,
    refused,
    schemas_of,
    store,
    uuid_of,
)
from daimon.adapters.web.runtime import WebRuntime
from daimon.data import manage
from daimon.data.identity import IdentityError
from fastapi import APIRouter
from pydantic import BaseModel, Field


class FieldOut(BaseModel):
    name: str
    label: str
    kind: str
    options: list[str]
    required: bool
    placeholder: str


class KindOut(BaseModel):
    kind: str
    name: str
    category: str
    live: bool
    fields: list[FieldOut]


class ParityOut(BaseModel):
    finished_at: datetime | None
    people: int
    checked: int
    wrong_more: int
    wrong_less: int
    unresolved: str | None
    ok: bool


class SourceOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    mode: str
    status: str
    publication: str
    bound: bool
    default_scope: str
    group: str | None
    created_at: datetime
    records: int
    last_sync_at: datetime | None
    last_status: str | None
    last_error: str | None
    errors: int
    parity: ParityOut | None
    can_publish: bool
    blocked: str | None


class RunOut(BaseModel):
    id: uuid.UUID
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    added: int
    updated: int
    deleted: int
    error: str | None


class NewSource(BaseModel):
    kind: str = Field(max_length=100)
    name: str = Field(max_length=200)
    settings: dict[str, str | list[str]] = Field(default_factory=dict, max_length=50)
    group: str | None = Field(default=None, max_length=63)


class Created(BaseModel):
    id: uuid.UUID


def _cut(text: str | None) -> str | None:
    return None if text is None else text[:ERROR_CHARS]


def _source(view: manage.SourceView) -> SourceOut:
    out = SourceOut.model_validate(view, from_attributes=True)
    return out.model_copy(update={"last_error": _cut(out.last_error)})


def integrations_data_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/integrations/data")
    schemas = schemas_of(runtime)

    @router.get("/kinds")
    async def kinds(caller: Manager) -> list[KindOut]:  # pyright: ignore[reportUnusedFunction]
        return [KindOut.model_validate(k, from_attributes=True) for k in manage.kinds()]

    @router.get("/sources")
    async def sources(caller: Manager) -> list[SourceOut]:  # pyright: ignore[reportUnusedFunction]
        async with store(runtime) as conn:
            found = await manage.sources(conn, schemas, caller.person.tenant_id)
        return [_source(v) for v in found]

    @router.post("/sources", status_code=201)
    async def create(caller: Manager, body: NewSource) -> Created:  # pyright: ignore[reportUnusedFunction]
        person = caller.person
        async with store(runtime) as conn:
            try:
                made = await manage.create_source(
                    conn, schemas, keys_of(runtime),
                    tenant_id=person.tenant_id, by=person.account_id,
                    kind=body.kind, name=body.name, settings=dict(body.settings), group=body.group,
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None
        return Created(id=made)

    @router.post("/sources/{source_id}/{action}", status_code=204)
    async def act(  # pyright: ignore[reportUnusedFunction]
        caller: Manager,
        source_id: str,
        action: Literal["pause", "resume", "retire", "publish", "unpublish", "sync"],
    ) -> None:
        person = caller.person
        async with store(runtime) as conn:
            try:
                await manage.act(
                    conn, schemas, tenant_id=person.tenant_id, by=person.account_id,
                    source_id=uuid_of(source_id), action=action,
                )  # fmt: skip
            except IdentityError as e:
                raise refused(e) from None

    @router.get("/sources/{source_id}/runs")
    async def runs(caller: Manager, source_id: str) -> list[RunOut]:  # pyright: ignore[reportUnusedFunction]
        async with store(runtime) as conn:
            try:
                found = await manage.runs(
                    conn, schemas, caller.person.tenant_id, uuid_of(source_id)
                )
            except IdentityError as e:
                raise refused(e) from None
        return [
            RunOut.model_validate(r, from_attributes=True).model_copy(
                update={"error": _cut(r.error)}
            )
            for r in found
        ]

    return router

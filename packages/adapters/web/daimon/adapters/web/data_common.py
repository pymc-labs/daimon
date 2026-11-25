"""What the data routes share: who may call them, the store connection, who a read answers for.

A person reading in the web app reads for themselves: the rule in `daimon.core.data_audience`
with a private origin, the person as asker and as the agent owner (a list row they hold shows).
Writes are the workspace admins'. The store's own refusals carry messages safe to show.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.data_audience import (
    DataAudience,
    DataOrigin,
    DataRefusal,
    Reader,
    data_audience,
    scope_ceiling,
)
from daimon.data import access, identity, pool, redaction
from daimon.data.access import ReadContext, person
from daimon.data.identity import Conflict, IdentityError, Missing, NotAllowed
from daimon.data.schema import Schemas
from fastapi import Depends, HTTPException
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from pydantic import BaseModel

Viewer = Annotated[WebCaller, Depends(requires("data.view"))]
Manager = Annotated[WebCaller, Depends(requires("data.manage"))]

NOT_FOUND = "That isn't here, or you can't read it."
ERROR_CHARS = 300
"""A failed run's text is shown cut to this."""


def schemas_of(runtime: WebRuntime) -> Schemas:
    return Schemas.of(runtime.settings.data)


def keys_of(runtime: WebRuntime) -> tuple[str, ...]:
    return tuple(k.get_secret_value() for k in runtime.settings.crypto.keys)


@asynccontextmanager
async def store(runtime: WebRuntime) -> AsyncIterator[AsyncConnection[DictRow]]:
    """The store owner's autocommit connection; the pool opens on first use."""
    async with pool.connection(runtime.settings) as conn:
        yield conn


def refused(error: IdentityError) -> HTTPException:
    """The store's refusal as an HTTP one; its message is safe to show."""
    status = 404 if isinstance(error, Missing) else 409 if isinstance(error, Conflict) else 403
    if not isinstance(error, Missing | Conflict | NotAllowed):
        status = 422
    return HTTPException(status_code=status, detail=str(error))


def audience_of(caller: WebCaller) -> DataAudience:
    """Who a web person's read answers for: themselves, privately."""
    account = caller.person.account_id
    asker = Reader(f"web:{account}", account)
    found = data_audience(DataOrigin("private", asker=asker), agent_owner=account)
    if isinstance(found, DataRefusal):  # a private origin with an asker is never refused
        raise HTTPException(status_code=403, detail=NOT_FOUND)
    return found


async def read_context(
    runtime: WebRuntime, conn: AsyncConnection[DictRow], caller: WebCaller
) -> ReadContext:
    """What this person may read: the scopes they hold and the access lists they are on."""
    schemas, tenant = schemas_of(runtime), caller.person.tenant_id
    account = caller.person.account_id
    audience = audience_of(caller)
    holders = await identity.holders_of(conn, schemas, tenant, [account])
    held = frozenset(await access.effective_scopes(conn, schemas, tenant, holders[account]))
    return ReadContext(
        tenant_ids=(tenant,),
        scopes=tuple(sorted(scope_ceiling(audience, {account: held}))),
        readers=(person(account),) if audience.list_rows else (),
        personal_ok=True,
        salt="web",
    )


@asynccontextmanager
async def reading(
    runtime: WebRuntime, caller: WebCaller
) -> AsyncIterator[tuple[AsyncConnection[DictRow], ReadContext, Callable[[object], object]]]:
    """The connection, what this person may read, and how a result goes out: through the
    workspace's redaction lists when the person is marked redacted."""
    async with store(runtime) as conn:
        ctx = await read_context(runtime, conn, caller)
        account = caller.person.account_id
        hidden = await redaction.redacted_among(
            conn, schemas_of(runtime), caller.person.tenant_id, [account]
        )
        entities = (
            await redaction.read_redactor(
                conn, schemas_of(runtime), caller.person.tenant_id, ctx.scopes
            )
            if hidden
            else None
        )
        yield conn, ctx, entities or (lambda value: value)


def shown[M: BaseModel](show: Callable[[object], object], model: M) -> M:
    """`model` with its text passed through `show` (the redaction lists)."""
    return type(model).model_validate(show(model.model_dump(mode="json")))


def uuid_of(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None

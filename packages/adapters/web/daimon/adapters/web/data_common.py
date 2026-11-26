"""What the data routes share: who may call them, the store connection, who a read answers for.

A person reading in the web app reads for themselves: the rule in `daimon.core.data_audience`
with a private origin, the person as asker and as the agent owner (a list row they hold shows).
Writes are the workspace admins', who manage data access as data admins do. The store's own
refusals carry messages safe to show.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
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
)
from daimon.data import pool
from daimon.data.identity import Conflict, IdentityError, Missing, NotAllowed
from daimon.data.reading import Reading, open_read
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


def workspace_admin(caller: WebCaller) -> bool:
    """A workspace owner or admin, who manages data access as a data admin does."""
    return caller.role in ("owner", "admin")


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


@asynccontextmanager
async def reading(
    runtime: WebRuntime, caller: WebCaller
) -> AsyncIterator[tuple[AsyncConnection[DictRow], Reading]]:
    """The connection and what this person may read (`daimon.data.reading`), as an agent
    answering them would."""
    async with store(runtime) as conn:
        tenant = caller.person.tenant_id
        yield (
            conn,
            await open_read(conn, schemas_of(runtime), tenant, audience_of(caller), salt="web"),
        )


def shown[M: BaseModel](reading: Reading, model: M) -> M:
    """`model` with its text as this reader may read it."""
    return type(model).model_validate(reading.show(model.model_dump(mode="json")))


def uuid_of(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from None

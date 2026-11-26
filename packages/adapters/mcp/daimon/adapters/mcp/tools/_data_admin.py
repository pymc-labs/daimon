"""What the chat tools for the data pages share: who may use them, the store connection and
people by name. The tools call the same `daimon.data` functions as the web routes, so a
refusal reads the same in both."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._web_admin import web_actor_of, web_admin_of
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.ports.gate import data_sources_on
from daimon.core.stores import web_people
from daimon.core.web_admin import WebAdmin
from daimon.core.web_roles_rules import is_admin
from daimon.data import pool
from daimon.data.identity import IdentityError
from daimon.data.schema import Schemas
from fastmcp.exceptions import ToolError
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from pydantic_core import to_jsonable_python

NOT_ON: Final = "Data sources aren't turned on here."
NOT_ADMIN: Final = "Only a workspace admin or owner manages data sources."
ERROR_CHARS: Final = 300
"""A failed run's text is shown cut to this, as on the web."""


async def data_is_on(runtime: McpRuntime, auth: AuthIdentity) -> None:
    """Refuse unless the deployment and this workspace have data sources on."""
    if not data_sources_on(runtime.settings):
        raise ToolError(NOT_ON)
    place = FeaturePlace(auth.tenant_id, auth.platform or "web")
    async with runtime.session_factory() as db:
        if not (await feature_in(db, runtime.settings, place, "data_sources")).on:
            raise ToolError(NOT_ON)


async def manager(runtime: McpRuntime, auth: AuthIdentity) -> WebAdmin:
    """The caller as a workspace admin or owner of the web app (`data.manage`), with data on."""
    await data_is_on(runtime, auth)
    actor = await web_admin_of(runtime, auth)
    if not is_admin(actor.role):
        raise ToolError(NOT_ADMIN)
    return actor


async def person(runtime: McpRuntime, auth: AuthIdentity) -> WebAdmin:
    """The caller as the web app sees them (`data.view`), with data on."""
    await data_is_on(runtime, auth)
    return await web_actor_of(runtime, auth)


@asynccontextmanager
async def store(runtime: McpRuntime) -> AsyncIterator[AsyncConnection[DictRow]]:
    async with pool.connection(runtime.settings) as conn:
        yield conn


def schemas_of(runtime: McpRuntime) -> Schemas:
    return Schemas.of(runtime.settings.data)


def keys_of(runtime: McpRuntime) -> tuple[str, ...]:
    return tuple(k.get_secret_value() for k in runtime.settings.crypto.keys)


async def names_of(runtime: McpRuntime, tenant_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """The workspace's web people by account, the people a scope or a link may name."""
    async with runtime.session_factory() as db:
        found = await web_people.list_people(db, tenant_id=tenant_id)
    return {p.account_id: p.display_name for p in found}


def account_of(names: dict[uuid.UUID, str], ref: str) -> uuid.UUID:
    """A person of the workspace by account id or by exact name (ignoring case)."""
    ref = ref.strip()
    try:
        found = uuid.UUID(ref)
    except ValueError:
        same = [a for a, n in names.items() if n.casefold() == ref.casefold()]
        if len(same) != 1:
            raise ToolError(
                "No one here has that name."
                if not same
                else "More than one person has that name; give the account id from the list."
            ) from None
        return same[0]
    if found not in names:
        raise ToolError("No such person here.")
    return found


def holder_of(names: dict[uuid.UUID, str], ref: str) -> str:
    """A grant's holder as listed (`account:<id>`, a role or group), or a person by name."""
    return ref.strip() if ":" in ref else f"account:{account_of(names, ref)}"


def uuid_of(raw: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw.strip())
    except ValueError:
        raise ToolError(f"{what} is not valid.") from None


def refused(error: IdentityError) -> ToolError:
    """The store's refusal; its message is safe to show."""
    return ToolError(f"{error} Nothing changed.")


def plain(value: object) -> dict[str, Any]:
    """A dataclass as JSON values."""
    return to_jsonable_python(dataclasses.asdict(value))  # pyright: ignore[reportArgumentType]


def plain_all(values: Sequence[object]) -> list[dict[str, Any]]:
    return [plain(v) for v in values]


def cut(text: str | None) -> str | None:
    return None if text is None else text[:ERROR_CHARS]


def own(auth: AuthIdentity) -> None:
    """Refuse a caller that is not a person acting for themselves."""
    if auth.is_external_agent or auth.hold is not None or auth.client:
        raise ToolError("This tool acts only on a person's own data.")

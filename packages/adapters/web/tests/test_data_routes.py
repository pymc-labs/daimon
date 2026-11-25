"""The data sources pages' routes: hidden when the feature is off, admin-only writes, declared
sources left alone, and no credential in any answer."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core.config import Settings
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.data.pool import close_pools
from daimon.testing.data_db import DataDb
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import Workspace, funded_workspace, signed_in
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_DSN = "postgresql://reader:hunter2@db.example.com:5432/app"
_SOURCES = "/api/integrations/data/sources"
_NEW = {"kind": "postgres", "name": "Orders", "settings": {"dsn": _DSN, "view": "orders"}}


class Rig:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        web_client: WebClientFactory,
        data: DataDb,
    ) -> None:
        self.factory, self.web_client, self.data = factory, web_client, data

    @asynccontextmanager
    async def open(
        self, *, as_: str = "owner", feature: bool = True
    ) -> AsyncIterator[tuple[httpx.AsyncClient, Workspace]]:
        ws = await funded_workspace(self.factory)
        if as_ in ("owner", "admin"):
            async with self.factory.begin() as db:
                await roles_store.set_workspace_role(
                    db,
                    tenant_id=ws.tenant_id,
                    account_id=ws.person.account_id,
                    role=as_,  # pyright: ignore[reportArgumentType]
                    granted_by_account_id=None,
                )
                await set_role(db, ws.person.account_id, Role.ADMIN)
        settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            database={"url": self.data.url},  # pyright: ignore[reportArgumentType]
            data={"schema_name": self.data.settings.schema_name},  # pyright: ignore[reportArgumentType]
            crypto={"keys": [Fernet.generate_key().decode()]},  # pyright: ignore[reportArgumentType]
            features={"data_sources": "on"} if feature else {},
        )
        token = await signed_in(self.factory, ws.person)
        async with self.web_client(
            web_settings(), sessionmaker=self.factory, settings=settings
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update(_OWN)
            yield client, ws


@pytest.fixture
async def rig(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    data_db: DataDb,
    tmp_path: Path,
) -> AsyncIterator[Rig]:
    yield Rig(db_session_factory, web_client, data_db)
    await close_pools()


async def test_feature_off_hides_the_routes(rig: Rig) -> None:
    async with rig.open(feature=False) as (client, _):
        for path in (
            "/api/data/sources",
            f"{_SOURCES}",
            "/api/admin/data/scopes",
            "/api/profile/data",
        ):
            res = await client.get(path)
            assert res.status_code == 403, f"{path} must be closed with the feature off: {res.text}"
        me = (await client.get("/api/me")).json()
        assert "data" not in me["modules"], "the data module is hidden with the feature off"


async def test_only_admins_write(rig: Rig) -> None:
    """Admins add sources; scopes and their grants need a data admin, who never grants
    themselves."""
    scope = {"id": "ops", "kind": "internal", "name": "Ops"}
    async with rig.open(as_="member") as (client, _):
        assert (await client.get("/api/data/sources")).status_code == 200, "members read"
        assert (await client.post(_SOURCES, json=_NEW)).status_code == 403
        assert (await client.post("/api/admin/data/scopes", json=scope)).status_code == 403
    async with rig.open(as_="owner") as (client, ws):
        assert (await client.post(_SOURCES, json=_NEW)).status_code == 201
        res = await client.post("/api/admin/data/scopes", json=scope)
        assert res.status_code == 403, "an owner who is not a data admin makes no scope"
        me = str(ws.person.account_id)
        res = await client.post("/api/admin/data/admins", json={"account_id": me})
        assert res.status_code == 204, res.text
        assert (await client.post("/api/admin/data/scopes", json=scope)).status_code == 201
        res = await client.post(
            "/api/admin/data/scopes/ops/grants", json={"holder": f"account:{me}"}
        )
        assert res.status_code == 403, "a data admin never grants themselves a scope"


async def test_declared_sources_refuse_edits(rig: Rig) -> None:
    async with rig.open() as (client, ws), rig.data.aconnect() as conn:
        cur = await conn.execute(
            f"insert into {rig.data.schemas.data}.sources (tenant_id, kind, name, default_scope,"
            " bound, declared_key) values (%s, 'postgres', 'Declared', 'internal', true, 'k')"
            " returning id".encode(),
            (ws.tenant_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        listed = (await client.get(_SOURCES)).json()
        assert [s["bound"] for s in listed] == [True], "the page can tell a declared source"
        for action in ("pause", "retire", "sync"):
            res = await client.post(f"{_SOURCES}/{row['id']}/{action}")
            assert res.status_code == 409, f"{action} must be refused on a declared source"
        after = await (
            await conn.execute(
                f"select status, publication from {rig.data.schemas.data}.sources where id = %s".encode(),
                (row["id"],),
            )
        ).fetchone()
        assert after == {"status": "active", "publication": "shadow"}, "the source is unchanged"


async def test_no_answer_holds_a_credential(rig: Rig) -> None:
    async with rig.open() as (client, _):
        made = await client.post(_SOURCES, json=_NEW)
        assert made.status_code == 201, made.text
        source = made.json()["id"]
        seen = [made.text]
        for path in (
            _SOURCES,
            f"{_SOURCES}/{source}/runs",
            "/api/integrations/data/kinds",
            "/api/data/sources",
            "/api/admin/data/scopes",
            "/api/profile/data",
        ):
            res = await client.get(path)
            assert res.status_code == 200, f"{path}: {res.text}"
            seen.append(res.text)
        everything = json.dumps(seen)
        assert "hunter2" not in everything and "db.example.com" not in everything, (
            "a source's connection string must never be sent back"
        )
        assert uuid.UUID(source), "the answer names the new source"

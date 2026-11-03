"""`/api/profile/*`: a person's own data, and nobody else's."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from daimon.core.config import Settings
from daimon.core.stores import mcp_tokens, web_identities, web_people
from daimon.core.stores.domain import WebPersonRow
from daimon.testing.turn_router import build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import Workspace, build_turns, funded_workspace, signed_in
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}


class Rig:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        web_client: WebClientFactory,
    ) -> None:
        self.factory, self.tmp_path, self.web_client = factory, tmp_path, web_client

    @asynccontextmanager
    async def open(
        self, *, delete_enabled: bool = True
    ) -> AsyncIterator[tuple[httpx.AsyncClient, Workspace]]:
        ws = await funded_workspace(self.factory)
        turns = build_turns(self.factory, build_turn_router(str(ws.tenant_id)), self.tmp_path)
        token = await signed_in(self.factory, ws.person)
        settings = Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
            privacy={"delete_enabled": delete_enabled},  # pyright: ignore[reportArgumentType]
        )
        async with self.web_client(
            web_settings(), sessionmaker=self.factory, turns=turns, settings=settings
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update(_OWN)
            yield client, ws


@pytest.fixture
def rig(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> Rig:
    return Rig(db_session_factory, tmp_path, web_client)


async def _key(
    factory: async_sessionmaker[AsyncSession], person: WebPersonRow, *, purpose: str = "coding"
) -> uuid.UUID:
    jti = uuid.uuid4()
    async with factory.begin() as db:
        await mcp_tokens.create_mcp_token_row(
            db,
            jti=jti,
            account_id=person.account_id,
            tenant_id=person.tenant_id,
            agent_id=str(uuid.uuid4()),
            label=None,
            created_at=datetime.now(UTC),
            purpose="coding" if purpose == "coding" else None,
        )
    return jti


async def test_signed_out_callers_are_refused(rig: Rig) -> None:
    async with rig.open() as (client, _):
        client.cookies.clear()
        assert (await client.get("/api/profile")).status_code == 401


async def test_the_profile_lists_the_persons_own_sign_ins_and_keys(rig: Rig) -> None:
    async with rig.open() as (client, ws):
        mine = await _key(rig.factory, ws.person)
        other = await funded_workspace(rig.factory)
        await _key(rig.factory, other.person)
        res = await client.get("/api/profile")
        assert res.status_code == 200, res.text
        body = res.json()
    assert body["name"] == "Ada", f"the profile names the person: {body}"
    assert [k["id"] for k in body["keys"]] == [str(mine)], f"only their own keys list: {body}"
    assert len(body["sign_ins"]) == 1, f"one sign-in is linked: {body}"
    assert body["memory"] == [] and body["connections"] == [], f"nothing else is held: {body}"


async def test_a_person_revokes_their_own_key_and_never_anothers(rig: Rig) -> None:
    async with rig.open() as (client, ws):
        mine = await _key(rig.factory, ws.person)
        other = await funded_workspace(rig.factory)
        theirs = await _key(rig.factory, other.person)
        assert (await client.delete(f"/api/profile/keys/{theirs}")).status_code == 404, (
            "another person's key is not reachable"
        )
        assert (await client.delete(f"/api/profile/keys/{mine}")).status_code == 200
        assert (await client.get("/api/profile")).json()["keys"] == [], "the revoked key is gone"
        async with rig.factory() as db:
            row = await mcp_tokens.get_mcp_token(db, jti=theirs)
        assert row is not None and row.revoked_at is None, "the other key is untouched"


async def test_the_last_sign_in_stays_and_a_spare_can_go(rig: Rig) -> None:
    async with rig.open() as (client, ws):
        only = (await client.get("/api/profile")).json()["sign_ins"][0]["id"]
        refused = await client.delete(f"/api/profile/sign-ins/{only}")
        assert refused.status_code == 409, f"the last sign-in is kept: {refused.text}"
        async with rig.factory.begin() as db:
            await web_identities.link_identity(
                db,
                tenant_id=ws.tenant_id,
                person_id=ws.person.id,
                provider="github",
                issuer="",
                subject="gh-1",
                label="ada",
                groups=(),
            )
        spare = next(
            i["id"]
            for i in (await client.get("/api/profile")).json()["sign_ins"]
            if i["provider"] == "github"
        )
        assert (await client.delete(f"/api/profile/sign-ins/{spare}")).status_code == 200
        assert len((await client.get("/api/profile")).json()["sign_ins"]) == 1


async def test_deleting_needs_the_name_and_the_setting(rig: Rig) -> None:
    async with rig.open() as (client, _):
        preview = (await client.get("/api/profile/delete")).json()
        assert preview["enabled"] is True and preview["held"], f"the preview lists data: {preview}"
        wrong = await client.post("/api/profile/delete", json={"confirm": "Bob"})
        assert wrong.status_code == 422, f"a wrong name deletes nothing: {wrong.text}"
    async with rig.open(delete_enabled=False) as (client, _):
        paused = await client.post("/api/profile/delete", json={"confirm": "Ada"})
        assert paused.status_code == 403, f"deletion can be paused: {paused.text}"


async def test_deleting_removes_the_person_and_ends_the_session(rig: Rig) -> None:
    async with rig.open() as (client, ws):
        done = await client.post("/api/profile/delete", json={"confirm": " ada "})
        assert done.status_code == 200, done.text
        async with rig.factory() as db:
            gone = await web_people.get_people(db, person_ids=[ws.person.id])
        assert gone == [], "the person's web row is erased"
        client.cookies.clear()
        assert (await client.get("/api/profile")).status_code == 401

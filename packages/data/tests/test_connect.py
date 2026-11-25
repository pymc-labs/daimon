"""A person's own sign-in at a source: code and device flows, conflicts, release and refresh."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.data import identity
from daimon.data.connect import Connector, Provider
from daimon.data.identity import Conflict, IdentityKey, Proof
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow

BASE = "https://id.example.com"
KEYS = [Fernet.generate_key().decode()]
"""One for the session: the registered client is kept in the store across tests."""
PROVIDERS = {
    "acme": Provider(
        "acme", "code", ("read",), issuer=BASE, whoami_url=f"{BASE}/me", email_key="email"
    ),
    "devco": Provider(
        "devco",
        "device",
        token_url=f"{BASE}/token",
        device_url=f"{BASE}/device",
        register_url=f"{BASE}/register",
        subject_key="user.id",
    ),
}


@dataclass
class FakeProvider:
    """Answers as the provider: who each code signs in, and the token answers to give."""

    users: dict[str, str] = field(default_factory=dict[str, str])
    device: list[str] = field(default_factory=lambda: ["authorization_pending", "approved"])
    refresh: list[tuple[int, dict[str, object]]] = field(
        default_factory=list[tuple[int, dict[str, object]]]
    )
    seen: list[str] = field(default_factory=list[str])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, form = request.url.path, parse_qs(request.content.decode())
        self.seen.append(path)
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": f"{BASE}/authorize",
                    "token_endpoint": f"{BASE}/token",
                    "registration_endpoint": f"{BASE}/register",
                },
            )
        if path == "/register":
            return httpx.Response(201, json={"client_id": "dyn-client"})
        if path == "/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "ABCD",
                    "verification_uri": f"{BASE}/activate",
                },
            )
        if path == "/me":
            user = request.headers["Authorization"].removeprefix("Bearer ")
            return httpx.Response(
                200, json={"sub": user, "email": f"{user}@example.com", "email_verified": True}
            )
        grant = form["grant_type"][0]
        if grant == "authorization_code":
            assert form["code_verifier"], "no PKCE verifier"
            return httpx.Response(
                200,
                json={
                    "access_token": self.users[form["code"][0]],
                    "refresh_token": "r1",
                    "expires_in": 3600,
                },
            )
        if grant == "refresh_token":
            self.seen.append(form["refresh_token"][0])
            status, body = self.refresh.pop(0)
            return httpx.Response(status, json=body)
        step = self.device.pop(0)
        if step != "approved":
            return httpx.Response(400, json={"error": step})
        return httpx.Response(200, json={"access_token": "t", "user": {"id": "dev-1"}})


@dataclass
class Rig:
    conn: AsyncConnection[DictRow]
    connector: Connector
    fake: FakeProvider
    tenant: uuid.UUID

    async def source(self, provider: str) -> uuid.UUID:
        row = await (
            await self.conn.execute(
                f"insert into {self.connector.schemas.data}.sources (tenant_id, kind, name, provider,"
                " default_scope, config) values (%s, 'docs', 'x', %s, 'internal', '{\"workspace\": \"w\"}')"
                " returning id".encode(),
                (self.tenant, provider),
            )
        ).fetchone()
        assert row, "a source insert returns its id"
        return row["id"]

    async def owner(self, provider: str, subject: str) -> tuple[uuid.UUID | None, str | None]:
        row = await (
            await self.conn.execute(
                f"select account_id, proof from {self.connector.schemas.data}.identities where tenant_id = %s"
                " and provider = %s and subject = %s".encode(),
                (self.tenant, provider, subject),
            )
        ).fetchone()
        return (row["account_id"], row["proof"]) if row else (None, None)

    async def sign_in(self, account: uuid.UUID, source: uuid.UUID, code: str):
        started = await self.connector.start(
            self.conn, tenant_id=self.tenant, account_id=account, source_id=source
        )
        state = parse_qs(urlparse(started.url).query)["state"][0]
        return await self.connector.finish_code(
            self.conn, account_id=account, state=state, code=code
        )


@pytest.fixture
async def rig(data_db: DataDb) -> AsyncIterator[Rig]:
    fake = FakeProvider()
    async with (
        data_db.aconnect() as conn,
        httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http,
    ):
        connector = Connector(
            data_db.schemas, KEYS, http, "https://daimon.example.com/callback", providers=PROVIDERS
        )
        yield Rig(conn, connector, fake, uuid.uuid4())


async def test_a_code_sign_in_links_who_signed_in_and_refuses_an_identity_of_someone_else(
    rig: Rig,
) -> None:
    me, you, src = uuid.uuid4(), uuid.uuid4(), await rig.source("acme")
    rig.fake.users.update({"c-me": "u-me", "c-you": "u-me"})
    done = await rig.sign_in(me, src, "c-me")
    assert done.made_link and await rig.owner("acme", "u-me") == (me, "oauth")
    email = await (
        await rig.conn.execute(
            f"select email, email_verified from {rig.connector.schemas.data}.identities where subject = 'u-me'".encode()
        )
    ).fetchone()
    assert email == {"email": "u-me@example.com", "email_verified": True}, (
        "the verified email is kept"
    )
    assert rig.fake.seen.count("/register") == 1, "the client is registered once"
    with pytest.raises(Conflict):
        await rig.sign_in(you, src, "c-you")
    assert await rig.connector.token(rig.conn, account_id=me, source_id=src) == "u-me"
    await rig.connector.disconnect(rig.conn, account_id=me, source_id=src)
    assert await rig.owner("acme", "u-me") == (None, None), "disconnect keeps the link it made"
    assert await rig.connector.token(rig.conn, account_id=me, source_id=src) is None


async def test_a_device_sign_in_waits_for_approval_and_leaves_a_link_it_did_not_make(
    rig: Rig,
) -> None:
    me, src = uuid.uuid4(), await rig.source("devco")
    await identity.link(
        rig.conn,
        rig.connector.schemas,
        tenant_id=rig.tenant,
        account_id=me,
        proof=Proof(IdentityKey("devco", "w", "dev-1"), "imported"),
    )
    started = await rig.connector.start(
        rig.conn, tenant_id=rig.tenant, account_id=me, source_id=src
    )
    assert started.user_code == "ABCD"
    assert await rig.connector.poll(rig.conn, account_id=me, pending_id=started.pending_id) is None
    done = await rig.connector.poll(rig.conn, account_id=me, pending_id=started.pending_id)
    assert done is not None and done.subject == "dev-1" and not done.made_link
    await rig.connector.disconnect(rig.conn, account_id=me, source_id=src)
    assert await rig.owner("devco", "dev-1") == (me, "imported")


async def test_an_ending_token_is_refreshed_with_rotation_and_a_refused_refresh_asks_to_reconnect(
    rig: Rig,
) -> None:
    me, src, d = uuid.uuid4(), await rig.source("acme"), rig.connector.schemas.data
    rig.fake.users["c"] = "u-1"
    await rig.sign_in(me, src, "c")
    end = f"update {d}.person_connections set access_expires_at = now() where account_id = %s".encode()
    answers: list[tuple[int, dict[str, object]]] = [
        (200, {"access_token": "a2", "refresh_token": "r2", "expires_in": 60}),
        (400, {"error": "invalid_grant"}),
    ]
    rig.fake.refresh += answers
    await rig.conn.execute(end, (me,))
    assert await rig.connector.token(rig.conn, account_id=me, source_id=src) == "a2"
    await rig.conn.execute(end, (me,))
    assert await rig.connector.token(rig.conn, account_id=me, source_id=src) is None
    row = await (
        await rig.conn.execute(
            f"select status, last_error from {d}.person_connections where account_id = %s".encode(),
            (me,),
        )
    ).fetchone()
    assert row == {"status": "reconnect", "last_error": "invalid_grant"}
    assert [t for t in rig.fake.seen if t.startswith("r")] == ["r1", "r2"], (
        "the rotated token is not used"
    )

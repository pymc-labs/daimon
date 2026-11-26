"""Who each source identity is: every proof, every refusal, and erasure."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
import pytest
from daimon.core import ports
from daimon.core.ports import erasure as erasure_port
from daimon.data import acl, identity
from daimon.data.access import ACL_SCOPE, ReadContext, person, scoped
from daimon.data.identity import (
    Conflict,
    Empty,
    IdentityKey,
    NotAllowed,
    Proof,
    SourceAccount,
    github_proof,
    sign_in_proof,
)
from daimon.data.ports import erasure
from daimon.data.schema import Schemas
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@dataclass
class People:
    conn: AsyncConnection[DictRow]
    schemas: Schemas
    tenant: uuid.UUID

    async def one(self, sql: str, *params: object) -> DictRow | None:
        return await (await self.conn.execute(sql.encode(), params)).fetchone()

    async def record_for(self, *refs: str, tenant: uuid.UUID | None = None) -> uuid.UUID:
        """A published source's record readable by exactly these subjects."""
        d = self.schemas.data
        async with self.conn.transaction():
            src = await self.one(
                f"insert into {d}.sources (tenant_id, kind, name, default_scope, publication)"
                " values (%s, 'docs', 'docs', %s, 'published') returning id",
                tenant or self.tenant,
                ACL_SCOPE,
            )
            assert src, "a source insert returns its id"
            list_id = await acl.intern(
                self.conn, self.schemas, src["id"], [acl.ref(r) for r in refs]
            )
            rec = await self.one(
                f"insert into {d}.records (source_id, external_id, kind, title, body, occurred_at, scope)"
                " values (%s, 'doc:1', 'doc', 'x', 'x', now(), %s) returning id",
                src["id"],
                ACL_SCOPE,
            )
            assert rec, "a record insert returns its id"
            await acl.set_records(
                self.conn,
                self.schemas,
                src["id"],
                [{"ext": "doc:1", "acl": list_id, "until": None}],
                datetime.now(UTC),
            )
        return rec["id"]

    async def reads(self, account: uuid.UUID, record: uuid.UUID) -> bool:
        ctx = ReadContext((self.tenant,), readers=(person(account),))
        async with scoped(self.conn, self.schemas, ctx) as c:
            rows = await (
                await c.execute(f"select id from {self.schemas.data}.records".encode())
            ).fetchall()
        return record in {r["id"] for r in rows}

    async def link(self, account: uuid.UUID, proof: Proof, tenant: uuid.UUID | None = None) -> None:
        await identity.link(
            self.conn,
            self.schemas,
            tenant_id=tenant or self.tenant,
            account_id=account,
            proof=proof,
        )

    async def listing(
        self, provider: str, *accounts: SourceAccount, domain: str | None = None
    ) -> list[str]:
        return await identity.record_accounts(
            self.conn,
            self.schemas,
            tenant_id=self.tenant,
            provider=provider,
            workspace="w",
            accounts=accounts,
            auto_link_domain=domain,
        )


@pytest.fixture
async def people(data_db: DataDb) -> AsyncIterator[People]:
    async with data_db.aconnect() as conn:
        yield People(conn, data_db.schemas, uuid.uuid4())


async def test_each_proof_makes_the_person_read_as_that_identity_in_its_tenant_only(
    people: People,
) -> None:
    proofs = [
        github_proof(42),
        sign_in_proof("slack", "T1", "U1", verified_email="me@example.com"),
        sign_in_proof("microsoft", "tid-1", "oid-1", verified_email="me@example.com"),
        Proof(IdentityKey("acme", "w", "u1"), "oauth"),
        Proof(IdentityKey("acme", "w", "u2"), "imported"),
    ]
    for proof in proofs:
        me = uuid.uuid4()
        record = await people.record_for(proof.key.ref())
        assert not await people.reads(me, record)
        await people.link(me, proof)
        assert await people.reads(me, record), proof.kind
    me, elsewhere = uuid.uuid4(), uuid.uuid4()
    await identity.mirror_platform(
        people.conn,
        people.schemas,
        tenant_id=people.tenant,
        platform="chat",
        workspace="g1",
        people={"p1": me},
    )
    assert await people.reads(me, await people.record_for("chat/g1:user:p1")), "platform"
    await people.link(me, Proof(IdentityKey("acme", "w", "x"), "oauth"), tenant=elsewhere)
    assert not await people.reads(me, await people.record_for("acme/w:user:x")), (
        "a link in another tenant reads here"
    )
    ms = await people.one(
        f"select email, email_verified from {people.schemas.data}.identities"
        " where provider = 'microsoft'"
    )
    assert ms == {"email": None, "email_verified": False}, "a Microsoft email is kept"
    await people.listing(
        "microsoft", SourceAccount("oid-2", email="x@example.com", email_verified=True)
    )
    assert not await identity.list_candidates(people.conn, people.schemas, people.tenant)


async def test_an_identity_linked_to_someone_else_is_refused(people: People) -> None:
    me, you = uuid.uuid4(), uuid.uuid4()
    proof = sign_in_proof("slack", "T1", "U1")
    await people.link(me, proof)
    await people.link(me, proof)
    with pytest.raises(Conflict):
        await people.link(you, proof)
    with pytest.raises(NotAllowed):
        await identity.admin_link(
            people.conn,
            people.schemas,
            tenant_id=people.tenant,
            by=me,
            key=proof.key,
            account_id=me,
        )


async def test_candidates_need_two_verified_emails_and_auto_link_respects_every_decision(
    people: People,
) -> None:
    me, you, third, admin = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await people.link(me, sign_in_proof("slack", "T1", "Um", verified_email="me@example.com"))
    await people.link(you, sign_in_proof("slack", "T1", "Uy", verified_email="you@example.com"))
    await people.link(third, sign_in_proof("slack", "T1", "Ut", verified_email="third@example.com"))
    await people.link(admin, github_proof(7))
    rows = [
        SourceAccount("a-me", email="ME@example.com", email_verified=True),
        SourceAccount("a-unverified", email="third@example.com"),
        SourceAccount("a-you", email="you@example.com", email_verified=True),
    ]
    assert await people.listing("acme", *rows) == []
    found = {
        (c.key.subject, c.account_id)
        for c in await identity.list_candidates(people.conn, people.schemas, people.tenant)
    }
    assert found == {("a-me", me), ("a-you", you)}, "an unverified email is a candidate"
    await identity.reject_candidate(
        people.conn,
        people.schemas,
        tenant_id=people.tenant,
        by=admin,
        key=IdentityKey("acme", "w", "a-you"),
        account_id=you,
        admin=True,
    )
    assert await people.listing("acme", *rows, domain="other.example") == [], "outside the domain"
    assert await people.listing("acme", *rows, domain="example.com") == ["a-me"], (
        "auto-link passed a rejected pair or an unverified email"
    )
    await people.listing(
        "dup",
        SourceAccount("d1", email="me@example.com", email_verified=True),
        SourceAccount("d2", email="me@example.com", email_verified=True),
    )
    assert (
        await people.listing(
            "dup",
            SourceAccount("d1", email="me@example.com", email_verified=True),
            SourceAccount("d2", email="me@example.com", email_verified=True),
            domain="example.com",
        )
        == []
    ), "an email two users share links"


async def test_a_workspace_admin_links_others_with_a_notice_and_a_trail_and_never_a_platform_unlink(
    people: People,
) -> None:
    d, admin, other, you = people.schemas.data, uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = IdentityKey("acme", "w", "u1")
    await people.listing("acme", SourceAccount("u1"))

    def link(by: uuid.UUID, to: uuid.UUID, *, admin: bool = True):
        return identity.admin_link(
            people.conn, people.schemas, tenant_id=people.tenant, by=by, key=key, account_id=to,
            admin=admin,
        )  # fmt: skip

    with pytest.raises(NotAllowed):
        await link(admin, you, admin=False)
    with pytest.raises(NotAllowed):
        await link(admin, admin)
    assert await link(admin, you) == "admin"
    with pytest.raises(Conflict):
        await link(admin, other)
    assert await people.reads(you, await people.record_for(key.ref()))
    assert (
        await identity.unlink(
            people.conn, people.schemas, tenant_id=people.tenant, by=admin, key=key, admin=True
        )
        == you
    )
    peeked = await identity.unseen_notices(people.conn, people.schemas, people.tenant, you)
    taken = await identity.take_notices(people.conn, people.schemas, people.tenant, you)
    assert peeked == taken, "looking leaves notices unseen; taking returns the same ones"
    assert [n.kind for n in taken] == ["linked", "unlinked"]
    assert {n.provider for n in taken} == {"acme"}, "a notice says which source it was"
    assert not await identity.take_notices(people.conn, people.schemas, people.tenant, you), (
        "seen twice"
    )
    trail = await people.one(
        f"select count(*) as n from {d}.audit where tenant_id = %s and account_id = %s"
        " and kind like 'identity.%%'",
        people.tenant,
        admin,
    )
    decided = await people.one(
        f"select decision from {d}.link_decisions where account_id = %s", you
    )
    assert trail == {"n": 2} and decided == {"decision": "unlinked"}
    with pytest.raises(NotAllowed):  # an own sign-in does not undo the admin's unlink
        await identity.link(
            people.conn,
            people.schemas,
            tenant_id=people.tenant,
            account_id=you,
            proof=Proof(key, "oauth"),
        )
    assert not await people.reads(you, await people.record_for(key.ref())), "it stays unlinked"
    await identity.mirror_platform(
        people.conn,
        people.schemas,
        tenant_id=people.tenant,
        platform="chat",
        workspace="g1",
        people={"p1": you},
    )
    with pytest.raises(Conflict):
        await identity.unlink(
            people.conn,
            people.schemas,
            tenant_id=people.tenant,
            by=admin,
            key=IdentityKey("chat", "g1", "p1"),
            admin=True,
        )


async def test_a_listing_drops_users_it_no_longer_lists_keeps_sign_ins_and_refuses_empty(
    people: People,
) -> None:
    me, you = uuid.uuid4(), uuid.uuid4()
    await people.listing("acme", SourceAccount("u1"), SourceAccount("u2"))
    await people.link(me, Proof(IdentityKey("acme", "w", "u1"), "imported"))
    await people.link(you, Proof(IdentityKey("acme", "w", "signed-in"), "oauth"))
    await people.listing("acme", SourceAccount("u2"))
    left = await people.conn.execute(
        f"select subject from {people.schemas.data}.identities where tenant_id = %s order by 1".encode(),
        (people.tenant,),
    )
    assert [r["subject"] for r in await left.fetchall()] == ["signed-in", "u2"]
    with pytest.raises(Empty):
        await people.listing("acme")


async def test_erasure_forgets_the_person_in_every_list_and_refuses_any_later_write(
    data_db: DataDb, people: People, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports.load()
    monkeypatch.setattr(erasure_port, "_REGISTERED", {s.key: s for s in erasure(data_db.schemas)})
    d, me = people.schemas.data, uuid.uuid4()
    proof = sign_in_proof("slack", "T1", "U1")
    await people.link(me, proof)
    await people.record_for(person(me), "acme/w:user:other")
    async with AsyncSession(db_engine) as session, session.begin():
        await erasure_port.erase_account(session, account_id=me)
    gone = await people.one(f"select count(*) as n from {d}.identities where account_id = %s", me)
    refs = await people.one(f"select count(*) as n from {d}.acl_refs where ref = %s", person(me))
    assert gone == {"n": 0} and refs == {"n": 0}, "erasure left a link or a list entry"
    with pytest.raises(NotAllowed):
        await people.link(me, proof)
    with pytest.raises(psycopg.errors.RaiseException):
        await people.conn.execute(
            f"insert into {d}.grants (tenant_id, scope, holder) values (%s, 'x', %s)".encode(),
            (people.tenant, person(me)),
        )

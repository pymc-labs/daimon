"""Who each source identity is: proofs, links, candidates and data admins (`0004_identity.sql`).

A link makes a person read as that identity, so one is written only
- for the person a provider or platform verified (`link`), at a web sign-in (Discord by
  `mirror_platform`, Slack and Microsoft `tid`+`oid` by `sign_in_proof`), a GitHub link
  or a person's own OAuth connect (`daimon.data.connect`);
- on an exact match of emails both providers verified, in an auto-link domain the source
  names (`record_accounts`, for a connector that lists accounts; none in this package
  does), never for a subject or account an admin decided on there;
- by a data admin for someone else (`admin_link`): never for themselves, with an audit
  row and a notice row (`take_notices`) for the person.
An email Microsoft reports proves nothing (any directory user may set it), so it is never
kept as verified.

Every writer runs its own transaction on a connection in autocommit and takes the account
lock that erasure takes, first, so nothing is written for an account being or already
erased. The caller resolves `tenant_id` and every account from its own signed facts.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from daimon.data.access import person, subject
from daimon.data.audit import record_audit
from daimon.data.schema import Schemas
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

ProofKind = Literal["platform", "github", "oauth", "slack", "microsoft", "imported"]
LinkProof = ProofKind | Literal["admin", "email", "auto"]
EMAIL_PROOFS = ("oauth", "slack", "imported")
"""Links whose verified email speaks for the person: their own sign-ins, and imports."""


class IdentityError(Exception):
    """A link change that does not apply; the message is safe to show."""


class Missing(IdentityError):
    """No such identity, link or candidate."""


class Conflict(IdentityError):
    """The identity is linked to someone else, or the change does not apply to it."""


class NotAllowed(IdentityError):
    """The caller may not make this change."""


class Empty(IdentityError):
    """A listing with no accounts for a workspace that had some: not believed."""


@dataclass(frozen=True)
class IdentityKey:
    provider: str
    workspace: str
    subject: str

    def ref(self) -> str:
        return subject(self.provider, self.workspace, "user", self.subject)


@dataclass(frozen=True)
class Proof:
    """What a provider verified: the identity, how, and an email it verified (if any)."""

    key: IdentityKey
    kind: ProofKind
    email: str | None = None


@dataclass(frozen=True)
class SourceAccount:
    """One user as a source lists them."""

    subject: str
    name: str = ""
    email: str | None = None
    email_verified: bool = False
    active: bool = True


@dataclass(frozen=True)
class Candidate:
    """An unlinked identity whose verified email matches a person's own verified email."""

    key: IdentityKey
    account_id: uuid.UUID
    email: str


def github_proof(github_user_id: int) -> Proof:
    """A GitHub account the person linked through Daimon's GitHub sign-in."""
    return Proof(IdentityKey("github", "github.com", str(github_user_id)), "github")


def sign_in_proof(
    provider: Literal["slack", "microsoft"],
    workspace: str,
    user_id: str,
    *,
    verified_email: str | None = None,
) -> Proof:
    """A Slack (team, user) or Microsoft (`tid`, `oid`) sign-in; a Microsoft email is dropped."""
    email = verified_email if provider == "slack" else None
    return Proof(IdentityKey(provider, workspace, user_id), provider, email)


async def fence_account(
    conn: AsyncConnection[DictRow], schemas: Schemas, *accounts: uuid.UUID | None
) -> None:
    """The account lock purge takes (core `privacy_session_deletes.lock_account`), then the
    tombstones: a write either commits before an erasure or sees it."""
    ids = sorted({a for a in accounts if a is not None})
    for account in ids:
        await conn.execute(
            b"select pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(account),)
        )
    cur = await conn.execute(
        f"select 1 from {schemas.data}.erased_accounts where account_id = any(%s)".encode(), (ids,)
    )
    if await cur.fetchone():
        raise NotAllowed("That account was erased.")


async def _row(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, key: IdentityKey
) -> DictRow | None:
    cur = await conn.execute(
        f"select * from {schemas.data}.identities where tenant_id = %s and provider = %s"
        " and workspace = %s and subject = %s for update".encode(),
        (tenant_id, key.provider, key.workspace, key.subject),
    )
    return await cur.fetchone()


async def _require_admin(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    if not await is_data_admin(conn, schemas, tenant_id, account_id):
        raise NotAllowed("Only a data admin can do this. Ask the owner.")


async def _decide(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    key: IdentityKey,
    account_id: uuid.UUID,
    decision: Literal["approved", "rejected", "unlinked"],
    by: uuid.UUID,
) -> None:
    await conn.execute(
        f"insert into {schemas.data}.link_decisions (tenant_id, provider, workspace, subject,"
        " account_id, decision, decided_by) values (%s, %s, %s, %s, %s, %s, %s)"
        " on conflict (tenant_id, provider, workspace, subject, account_id) do update set"
        " decision = excluded.decision, decided_by = excluded.decided_by,"
        " decided_at = now()".encode(),
        (tenant_id, key.provider, key.workspace, key.subject, account_id, decision, by),
    )


async def _notify(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    kind: Literal["linked", "unlinked"],
    key: IdentityKey,
) -> None:
    await conn.execute(
        f"insert into {schemas.data}.identity_notices (tenant_id, account_id, kind, detail)"
        " values (%s, %s, %s, %s)".encode(),
        (tenant_id, account_id, kind, Jsonb({"identity": key.ref()})),
    )


# ---- a person's own proofs -------------------------------------------------------------------


async def link(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    proof: Proof,
) -> None:
    """Link the identity `proof` verified to the person who presented it; a link they already
    hold keeps its proof. An identity linked to someone else raises Conflict, and one a data
    admin rejected or unlinked for them NotAllowed, writing nothing."""
    k = proof.key
    async with conn.transaction():
        await fence_account(conn, schemas, account_id)
        cur = await conn.execute(
            f"select 1 from {schemas.data}.link_decisions where tenant_id = %s and provider = %s"
            " and workspace = %s and subject = %s and account_id = %s"
            " and decision <> 'approved'".encode(),
            (tenant_id, k.provider, k.workspace, k.subject, account_id),
        )
        if await cur.fetchone() is not None:
            raise NotAllowed("A data admin unlinked that account from you. Ask one to link it.")
        cur = await conn.execute(
            f"insert into {schemas.data}.identities as i (tenant_id, provider, workspace, subject,"
            " account_id, proof, email, email_verified, linked_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, now())"
            " on conflict (tenant_id, provider, workspace, subject) do update set"
            " account_id = excluded.account_id, proof = coalesce(i.proof, excluded.proof),"
            " email = coalesce(excluded.email, i.email),"
            " email_verified = excluded.email is not null or i.email_verified,"
            " linked_at = coalesce(i.linked_at, now())"
            " where i.account_id is null or i.account_id = excluded.account_id"
            " returning 1".encode(),
            (
                tenant_id,
                k.provider,
                k.workspace,
                k.subject,
                account_id,
                proof.kind,
                proof.email,
                proof.email is not None,
            ),
        )
        if await cur.fetchone() is None:
            raise Conflict("That account is linked to someone else.")
        await record_audit(
            conn,
            schemas,
            tenant_id=tenant_id,
            kind="identity.link",
            account_id=account_id,
            target=k.ref(),
            detail={"proof": proof.kind},
        )


async def mirror_platform(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    workspace: str,
    people: Mapping[str, uuid.UUID],
) -> None:
    """The tenant's own platform principals (platform user id -> account) as identities of its
    `workspace`, the tenant's external id, each read as `user_subject(platform, workspace, id)`.
    Core admits a principal, so it is who that user is here and wins over any other link of
    the same user."""
    async with conn.transaction():
        await fence_account(conn, schemas, *people.values())
        for user_id, account_id in sorted(people.items()):
            await conn.execute(
                f"insert into {schemas.data}.identities as i (tenant_id, provider, workspace,"
                " subject, account_id, proof, linked_at)"
                " values (%s, %s, %s, %s, %s, 'platform', now())"
                " on conflict (tenant_id, provider, workspace, subject) do update set"
                " account_id = excluded.account_id, proof = 'platform', linked_by = null,"
                " linked_at = now() where i.account_id is distinct from excluded.account_id"
                " or i.proof is distinct from 'platform'".encode(),
                (tenant_id, platform, workspace, user_id, account_id),
            )


# ---- a source's listing, candidates and auto-links -------------------------------------------


async def record_accounts(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    provider: str,
    workspace: str,
    accounts: Sequence[SourceAccount],
    auto_link_domain: str | None = None,
) -> list[str]:
    """A source's whole account listing for its workspace. A user it listed before and no longer
    lists leaves with their link, so a reused id never inherits it. With `auto_link_domain`,
    links each exact verified-email match in that domain; returns the subjects linked."""
    d = schemas.data
    async with conn.transaction():
        cur = await conn.execute(
            f"select count(*) as n from {d}.identities where tenant_id = %s and provider = %s"
            " and workspace = %s and listed".encode(),
            (tenant_id, provider, workspace),
        )
        had = await cur.fetchone()
        if had and had["n"] and not accounts:
            raise Empty("the source listed no accounts")
        for a in accounts:
            await conn.execute(
                f"insert into {d}.identities (tenant_id, provider, workspace, subject, name, email,"
                " email_verified, active, listed) values (%s, %s, %s, %s, %s, %s, %s, %s, true)"
                " on conflict (tenant_id, provider, workspace, subject) do update set"
                " name = excluded.name, email = excluded.email, listed = true,"
                " email_verified = excluded.email_verified, active = excluded.active".encode(),
                (
                    tenant_id,
                    provider,
                    workspace,
                    a.subject,
                    a.name,
                    a.email,
                    a.email_verified and a.email is not None and provider != "microsoft",
                    a.active,
                ),
            )
        await conn.execute(
            f"delete from {d}.identities where tenant_id = %s and provider = %s and workspace = %s"
            " and listed and not (subject = any(%s))".encode(),
            (tenant_id, provider, workspace, [a.subject for a in accounts]),
        )
    domain = (auto_link_domain or "").strip().lower()
    return await _auto_link(conn, schemas, tenant_id, provider, workspace, domain) if domain else []


_CANDIDATES = """
select distinct i.provider, i.workspace, i.subject, j.account_id, i.email
from {d}.identities i
join {d}.identities j on j.tenant_id = i.tenant_id and j.account_id is not null
  and j.email_verified and lower(j.email) = lower(i.email) and j.proof = any(%(proofs)s)
where i.tenant_id = %(tenant)s and i.account_id is null and i.active and i.email_verified
  and not exists (select 1 from {d}.link_decisions x where x.tenant_id = i.tenant_id
    and x.provider = i.provider and x.workspace = i.workspace and x.subject = i.subject
    and x.account_id = j.account_id and x.decision <> 'approved')
"""


async def list_candidates(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID
) -> list[Candidate]:
    """Open link candidates for a data admin to approve or reject."""
    cur = await conn.execute(
        (_CANDIDATES.format(d=schemas.data) + " order by 1, 2, 3, 4").encode(),
        {"tenant": tenant_id, "proofs": list(EMAIL_PROOFS)},
    )
    return [
        Candidate(
            IdentityKey(r["provider"], r["workspace"], r["subject"]), r["account_id"], r["email"]
        )
        for r in await cur.fetchall()
    ]


_AUTO = """
select c.subject, c.account_id from ({candidates}) c
where c.provider = %(provider)s and c.workspace = %(workspace)s
  and lower(c.email) like '%%@' || %(domain)s
  and (select count(*) from {d}.identities y where y.tenant_id = %(tenant)s
       and y.provider = c.provider and y.workspace = c.workspace
       and lower(y.email) = lower(c.email)) = 1
  and (select count(distinct y.account_id) from {d}.identities y where y.tenant_id = %(tenant)s
       and y.account_id is not null and y.email_verified and lower(y.email) = lower(c.email)
       and y.proof = any(%(proofs)s)) = 1
"""
_UNDECIDED = """
not exists (select 1 from {d}.identities y where y.tenant_id = %(tenant)s
  and y.provider = %(provider)s and y.workspace = %(workspace)s and y.account_id = %(account)s)
and not exists (select 1 from {d}.link_decisions x where x.tenant_id = %(tenant)s
  and x.provider = %(provider)s and x.workspace = %(workspace)s
  and (x.subject = %(subject)s or x.account_id = %(account)s))
"""


async def _auto_link(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    provider: str,
    workspace: str,
    domain: str,
) -> list[str]:
    """Each match in its own transaction, fenced on its account and checked again there:
    never an account or subject an admin decided on, nor one already linked here."""
    d = schemas.data
    args: dict[str, object] = {
        "tenant": tenant_id,
        "provider": provider,
        "workspace": workspace,
        "domain": domain,
        "proofs": list(EMAIL_PROOFS),
    }
    cur = await conn.execute(_AUTO.format(candidates=_CANDIDATES.format(d=d), d=d).encode(), args)
    linked: list[str] = []
    for found in await cur.fetchall():
        one = {**args, "subject": found["subject"], "account": found["account_id"]}
        async with conn.transaction():
            await fence_account(conn, schemas, found["account_id"])
            done = await conn.execute(
                f"update {d}.identities set account_id = %(account)s, proof = 'auto',"
                " linked_at = now() where tenant_id = %(tenant)s and provider = %(provider)s"
                " and workspace = %(workspace)s and subject = %(subject)s"
                " and account_id is null and email_verified"
                f" and {_UNDECIDED.format(d=d)} returning 1".encode(),
                one,
            )
            if await done.fetchone() is None:
                continue
            key = IdentityKey(provider, workspace, found["subject"])
            await record_audit(
                conn,
                schemas,
                tenant_id=tenant_id,
                kind="identity.auto_link",
                target=key.ref(),
                detail={"account": str(found["account_id"])},
            )
            linked.append(found["subject"])
    return linked


# ---- data admins and their decisions ---------------------------------------------------------


async def is_data_admin(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    cur = await conn.execute(
        f"select 1 from {schemas.data}.data_admins where tenant_id = %s"
        " and account_id = %s".encode(),
        (tenant_id, account_id),
    )
    return await cur.fetchone() is not None


async def grant_data_admin(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    account_id: uuid.UUID,
    by_owner: bool,
) -> None:
    """Make `account_id` a data admin: only the tenant's owner (`by_owner`, which the caller
    checks) or another data admin may."""
    async with conn.transaction():
        await fence_account(conn, schemas, by, account_id)
        if not by_owner:
            await _require_admin(conn, schemas, tenant_id, by)
        await conn.execute(
            f"insert into {schemas.data}.data_admins (tenant_id, account_id, granted_by)"
            " values (%s, %s, %s) on conflict do nothing".encode(),
            (tenant_id, account_id, by),
        )
        await record_audit(
            conn,
            schemas,
            tenant_id=tenant_id,
            kind="data_admin.grant",
            account_id=by,
            target=person(account_id),
        )


async def revoke_data_admin(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    account_id: uuid.UUID,
    by_owner: bool,
) -> None:
    async with conn.transaction():
        await fence_account(conn, schemas, by)
        if not by_owner:
            await _require_admin(conn, schemas, tenant_id, by)
        await conn.execute(
            f"delete from {schemas.data}.data_admins where tenant_id = %s"
            " and account_id = %s".encode(),
            (tenant_id, account_id),
        )
        await record_audit(
            conn,
            schemas,
            tenant_id=tenant_id,
            kind="data_admin.revoke",
            account_id=by,
            target=person(account_id),
        )


async def admin_link(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    key: IdentityKey,
    account_id: uuid.UUID,
) -> LinkProof:
    """A data admin links a known identity to someone else: proof `email` when it is that
    person's open candidate, else `admin`. The person gets a notice; the trail gets a row."""
    if by == account_id:
        raise NotAllowed("You can't link an account to yourself. Ask another data admin.")
    async with conn.transaction():
        await fence_account(conn, schemas, by, account_id)
        await _require_admin(conn, schemas, tenant_id, by)
        row = await _row(conn, schemas, tenant_id, key)
        if row is None:
            raise Missing("No such account.")
        if row["account_id"] == account_id:
            return row["proof"]
        if row["account_id"] is not None:
            raise Conflict("That account is linked to someone else. Unlink it first.")
        matched = any(
            c.key == key and c.account_id == account_id
            for c in await list_candidates(conn, schemas, tenant_id)
        )
        proof: LinkProof = "email" if matched else "admin"
        await conn.execute(
            f"update {schemas.data}.identities set account_id = %s, proof = %s, linked_by = %s,"
            " linked_at = now() where tenant_id = %s and provider = %s and workspace = %s"
            " and subject = %s".encode(),
            (account_id, proof, by, tenant_id, key.provider, key.workspace, key.subject),
        )
        await _decide(conn, schemas, tenant_id, key, account_id, "approved", by)
        await record_audit(
            conn,
            schemas,
            tenant_id=tenant_id,
            kind="identity.admin_link",
            account_id=by,
            target=key.ref(),
            detail={"account": str(account_id), "proof": proof},
        )
        await _notify(conn, schemas, tenant_id, account_id, "linked", key)
    return proof


async def reject_candidate(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    key: IdentityKey,
    account_id: uuid.UUID,
) -> None:
    """The pair is never proposed again, and neither side is auto-linked in that workspace."""
    async with conn.transaction():
        await fence_account(conn, schemas, by, account_id)
        await _require_admin(conn, schemas, tenant_id, by)
        await _decide(conn, schemas, tenant_id, key, account_id, "rejected", by)
        await record_audit(
            conn,
            schemas,
            tenant_id=tenant_id,
            kind="identity.reject",
            account_id=by,
            target=key.ref(),
            detail={"account": str(account_id)},
        )


async def unlink(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    *,
    tenant_id: uuid.UUID,
    by: uuid.UUID,
    key: IdentityKey,
) -> uuid.UUID:
    """A data admin ends a link; the pair is not linked automatically again. A platform
    principal's link is who that user is here, so it stays. Returns who was unlinked."""
    cur = await conn.execute(
        f"select account_id from {schemas.data}.identities where tenant_id = %s and provider = %s"
        " and workspace = %s and subject = %s".encode(),
        (tenant_id, key.provider, key.workspace, key.subject),
    )
    seen = await cur.fetchone()
    async with conn.transaction():
        await fence_account(conn, schemas, by, seen["account_id"] if seen else None)
        await _require_admin(conn, schemas, tenant_id, by)
        row = await _row(conn, schemas, tenant_id, key)
        if row is None or row["account_id"] is None or seen is None:
            raise Missing("That account is not linked.")
        if row["account_id"] != seen["account_id"]:
            raise Conflict("That link just changed. Try again.")
        if row["proof"] == "platform":
            raise Conflict("That is the person's own chat account here; it stays linked.")
        account: uuid.UUID = row["account_id"]
        await conn.execute(
            f"update {schemas.data}.identities set account_id = null, proof = null,"
            " linked_by = null, linked_at = null where tenant_id = %s and provider = %s"
            " and workspace = %s and subject = %s".encode(),
            (tenant_id, key.provider, key.workspace, key.subject),
        )
        await _decide(conn, schemas, tenant_id, key, account, "unlinked", by)
        await record_audit(
            conn,
            schemas,
            tenant_id=tenant_id,
            kind="identity.unlink",
            account_id=by,
            target=key.ref(),
            detail={"account": str(account)},
        )
        await _notify(conn, schemas, tenant_id, account, "unlinked", key)
    return account


# ---- reads -----------------------------------------------------------------------------------


async def list_holders(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> list[str]:
    """Who a person holds grants as: their account and every identity linked to it here. With
    their platform roles, the holders `access.effective_scopes` takes."""
    return (await holders_of(conn, schemas, tenant_id, [account_id]))[account_id]


async def holders_of(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    tenant_id: uuid.UUID,
    account_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
    """`list_holders` for each of `account_ids`, in one query."""
    cur = await conn.execute(
        f"select a, s from unnest(%s::uuid[]) a, {schemas.data}.linked_subjects(a, %s) s"
        " order by a, s".encode(),
        (list(account_ids), [tenant_id]),
    )
    out = {a: [person(a)] for a in account_ids}
    for r in await cur.fetchall():
        out[r["a"]].append(str(r["s"]))
    return out


@dataclass(frozen=True)
class Notice:
    kind: Literal["linked", "unlinked"]
    identity: str


async def take_notices(
    conn: AsyncConnection[DictRow], schemas: Schemas, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> list[Notice]:
    """The person's unseen notices about links others changed, marked seen."""
    cur = await conn.execute(
        f"update {schemas.data}.identity_notices set seen_at = now() where tenant_id = %s"
        " and account_id = %s and seen_at is null returning kind, detail, created_at".encode(),
        (tenant_id, account_id),
    )
    rows = sorted(await cur.fetchall(), key=lambda r: r["created_at"])
    return [Notice(r["kind"], str(r["detail"]["identity"])) for r in rows]

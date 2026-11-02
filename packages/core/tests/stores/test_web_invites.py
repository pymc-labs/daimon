"""Real-Postgres tests for the invite and agent grant stores."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from daimon.core.stores import web_agent_grants as grants
from daimon.core.stores import web_invites as invites
from daimon.core.stores import web_people as people
from daimon.core.stores.domain import WebInviteRow
from daimon.testing.factories import make_tenant, make_web_person
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 5, 1, tzinfo=UTC)


async def _invite(db: AsyncSession, tenant_id: uuid.UUID, token: bytes) -> WebInviteRow:
    return await invites.create_invite(
        db,
        tenant_id=tenant_id,
        token_sha256=token,
        kind="invite",
        email="guest@example.com",
        credit_usd=Decimal("5.00"),
        agent_ids=["b", "a", "a"],
        expires_at=_NOW + timedelta(days=7),
    )


async def test_create_keeps_agents_once_and_lists_newest_first(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _invite(db_session, tenant.id, b"t1")
    second = await _invite(db_session, tenant.id, b"t2")
    await invites.create_invite(
        db_session,
        tenant_id=tenant.id,
        token_sha256=b"t3",
        kind="owner",
        expires_at=_NOW + timedelta(days=1),
    )

    listed = await invites.list_invites(db_session, tenant_id=tenant.id)

    assert len(listed) == 2, "owner and sign-in links are not invites"
    assert listed[0].agent_ids == ("a", "b"), "agents are stored once each"
    assert {i.id for i in listed} >= {second.id}, "the new invite is listed"


async def test_claim_is_single_use(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _invite(db_session, tenant.id, b"once")

    first = await invites.claim_invite(db_session, token_sha256=b"once", now=_NOW)
    again = await invites.claim_invite(db_session, token_sha256=b"once", now=_NOW)

    assert first is not None and first.accepted_at == _NOW, "the first claim accepts the invite"
    assert again is None, "a used link cannot be claimed twice"


async def test_claim_refuses_expired_and_revoked_links(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _invite(db_session, tenant.id, b"old")
    revoked = await _invite(db_session, tenant.id, b"gone")
    await invites.revoke_invite(db_session, tenant_id=tenant.id, invite_id=revoked.id, now=_NOW)

    late = await invites.claim_invite(db_session, token_sha256=b"old", now=_NOW + timedelta(days=8))
    gone = await invites.claim_invite(db_session, token_sha256=b"gone", now=_NOW)

    assert late is None, "an expired link is refused"
    assert gone is None, "a revoked link is refused"
    assert await invites.find_by_token(db_session, token_sha256=b"old") is not None, (
        "a refused claim leaves the link readable"
    )


async def test_revoking_closes_the_invites_agents(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    invite = await _invite(db_session, tenant.id, b"rv")
    before = await invites.invited_agent_ids(db_session, tenant_id=tenant.id, invite_id=invite.id)

    await invites.revoke_invite(db_session, tenant_id=tenant.id, invite_id=invite.id, now=_NOW)
    after = await invites.invited_agent_ids(db_session, tenant_id=tenant.id, invite_id=invite.id)

    assert before == {"a", "b"}, "an open invite reaches its agents"
    assert after == frozenset(), "a revoked invite reaches none"
    assert await invites.list_invites(db_session, tenant_id=tenant.id) == [], "revoked: unlisted"


async def test_accepted_invite_goes_with_its_person(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    person = await make_web_person(db_session, tenant=tenant)
    invite = await _invite(db_session, tenant.id, b"acc")
    await invites.set_accepted_by(db_session, invite_id=invite.id, person_id=person.id)

    await people.delete_for_account(db_session, account_id=person.account_id)

    assert await invites.find_by_token(db_session, token_sha256=b"acc") is None, (
        "erasing the person erases the invite and its email"
    )


async def test_grants_upsert_per_agent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await grants.set_grant(
        db_session,
        tenant_id=tenant.id,
        agent_id="a",
        web=True,
        who="groups",
        groups=["x", "x", "y"],
        now=_NOW,
    )
    updated = await grants.set_grant(
        db_session,
        tenant_id=tenant.id,
        agent_id="a",
        web=False,
        who="everyone",
        groups=[],
        now=_NOW,
    )

    listed = await grants.list_grants(db_session, tenant_id=tenant.id)

    assert updated.web is False and updated.who == "everyone", "the second write wins"
    assert list(listed) == ["a"], "one grant per agent"


async def test_revoke_open_links_only_touches_unused_links_of_that_kind_and_person(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    ada = await make_web_person(db_session, tenant=tenant)
    bob = await make_web_person(db_session, tenant=tenant)
    expires = _NOW + timedelta(days=1)
    made = {
        name: await invites.create_invite(
            db_session,
            tenant_id=tenant.id,
            token_sha256=name.encode(),
            kind=kind,
            person_id=person,
            expires_at=expires,
        )
        for name, kind, person in (
            ("ada-open", "sign_in", ada.id),
            ("ada-used", "sign_in", ada.id),
            ("bob-open", "sign_in", bob.id),
            ("owner", "owner", None),
        )
    }
    await invites.claim_invite(db_session, token_sha256=b"ada-used", now=_NOW)

    revoked = await invites.revoke_open_links(
        db_session, tenant_id=tenant.id, kind="sign_in", now=_NOW, person_id=ada.id
    )

    assert revoked == 1, "only Ada's unused sign-in link is revoked"
    for name, usable in (("ada-open", False), ("bob-open", True), ("owner", True)):
        row = await invites.get_invite(db_session, tenant_id=tenant.id, invite_id=made[name].id)
        assert row is not None and (row.revoked_at is None) is usable, f"{name} usable={usable}"
    everyone = await invites.revoke_open_links(
        db_session, tenant_id=tenant.id, kind="owner", now=_NOW
    )
    assert everyone == 1, "without a person it revokes every open link of the kind"

"""`/api/artifacts`: who sees which page, and what each role may change."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
from daimon.core.config import ArtifactPagesSettings
from daimon.core.stores import web_invites, web_people
from daimon.core.stores.domain import ArtifactRow, WebPersonRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN, make_admin
from .artifact_support import COOKIE, member_of, page_client, page_settings, publish, team
from .chat_support import visitor_of
from .conftest import web_settings


async def test_a_public_page_is_listed_only_to_those_who_manage_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    await publish(db_session_factory, t.owner, slug="notes")
    expires = datetime.now(UTC) + timedelta(days=3)
    await publish(db_session_factory, t.owner, slug="launch", access="public", expires_at=expires)

    async with page_client(db_session_factory, t.member) as client:
        seen = [a["slug"] for a in (await client.get("/api/artifacts")).json()]
    async with page_client(db_session_factory, t.owner) as client:
        mine = (await client.get("/api/artifacts")).json()

    assert seen == ["notes"]
    assert {a["slug"] for a in mine} == {"notes", "launch"}
    assert all(a["url"] == f"/a/{a['url'].split('/')[2]}/{a['slug']}" for a in mine)


async def test_sharing_takes_can_share_and_a_viewer_is_told_who_can_help(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(
        db_session_factory, t.owner, editors=(t.member.account_id,), editors_can_share=False
    )
    viewer = await publish(db_session_factory, t.owner, slug="board")

    async with page_client(db_session_factory, t.member) as client:
        shared = await client.patch(
            f"/api/artifacts/{page.id}", json={"access": "people"}, headers=OWN
        )
        renamed = await client.patch(
            f"/api/artifacts/{page.id}", json={"title": "Vote"}, headers=OWN
        )
        refused = await client.patch(
            f"/api/artifacts/{viewer.id}", json={"title": "X"}, headers=OWN
        )

    assert shared.status_code == 403 and shared.json() == {"detail": "Ask the owner or an admin."}
    assert renamed.status_code == 200 and renamed.json()["title"] == "Vote"
    assert refused.status_code == 403 and refused.json() == {"detail": "Ask the owner or an admin."}


async def test_a_page_transfers_to_an_editor_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, editors=(t.member.account_id,))
    other = await publish(db_session_factory, t.owner, slug="other")

    async with page_client(db_session_factory, t.owner) as client:
        url = f"/api/artifacts/{other.id}/transfer"
        refused = await client.post(url, json={"person": str(t.member.account_id)}, headers=OWN)
        url = f"/api/artifacts/{page.id}/transfer"
        moved = await client.post(url, json={"person": str(t.member.account_id)}, headers=OWN)

    assert refused.status_code == 422, refused.text
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["owner"]["id"] == str(t.member.account_id)
    assert [e["id"] for e in body["editors"]] == [str(t.owner.account_id)]


async def test_restore_refuses_the_current_version(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    await publish(db_session_factory, t.owner, html="<p>one</p>")
    page = await publish(db_session_factory, t.owner, html="<p>two</p>")

    async with page_client(db_session_factory, t.owner) as client:
        current = await client.post(f"/api/artifacts/{page.id}/versions/2/restore", headers=OWN)
        older = await client.post(f"/api/artifacts/{page.id}/versions/1/restore", headers=OWN)

    assert current.status_code == 422, current.text
    assert older.status_code == 200 and older.json()["version"] == 3


async def test_a_data_write_over_the_document_cap_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, data="write")

    async with page_client(db_session_factory, t.owner) as client:
        url = f"/api/artifacts/{page.id}/data/votes/a"
        saved = await client.put(url, json={"data": {"n": 1}}, headers=OWN)
        refused = await client.put(url, json={"data": {"n": "x" * 70_000}}, headers=OWN)

    assert saved.status_code == 200 and saved.json()["updated_by"] == "Ada"
    assert refused.status_code == 422 and "limit" in refused.json()["detail"]


async def test_sharing_by_email_makes_a_visitor_who_reaches_only_the_page(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A new address becomes a visitor with no agents and a sign-in link that lands on the
    page; a known address is added with no link; the sharer's daily cap holds."""
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, access="people")
    board = await publish(db_session_factory, t.owner, slug="board")
    capped = page_settings().model_copy(
        update={"artifact_pages": ArtifactPagesSettings(shares_per_day=2)}  # team() invited one
    )
    url = f"/api/artifacts/{page.id}/people"
    async with page_client(db_session_factory, t.owner, settings=capped) as client:
        made = await client.post(url, json={"email": " New@Example.com "}, headers=OWN)
        known = await client.post(url, json={"email": "BEA@example.com"}, headers=OWN)
        over = await client.post(url, json={"email": "other@example.com"}, headers=OWN)
        public = await client.post(
            f"/api/artifacts/{board.id}/people", json={"email": "x@example.com"}, headers=OWN
        )

    assert made.status_code == 200, made.text
    link = made.json()["invite_link"]
    token, next_page = link.split("/invite/")[1].split("?next=")
    assert unquote(next_page) == f"/a/{page.public_id}/poll", link
    assert known.status_code == 200 and known.json()["invite_link"] is None, known.text
    assert sorted(p["kind"] for p in known.json()["people"]) == ["member", "visitor"]
    assert over.status_code == 429 and public.status_code == 422, (over.text, public.text)

    async with db_session_factory() as db:
        [guest] = await web_people.list_people(db, tenant_id=t.tenant_id, search="new@example.com")
        assert guest.invite_id is not None, guest
        agents = await web_invites.invited_agent_ids(
            db, tenant_id=t.tenant_id, invite_id=guest.invite_id
        )
    assert (guest.kind, agents) == ("visitor", frozenset()), (guest, agents)
    async with page_client(db_session_factory) as client:
        assert (await client.get(f"/api/invites/{token}")).json()["agents"] == []
        accepted = await client.post(f"/api/invites/{token}/accept", headers=OWN)
        assert accepted.status_code == 200, accepted.text
        client.cookies.set(COOKIE, accepted.cookies[COOKIE], domain="web.example.com")
        assert (await client.get(f"/a/{page.public_id}/poll")).status_code == 200
        assert (await client.get(f"/a/{board.public_id}/board")).status_code == 404
        assert (await client.get("/api/artifacts")).status_code == 403


async def test_sharing_by_email_with_google_sign_in_returns_no_link(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With Google sign-in on, a new address becomes a visitor with no sign-in link; a
    known address behaves as before, and the inviter-or-admin rule still holds."""
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, access="people")
    other = await publish(db_session_factory, t.owner, slug="other", access="people")
    cy = await member_of(db_session_factory, t.tenant_id, "Cy")
    theirs = await publish(db_session_factory, cy, slug="theirs", access="people")
    google = web_settings(google_client_id="gid", google_client_secret="gsecret")
    url = f"/api/artifacts/{page.id}/people"
    async with page_client(db_session_factory, t.owner, web=google) as client:
        made = await client.post(url, json={"email": "New@Example.com"}, headers=OWN)
        again = await client.post(
            f"/api/artifacts/{other.id}/people", json={"email": "new@example.com"}, headers=OWN
        )
    async with page_client(db_session_factory, cy, web=google) as client:
        refused = await client.post(
            f"/api/artifacts/{theirs.id}/people", json={"email": "new@example.com"}, headers=OWN
        )

    assert made.status_code == 200 and made.json()["invite_link"] is None, made.text
    assert again.status_code == 200 and again.json()["invite_link"] is None, again.text
    assert refused.status_code == 403, "another member still cannot hand the visitor pages"
    async with db_session_factory() as db:
        [guest] = await web_people.list_people(db, tenant_id=t.tenant_id, search="new@example.com")
    assert guest.kind == "visitor", "the address still becomes a visitor, one for both pages"


async def test_only_the_inviter_or_an_admin_adds_their_visitor_in_the_page_settings(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The settings route holds the rule the email share holds: another member cannot
    add a visitor someone else invited, the inviter and an admin can."""
    t = await team(db_session_factory)
    cy = await member_of(db_session_factory, t.tenant_id, "Cy")
    guest = await visitor_of(
        db_session_factory, SimpleNamespace(tenant_id=t.tenant_id, person=t.member)
    )  # type: ignore[arg-type]
    theirs = await publish(db_session_factory, cy, slug="theirs", access="people")
    mine = await publish(db_session_factory, t.member, slug="mine", access="people")
    adds = {"people": [str(guest.account_id)]}

    async def patch(person: WebPersonRow, page: ArtifactRow) -> httpx.Response:
        async with page_client(db_session_factory, person) as client:
            return await client.patch(f"/api/artifacts/{page.id}", json=adds, headers=OWN)

    refused = await patch(cy, theirs)
    assert refused.status_code == 403, refused.text
    assert (await patch(t.member, mine)).status_code == 200, "the inviter adds them"
    await make_admin(db_session_factory, t.owner)
    owned = await publish(db_session_factory, t.owner, slug="owned", access="people")
    assert (await patch(t.owner, owned)).status_code == 200, "an admin adds them"


async def test_only_the_inviter_or_an_admin_shares_more_with_an_emailed_visitor(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Whoever made the visitor holds their sign-in link, so another member cannot hand
    them pages; the inviter and an admin can."""
    t = await team(db_session_factory)
    await make_admin(db_session_factory, t.owner)
    cy = await member_of(db_session_factory, t.tenant_id, "Cy")
    first = await publish(db_session_factory, t.member, slug="first", access="people")
    second = await publish(db_session_factory, t.member, slug="second", access="people")
    theirs = await publish(db_session_factory, cy, slug="theirs", access="people")
    admins = await publish(db_session_factory, t.owner, slug="admins", access="people")
    body = {"email": "guest@example.com"}

    async def share(person: WebPersonRow, page: ArtifactRow) -> httpx.Response:
        async with page_client(db_session_factory, person) as client:
            return await client.post(f"/api/artifacts/{page.id}/people", json=body, headers=OWN)

    assert (await share(t.member, first)).status_code == 200, "the inviter makes the visitor"
    refused = await share(cy, theirs)
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"detail": "An admin needs to share with guest@example.com."}
    assert (await share(t.member, second)).status_code == 200, "the inviter shares more"
    res = await share(t.owner, admins)
    assert res.status_code == 200, res.text

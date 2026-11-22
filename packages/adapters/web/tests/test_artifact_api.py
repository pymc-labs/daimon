"""`/api/artifacts`: who sees which page, and what each role may change."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN
from .artifact_support import page_client, publish, team


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

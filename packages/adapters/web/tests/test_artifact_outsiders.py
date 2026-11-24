"""An outsider (a Discord member with no internal role while `audience_hold` is on) opens
pages as a visitor does, on every web path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.artifact_pages.events import event_matches
from daimon.core.stores import artifact_pages as artifact_store
from daimon.core.stores.domain import CollectionRule, WebPersonRow
from daimon.core.stores.workspace_members import upsert_member
from daimon.testing.outsiders import INTERNAL_ROLE, hold_audience, link_discord_member
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN
from .artifact_support import member_of, page_client, publish, team
from .test_artifact_hosting import _png  # pyright: ignore[reportPrivateUsage]
from .test_artifact_pages import _page_watch, _path  # pyright: ignore[reportPrivateUsage]

Factory = async_sessionmaker[AsyncSession]


async def _held_team(
    factory: Factory,
) -> tuple[uuid.UUID, WebPersonRow, WebPersonRow, WebPersonRow]:
    """The tenant with `audience_hold` on: its owner and a member with the internal role,
    and an outsider."""
    t = await team(factory)
    outsider = await member_of(factory, t.tenant_id, "Out")
    async with factory.begin() as db:
        await hold_audience(db, t.tenant_id)
        for person, internal in ((t.owner, True), (t.member, True), (outsider, False)):
            await link_discord_member(
                db, tenant_id=t.tenant_id, account_id=person.account_id, internal=internal
            )
    return t.tenant_id, t.owner, t.member, outsider


async def test_an_outsider_opens_a_workspace_page_only_when_it_names_them(
    db_session_factory: Factory,
) -> None:
    _, owner, member, outsider = await _held_team(db_session_factory)
    plain = await publish(db_session_factory, owner)
    named = await publish(
        db_session_factory, owner, slug="named", access="people", people=(outsider.account_id,)
    )

    async with page_client(db_session_factory, outsider) as client:
        refused = await client.get(_path(plain))
        opened = await client.get(_path(named))
    async with page_client(db_session_factory, member) as client:
        insider = await client.get(_path(plain))

    assert refused.status_code == 404, "an outsider is not the workspace"
    assert opened.status_code == 200 and insider.status_code == 200


async def test_the_page_api_and_lists_treat_an_outsider_as_a_visitor(
    db_session_factory: Factory,
) -> None:
    _, owner, member, outsider = await _held_team(db_session_factory)
    page = await publish(db_session_factory, owner)

    async with page_client(db_session_factory, outsider) as client:
        listed = (await client.get("/api/artifacts")).json()
        detail = await client.get(f"/api/artifacts/{page.id}")
        called = await client.post(
            f"/a/{page.public_id}/_api/db.list", json={"collection": "votes"}, headers=OWN
        )
    async with page_client(db_session_factory, owner) as client:
        picker = {p["id"]: p["kind"] for p in (await client.get("/api/artifacts/people")).json()}
        editor = await client.patch(
            f"/api/artifacts/{page.id}", json={"editors": [str(outsider.account_id)]}, headers=OWN
        )
    async with page_client(db_session_factory, member) as client:
        seen = [a["slug"] for a in (await client.get("/api/artifacts")).json()]

    assert listed == [] and seen == [page.slug]
    assert detail.status_code == 404, "no details of a page they may not open"
    assert called.status_code in (403, 404), called.text
    assert picker[str(outsider.account_id)] == "visitor", "the sharing pickers list a guest"
    assert picker[str(member.account_id)] == "member"
    assert editor.status_code == 422, "a guest is never an editor"


async def test_an_outsiders_event_is_a_visitors_on_a_page_visible_outside(
    db_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    _, owner, _, outsider = await _held_team(db_session_factory)
    page = await publish(
        db_session_factory,
        owner,
        access="people",
        people=(outsider.account_id,),
        events=True,
    )

    async with page_client(db_session_factory, outsider) as client:
        sent = await client.post(
            f"/a/{page.public_id}/_api/notify", json={"event": "vote", "data": {}}, headers=OWN
        )

    assert sent.status_code == 200, sent.text
    async with db_session_factory() as db:
        event = await artifact_store.get_doc(db, page.id, "_events", sent.json()["id"])
    assert event is not None
    assert event.data["sender"] == "visitor" and event.data["by"] == "Out", event.data
    assert event.data["page_visible_outside_workspace"] is True, event.data
    assert event_matches({"sender": "visitor"}, event.data), "a `where` on visitors sees them"
    assert not event_matches({"sender": "member"}, event.data), "and not as a member"


async def test_a_watch_is_not_fed_once_its_owner_is_an_outsider(
    db_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    tenant_id, owner, member, _ = await _held_team(db_session_factory)
    page = await publish(db_session_factory, owner, events=True)
    await _page_watch(db_session_factory, tenant_id, page)  # owned by Discord user "123"

    async def role_of_watch_owner(*role_ids: str) -> None:
        async with db_session_factory.begin() as db:
            from daimon.core.stores.workspace_members import upsert_member

            await upsert_member(
                db,
                tenant_id=tenant_id,
                platform_user_id="123",
                is_admin=False,
                now=datetime.now(UTC),
                role_ids=list(role_ids),
            )

    async def delivered() -> int:
        async with page_client(db_session_factory, member) as client:
            sent = await client.post(
                f"/a/{page.public_id}/_api/notify",
                json={"event": "vote", "data": {}},
                headers=OWN,
            )
        async with db_session_factory() as db:
            event = await artifact_store.get_doc(db, page.id, "_events", sent.json()["id"])
        assert event is not None
        return int(event.data["delivered"])

    await role_of_watch_owner(INTERNAL_ROLE)
    inside = await delivered()
    await role_of_watch_owner("9002")
    outside = await delivered()

    assert (inside, outside) == (1, 0), "the watch's maker must still be able to use the page"


async def test_an_owner_or_editor_turned_outsider_manages_nothing_until_a_member_again(
    db_session_factory: Factory,
) -> None:
    """The stored role stays but reads as a visitor's on every web path that manages a page."""
    factory = db_session_factory
    t = await team(factory)
    async with factory.begin() as db:
        await hold_audience(db, t.tenant_id)
        await link_discord_member(
            db, tenant_id=t.tenant_id, account_id=t.owner.account_id, internal=True
        )
        bea = await link_discord_member(
            db, tenant_id=t.tenant_id, account_id=t.member.account_id, internal=True
        )
    rules = {"votes": CollectionRule(write="none")}
    mine = await publish(
        factory, t.member, slug="mine", access="people", data="write", collection_rules=rules
    )
    edited = await publish(
        factory,
        t.owner,
        slug="edited",
        access="people",
        editors=(t.member.account_id,),
        data="write",
        collection_rules=rules,
    )

    async def standing(*role_ids: str) -> None:
        async with factory.begin() as db:
            await upsert_member(
                db,
                tenant_id=t.tenant_id,
                platform_user_id=bea,
                is_admin=False,
                now=datetime.now(UTC),
                role_ids=list(role_ids),
            )

    png = {**OWN, "Content-Type": "image/png"}
    await standing("9002")
    async with page_client(factory, t.member) as client:
        for page in (mine, edited):
            base = f"/api/artifacts/{page.id}"
            seen = (await client.get(base)).json()
            refused = {
                "settings": await client.patch(base, json={"title": "X"}, headers=OWN),
                "share": await client.post(
                    f"{base}/people", json={"email": "new@example.com"}, headers=OWN
                ),
                "link": await client.post(f"{base}/link", headers=OWN),
                "versions": await client.get(f"{base}/versions"),
                "restore": await client.post(f"{base}/versions/1/restore", headers=OWN),
                "transfer": await client.post(
                    f"{base}/transfer", json={"person": str(t.owner.account_id)}, headers=OWN
                ),
                "delete": await client.delete(base, headers=OWN),
                "data": await client.put(
                    f"{base}/data/votes/a", json={"data": {"n": 1}}, headers=OWN
                ),
                "thumbnail": await client.put(
                    f"{base}/versions/1/thumbnail", content=_png(64, 40), headers=png
                ),
            }
            bound = await client.post(
                f"/a/{page.public_id}/_api/db.set",
                json={"path": "votes/a", "data": {"n": 1}},
                headers=OWN,
            )
            assert seen["role"] == "viewer" and not any(seen["can"].values()), (page.slug, seen)
            assert {k: r.status_code for k, r in refused.items()} == dict.fromkeys(refused, 403)
            assert bound.status_code == 403, f"{page.slug}: the collection rules bind a visitor"
    await standing(INTERNAL_ROLE)
    async with page_client(factory, t.member) as client:
        back = [
            await client.patch(f"/api/artifacts/{p.id}", json={"title": "Back"}, headers=OWN)
            for p in (mine, edited)
        ]

    assert [r.status_code for r in back] == [200, 200], "the stored role comes back"
    assert [r.json()["role"] for r in back] == ["owner", "editor"]

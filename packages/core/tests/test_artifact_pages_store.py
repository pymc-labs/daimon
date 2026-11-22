"""Artifact pages in Postgres: versions, sharing rules, shared data, events, cleanup."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import Artifact, ArtifactDocument, TaskContinuation, Watch
from daimon.core.artifact_pages import events
from daimon.core.artifact_pages.access import PageChange
from daimon.core.artifact_pages.documents import DocumentWrite, ListQuery
from daimon.core.artifact_pages.keys import page_event_key
from daimon.core.artifact_pages.model import ArtifactError, Limits
from daimon.core.continuity.page_events import sign_page_event
from daimon.core.stores import artifact_pages as store
from daimon.core.stores import watches, web_people
from daimon.core.stores.domain import AccountRow, ArtifactRow, TenantRow
from daimon.testing.factories import make_account, make_tenant, make_web_person
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .artifact_support import page_watch, watch_owner

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
_PUBLIC = PageChange(access="public", expires_at=NOW + timedelta(days=30))


async def _publish(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    slug: str = "runsheet",
    html: str = "<p>v</p>",
    change: PageChange | None = None,
    is_admin: bool = False,
    now: datetime = NOW,
) -> ArtifactRow:
    return await store.publish(
        db,
        tenant_id=tenant_id,
        agent_id="ag_1",
        account_id=account_id,
        is_admin=is_admin,
        slug=slug,
        title="Runsheet",
        html=html,
        change=change or PageChange(),
        origin=store.PublishOrigin("discord", "4242", "m1"),
        new_link=False,
        now=now,
    )


@pytest.fixture
async def tenant(db_session: AsyncSession) -> TenantRow:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    return await make_tenant(db_session)


async def test_publishing_adds_versions_and_keeps_the_newest(
    db_session: AsyncSession, tenant: TenantRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Limits, "VERSIONS_KEPT", 2)
    owner = await make_account(db_session, tenant=tenant)
    pages = [await _publish(db_session, tenant.id, owner.id, html=f"<p>{n}</p>") for n in range(3)]
    page = pages[-1]
    versions = await store.list_versions(db_session, page.id)
    assert [v.version for v in versions] == [3, 2], "pruned to the newest two"
    assert versions[0].html == "" and versions[0].origin_message_id == "m1", versions[0]
    refs = await store.published_in(
        db_session, platform="discord", thread_id="4242", message_id="m1", public_url="https://d"
    )
    assert [(r.version, r.url) for r in refs] == [(3, f"https://d/a/{page.public_id}/runsheet")]


async def test_a_public_link_is_stored_encrypted_and_rotates(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner = await make_account(db_session, tenant=tenant)
    page = await _publish(db_session, tenant.id, owner.id, change=_PUBLIC)
    stored = await db_session.scalar(select(Artifact.link_secret).where(Artifact.id == page.id))
    assert page.link_secret and stored and page.link_secret.encode() not in stored, "ciphertext"
    rotated = await store.rotate_link(db_session, page, role="owner", now=NOW)
    assert rotated.link_secret not in (None, page.link_secret), "a new link"
    private = await store.update_settings(
        db_session, rotated, PageChange(access="workspace"), role="owner", now=NOW
    )
    assert private.link_secret is None, "no link once it is not public"


async def test_a_slug_is_one_page_per_workspace_and_only_editors_republish(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner = await make_account(db_session, tenant=tenant)
    member = await make_account(db_session, tenant=tenant)
    page = await _publish(db_session, tenant.id, owner.id)
    with pytest.raises(ArtifactError) as refused:
        await _publish(db_session, tenant.id, member.id)
    assert refused.value.code == "forbidden", "a member cannot overwrite someone's page"
    again = await _publish(db_session, tenant.id, member.id, is_admin=True)
    assert (again.id, again.current_version) == (page.id, 2), "an admin may"
    other = await make_tenant(db_session)
    elsewhere = await _publish(
        db_session, other.id, (await make_account(db_session, tenant=other)).id
    )
    assert elsewhere.id != page.id, "the same slug in another workspace is its own page"


async def test_public_pages_list_only_to_the_people_who_manage_them(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner, editor, member = [await make_account(db_session, tenant=tenant) for _ in range(3)]
    await _publish(db_session, tenant.id, owner.id, slug="open")
    change = _PUBLIC.model_copy(update={"editors": (editor.id,)})
    await _publish(db_session, tenant.id, owner.id, slug="link", change=change)

    async def slugs(account: AccountRow, is_admin: bool = False) -> set[str]:
        rows = await store.list_visible(
            db_session, tenant_id=tenant.id, account_id=account.id, is_admin=is_admin
        )
        return {row.slug for row in rows}

    assert await slugs(member) == {"open"}
    assert (
        await slugs(editor)
        == await slugs(owner)
        == await slugs(member, True)
        == {
            "open",
            "link",
        }
    )


async def test_transfer_goes_to_an_editor_and_restore_copies_an_older_version(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner, editor, member = [await make_account(db_session, tenant=tenant) for _ in range(3)]
    change = PageChange(editors=(editor.id,), people=(editor.id,))
    page = await _publish(db_session, tenant.id, owner.id, change=change)
    page = await _publish(db_session, tenant.id, editor.id, html="<p>2</p>")
    with pytest.raises(ArtifactError, match="editor first"):
        await store.transfer(db_session, page, to_account_id=member.id, role="owner", now=NOW)
    with pytest.raises(ArtifactError):
        await store.transfer(db_session, page, to_account_id=editor.id, role="editor", now=NOW)
    moved = await store.transfer(db_session, page, to_account_id=editor.id, role="owner", now=NOW)
    assert (moved.owner_account_id, moved.editors, moved.people) == (editor.id, (owner.id,), ())
    with pytest.raises(ArtifactError, match="current version"):
        await store.restore(db_session, moved, 2, role="owner", account_id=editor.id, now=NOW)
    restored = await store.restore(
        db_session, moved, 1, role="editor", account_id=owner.id, now=NOW
    )
    version = await store.get_version(db_session, page.id, restored.current_version)
    assert version is not None and (version.version, version.html) == (3, "<p>v</p>"), version


async def test_a_visitor_can_open_a_page_but_never_edit_it(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner, guest = [await make_account(db_session, tenant=tenant) for _ in range(2)]
    person = await make_web_person(db_session, tenant=tenant, account=guest)
    await web_people.update_person(db_session, person_id=person.id, kind="visitor")
    with pytest.raises(ArtifactError, match="Only members"):
        await _publish(db_session, tenant.id, owner.id, change=PageChange(editors=(guest.id,)))
    page = await _publish(db_session, tenant.id, owner.id, change=PageChange(people=(guest.id,)))
    with pytest.raises(ArtifactError, match="Only members"):
        await store.update_settings(
            db_session, page, PageChange(editors=(guest.id,)), role="owner", now=NOW
        )
    assert await store.visible_outside_workspace(db_session, page)


async def test_data_writes_stay_under_the_caps_and_move_the_revision(
    db_session: AsyncSession, tenant: TenantRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Limits, "DOCUMENTS", 2)
    owner = await make_account(db_session, tenant=tenant)
    page = await _publish(db_session, tenant.id, owner.id, change=PageChange(data="write"))

    async def put(doc_id: str, value: int, mode: str = "set") -> int:
        write = DocumentWrite.model_validate({"mode": mode, "data": {"n": value}})
        _, revision = await store.write_doc(
            db_session, page, "votes", doc_id, write, by="Ines", account_id=owner.id, now=NOW
        )
        return revision

    assert [await put("a", 2), await put("b", 1), await put("a", 3, "merge")] == [1, 2, 3]
    with pytest.raises(ArtifactError, match="at most"):
        await put("c", 0)
    rows = await store.list_docs(db_session, page.id, ListQuery(collection="votes", order_by="n"))
    assert [(r.doc_id, r.data) for r in rows] == [("b", {"n": 1}), ("a", {"n": 3})]
    assert await store.delete_doc(db_session, page, "votes", "a") == 4
    assert await store.delete_doc(db_session, page, "votes", "a") == 4, "nothing to delete"


async def test_events_keep_only_the_newest(
    db_session: AsyncSession, tenant: TenantRow, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Limits, "EVENTS", 2)
    owner = await make_account(db_session, tenant=tenant)
    page = await _publish(db_session, tenant.id, owner.id, change=PageChange(events=True))
    ids = [uuid.uuid4() for _ in range(3)]
    for n, event_id in enumerate(ids):
        at = NOW + timedelta(seconds=n)
        event = events.build_event(
            slug=page.slug,
            name="vote",
            data=n,
            sender="member",
            by="Ines",
            visible_outside_workspace=False,
            event_id=event_id,
            now=at,
        )
        await store.add_event(db_session, page, event, account_id=owner.id, now=at)
    kept = await db_session.scalars(
        select(ArtifactDocument.doc_id).where(ArtifactDocument.collection == "_events")
    )
    assert set(kept) == {str(ids[1]), str(ids[2])}
    assert await store.usage(db_session, page.id) == store.ArtifactUsage(documents=0, bytes=0)


async def test_erasure_deletes_owned_pages_and_scrubs_the_rest(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner, gone = [await make_account(db_session, tenant=tenant) for _ in range(2)]
    theirs = await _publish(db_session, tenant.id, gone.id, slug="theirs")
    change = PageChange(people=(gone.id,), editors=(gone.id,), data="write", events=True)
    page = await _publish(db_session, tenant.id, owner.id, change=change)
    write = DocumentWrite(data={"by": "Gone"})
    await store.write_doc(
        db_session, page, "notes", "n1", write, by="Gone", account_id=gone.id, now=NOW
    )
    event = events.build_event(
        slug=page.slug,
        name="vote",
        data=1,
        sender="member",
        by="Gone",
        visible_outside_workspace=False,
        event_id=uuid.uuid4(),
        now=NOW,
    )
    await store.add_event(db_session, page, event, account_id=gone.id, now=NOW)
    assert await store.count_owned(db_session, account_id=gone.id) == 1
    assert await store.count_mentions(db_session, account_id=gone.id) == 3
    assert await store.delete_owned(db_session, account_id=gone.id) == 1
    assert await store.scrub_account(db_session, account_id=gone.id) == 3
    assert await store.get(db_session, theirs.id) is None
    db_session.expire_all()
    kept = await store.get(db_session, page.id)
    assert kept is not None and (kept.people, kept.editors) == ((), ()), kept
    docs = {
        row.collection: row
        for row in await db_session.scalars(
            select(ArtifactDocument).where(ArtifactDocument.artifact_id == page.id)
        )
    }
    assert docs["notes"].data == {"by": "Gone"}, "the page's own data stays as written"
    assert (docs["notes"].updated_by, docs["_events"].updated_by) == (None, None)
    assert "by" not in docs["_events"].data, "the event no longer names them"


@pytest.mark.fresh_schema
async def test_deleting_a_page_removes_its_watches_and_their_queued_turns(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    keys = (Fernet.generate_key().decode(),)
    db_session.info["crypto_keys"] = keys
    binding = await watch_owner(db_session, monkeypatch)
    page = await _publish(db_session, binding.tenant_id, binding.requester_account_id, now=now)
    watch, key_id = await page_watch(
        db_session, binding, page_id=page.id, crypto_keys=keys, now=now
    )
    key = page_event_key(keys, watch.id, key_id)
    assert key is not None
    signed = sign_page_event(
        key=key, watch_id=watch.id, page_id=page.id, key_id=key_id, body=b"{}", now=now
    )
    assert await watches.accept_page_event(db_session, signed, key=key) is not None
    await store.delete_artifact(db_session, page, role="owner", now=now)
    assert await db_session.get(Watch, watch.id) is None
    pending = await db_session.scalar(
        select(func.count()).where(
            TaskContinuation.tenant_id == binding.tenant_id, TaskContinuation.status == "pending"
        )
    )
    assert pending == 0, "a deleted page wakes nobody"


async def test_expired_pages_go_after_the_grace_period(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner = await make_account(db_session, tenant=tenant)
    page = await _publish(db_session, tenant.id, owner.id, change=_PUBLIC)
    expiry = NOW + timedelta(days=30)
    assert await store.purge_expired(db_session, now=expiry + store.PURGE_AFTER) == 0
    later = expiry + store.PURGE_AFTER + timedelta(seconds=1)
    assert await store.purge_expired(db_session, now=later) == 1
    assert await store.get(db_session, page.id) is None

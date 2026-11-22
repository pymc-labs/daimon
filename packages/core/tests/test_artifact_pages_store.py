"""Artifact pages in Postgres: versions, sharing rules, shared data, events, cleanup."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import Artifact, ArtifactDocument, TaskContinuation, Watch, WatchFiring
from daimon.core.artifact_pages import events
from daimon.core.artifact_pages.access import PageChange
from daimon.core.artifact_pages.documents import DocumentWrite, ListQuery
from daimon.core.artifact_pages.keys import page_event_key
from daimon.core.artifact_pages.model import ArtifactError
from daimon.core.config import ArtifactPagesSettings
from daimon.core.continuity.page_events import sign_page_event
from daimon.core.stores import artifact_pages as store
from daimon.core.stores import watches, web_people
from daimon.core.stores.domain import AccountRow, ArtifactRow, CollectionRule, TenantRow
from daimon.testing.factories import make_account, make_tenant, make_web_person
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .artifact_support import page_watch, watch_owner

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
_PUBLIC = PageChange(access="public", expires_at=NOW + timedelta(days=30))
LIMITS = ArtifactPagesSettings()


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
    limits: ArtifactPagesSettings = LIMITS,
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
        limits=limits,
    )


@pytest.fixture
async def tenant(db_session: AsyncSession) -> TenantRow:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    return await make_tenant(db_session)


async def test_publishing_adds_versions_and_keeps_the_newest(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner = await make_account(db_session, tenant=tenant)
    kept = ArtifactPagesSettings(versions_kept=2)
    pages = [
        await _publish(db_session, tenant.id, owner.id, html=f"<p>{n}</p>", limits=kept)
        for n in range(3)
    ]
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
        db_session,
        rotated,
        PageChange(access="workspace"),
        role="owner",
        account_id=owner.id,
        now=NOW,
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
        await store.restore(
            db_session, moved, 2, role="owner", account_id=editor.id, now=NOW, limits=LIMITS
        )
    restored = await store.restore(
        db_session, moved, 1, role="editor", account_id=owner.id, now=NOW, limits=LIMITS
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
            db_session,
            page,
            PageChange(editors=(guest.id,)),
            role="owner",
            account_id=owner.id,
            now=NOW,
        )
    assert await store.visible_outside_workspace(db_session, page)


async def test_data_writes_stay_under_the_caps_and_move_the_revision(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    limits = ArtifactPagesSettings(documents=2)
    owner = await make_account(db_session, tenant=tenant)
    page = await _publish(db_session, tenant.id, owner.id, change=PageChange(data="write"))

    async def put(doc_id: str, value: int, mode: str = "set") -> int:
        write = DocumentWrite.model_validate({"mode": mode, "data": {"n": value}})
        _, revision = await store.write_doc(
            db_session,
            page,
            "votes",
            doc_id,
            write,
            by="Ines",
            account_id=owner.id,
            rule=None,
            now=NOW,
            limits=limits,
        )
        return revision

    assert [await put("a", 2), await put("b", 1), await put("a", 3, "merge")] == [1, 2, 3]
    with pytest.raises(ArtifactError, match="at most"):
        await put("c", 0)
    query = ListQuery(collection="votes", order_by="n", limit=10)
    rows = await store.list_docs(db_session, page.id, query, limits=limits)
    assert [(r.doc_id, r.data) for r in rows] == [("b", {"n": 1}), ("a", {"n": 3})]
    assert [r.revision for r in rows] == [2, 3], "each document keeps the revision it made"
    gone = await store.delete_doc(db_session, page, "votes", "a", rule=None, account_id=None)
    assert gone == 4, "a delete moves the revision"
    again = await store.delete_doc(db_session, page, "votes", "a", rule=None, account_id=None)
    assert again == 4, "nothing to delete"


async def test_events_keep_only_the_newest(db_session: AsyncSession, tenant: TenantRow) -> None:
    limits = ArtifactPagesSettings(events=2)
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
            max_bytes=limits.event_bytes,
        )
        await store.add_event(db_session, page, event, account_id=owner.id, now=at, limits=limits)
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
        db_session,
        page,
        "notes",
        "n1",
        write,
        by="Gone",
        account_id=gone.id,
        rule=None,
        now=NOW,
        limits=LIMITS,
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
        max_bytes=LIMITS.event_bytes,
    )
    await store.add_event(db_session, page, event, account_id=gone.id, now=NOW, limits=LIMITS)
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
    assert docs["notes"].created_by_account_id is None, "nor who created it"
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


async def _put(
    db: AsyncSession,
    page: ArtifactRow,
    doc_id: str,
    data: dict[str, object],
    *,
    account_id: uuid.UUID | None,
    rule: CollectionRule | None = None,
    now: datetime = NOW,
) -> int:
    write = DocumentWrite.model_validate({"data": data})
    _, revision = await store.write_doc(
        db,
        page,
        "votes",
        doc_id,
        write,
        by="x",
        account_id=account_id,
        rule=rule,
        now=now,
        limits=LIMITS,
    )
    return revision


async def test_own_rules_keep_viewers_to_the_documents_they_created(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner, ines, tom = [await make_account(db_session, tenant=tenant) for _ in range(3)]
    page = await _publish(db_session, tenant.id, owner.id, change=PageChange(data="write"))
    own = CollectionRule(read="own", write="own")
    await _put(db_session, page, "a", {"n": 1}, account_id=ines.id, rule=own)
    with pytest.raises(ArtifactError) as refused:
        await _put(db_session, page, "a", {"n": 2}, account_id=tom.id, rule=own)
    assert refused.value.code == "not_allowed", "not their document"
    with pytest.raises(ArtifactError):
        await store.delete_doc(db_session, page, "votes", "a", rule=own, account_id=tom.id)
    await _put(db_session, page, "a", {"n": 3}, account_id=owner.id)  # managers are not bound
    doc = await store.get_doc(db_session, page.id, "votes", "a")
    assert doc is not None and doc.created_by_account_id == ines.id, "the creator never changes"
    query = ListQuery(collection="votes", limit=10, created_by=tom.id)
    assert await store.list_docs(db_session, page.id, query, limits=LIMITS) == [], "none of tom's"


async def test_lists_go_by_creation_with_unordered_documents_last(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner = await make_account(db_session, tenant=tenant)
    page = await _publish(db_session, tenant.id, owner.id, change=PageChange(data="write"))
    for n, (doc_id, data) in enumerate([("b", {}), ("a", {"n": 2}), ("c", {"n": 1})]):
        await _put(db_session, page, doc_id, data, account_id=owner.id, now=NOW + timedelta(n))

    async def ids(**query: object) -> list[str]:
        listed = ListQuery.model_validate({"collection": "votes", "limit": 10, **query})
        return [d.doc_id for d in await store.list_docs(db_session, page.id, listed, limits=LIMITS)]

    assert await ids(by_creation=True) == ["b", "a", "c"], "oldest first"
    assert await ids(order_by="n") == ["c", "a", "b"], "without the field last"
    assert await ids(order_by="n", descending=True) == ["a", "c", "b"], "last either way"


async def test_editors_without_sharing_may_leave_but_keep_an_outside_pages_data(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    owner, editor, guest = [await make_account(db_session, tenant=tenant) for _ in range(3)]
    person = await make_web_person(db_session, tenant=tenant, account=guest)
    await web_people.update_person(db_session, person_id=person.id, kind="visitor")
    change = PageChange(editors=(editor.id,), people=(guest.id,), editors_can_share=False)
    page = await _publish(db_session, tenant.id, owner.id, change=change)
    with pytest.raises(ArtifactError, match="Ask the owner"):
        await store.update_settings(
            db_session, page, PageChange(data="read"), role="editor", account_id=editor.id, now=NOW
        )
    left = await store.update_settings(
        db_session, page, PageChange(editors=()), role="editor", account_id=editor.id, now=NOW
    )
    assert left.editors == (), "an editor may always leave"
    forever = await _publish(db_session, tenant.id, owner.id, slug="forever", change=_PUBLIC)
    forever = await store.update_settings(
        db_session, forever, PageChange(expires_at=None), role="owner", account_id=owner.id, now=NOW
    )
    assert forever.expires_at is None, "a public page may be permanent"


async def test_events_kept_scale_with_the_data_scale(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    limits = ArtifactPagesSettings(events=1)
    owner = await make_account(db_session, tenant=tenant)
    change = PageChange(events=True, data_scale=2)
    page = await _publish(db_session, tenant.id, owner.id, change=change)
    for n in range(3):
        event = events.build_event(
            slug=page.slug,
            name="vote",
            data=n,
            sender="member",
            by=None,
            visible_outside_workspace=False,
            event_id=uuid.uuid4(),
            now=NOW + timedelta(seconds=n),
            max_bytes=limits.event_bytes,
        )
        await store.add_event(db_session, page, event, account_id=None, now=NOW, limits=limits)
    kept = await db_session.scalar(
        select(func.count()).where(ArtifactDocument.collection == "_events")
    )
    assert kept == 2, "one event times a data scale of two"


@pytest.mark.fresh_schema
async def test_a_turn_woken_by_an_outside_pages_event_is_known(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    keys = (Fernet.generate_key().decode(),)
    db_session.info["crypto_keys"] = keys
    binding = await watch_owner(db_session, monkeypatch)
    page = await _publish(
        db_session,
        binding.tenant_id,
        binding.requester_account_id,
        change=PageChange(access="public", events=True),
        now=now,
    )
    watch, key_id = await page_watch(
        db_session, binding, page_id=page.id, crypto_keys=keys, now=now
    )
    key = page_event_key(keys, watch.id, key_id)
    assert key is not None
    signed = sign_page_event(
        key=key, watch_id=watch.id, page_id=page.id, key_id=key_id, body=b"{}", now=now
    )
    assert await watches.accept_page_event(db_session, signed, key=key) is not None

    async def woken() -> bool:
        return await store.woken_by_outside_page(
            db_session, tenant_id=binding.tenant_id, platform="discord", thread_id="4242"
        )

    assert not await woken(), "a queued wake is not running yet"
    await db_session.execute(
        update(TaskContinuation)
        .where(TaskContinuation.tenant_id == binding.tenant_id)
        .values(status="claimed")
    )
    assert await woken(), "the running turn came from a public page's event"
    firing = await db_session.scalar(select(WatchFiring.wake_key))
    assert firing is not None
    await watches.record_delivery_thread(db_session, firing, "9999")
    assert await store.woken_by_outside_page(
        db_session, tenant_id=binding.tenant_id, platform="discord", thread_id="9999"
    ), "a report in a new thread is the same turn"


@pytest.mark.fresh_schema
async def test_a_turn_woken_by_a_poll_of_an_outside_pages_data_is_known(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    binding = await watch_owner(db_session, monkeypatch)
    page = await _publish(
        db_session,
        binding.tenant_id,
        binding.requester_account_id,
        change=PageChange(access="public", events=True),
        now=now,
    )
    watch = await watches.register_poll_watch(
        db_session,
        binding,
        instructions="Say when a vote lands.",
        tool=watches.WEB_POLL_TOOL,
        arguments={"slug": page.slug, "collection": "votes", "limit": 20},
        projection=".rows",
        rule="change",
        interval=60,
        expires_at=now + timedelta(days=1),
        now=now,
    )

    async def poll(value: int) -> None:
        await db_session.execute(
            update(Watch).where(Watch.id == watch.id).values(next_check_at=now - timedelta(hours=1))
        )
        (claimed,) = [
            r
            for r in await watches.claim_poll_watches(db_session)
            if r["watch_id"] == str(watch.id)
        ]
        leased = await watches.leased_poll(db_session, watch.id, claimed["lease"], now=now)
        assert leased is not None
        await watches.finish_poll(db_session, leased, claimed["lease"], value=value, payload="x")

    await poll(1)
    await poll(2)
    await db_session.execute(
        update(TaskContinuation)
        .where(TaskContinuation.tenant_id == binding.tenant_id)
        .values(status="claimed")
    )
    assert await store.woken_by_outside_page(
        db_session, tenant_id=binding.tenant_id, platform="discord", thread_id="4242"
    ), "the running turn came from a poll of a public page's data"

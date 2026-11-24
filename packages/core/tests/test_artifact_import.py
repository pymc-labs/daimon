"""Importing pages exported by another system: the bundle, the compat scan and the store."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import Artifact, ArtifactDocument, ArtifactVersion, Watch, WebIdentity
from daimon.core.artifact_pages.access import PageChange
from daimon.core.artifact_pages.documents import DocumentWrite
from daimon.core.artifact_pages.events import MAX_WATCHES_PER_PAGE
from daimon.core.artifact_pages.importing import Bundle, PageIn, compat_warnings
from daimon.core.artifact_pages.model import page_url
from daimon.core.config import ArtifactPagesSettings
from daimon.core.continuity.watch_imports import ImportedCallbackWatch
from daimon.core.stores import artifact_pages, watches
from daimon.core.stores.artifact_import import ImportTarget, import_page
from daimon.core.stores.domain import ArtifactRow, TenantRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.outsiders import hold_audience, link_discord_member
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
LEGACY = "https://pages.example.org/a/q3-plan"
SECRET = "s3cret-link-value-0123456789"
OWNER, EDITOR, MEMBER = "1001", "1002", "1003"


def _page(**over: Any) -> PageIn:
    body: dict[str, Any] = {
        "legacy_url": LEGACY,
        "public_id": "abcdefgh23",
        "slug": "q3-plan",
        "title": "Q3 plan",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-02-01T00:00:00Z",
        "owner": {"discord_id": OWNER},
        "editors": [{"discord_id": EDITOR}],
        "access": "public",
        "link_secret": SECRET,
        "data": "read",
        "events": False,
        "data_revision": 7,
        "current_version": 2,
        "versions": [
            {"version": 1, "html": "<p>one</p>", "created_at": "2026-01-01T00:00:00Z"},
            {
                "version": 2,
                "html": "<p>two</p>",
                "published_by": {"discord_id": EDITOR},
                "created_at": "2026-02-01T00:00:00Z",
            },
        ],
        "documents": [
            {
                "collection": "votes",
                "doc_id": "a",
                "data": {"n": 1},
                "updated_by_ref": {"discord_id": MEMBER},
                "created_at": "2026-01-02T00:00:00Z",
                "updated_at": "2026-01-03T00:00:00Z",
            }
        ],
    }
    return PageIn.model_validate({**body, **over})


@pytest.fixture
async def tenant(db_session: AsyncSession) -> TenantRow:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    return await make_tenant(db_session, platform="discord", workspace_id="900")


def _target(tenant: TenantRow, db: AsyncSession) -> ImportTarget:
    return ImportTarget(
        tenant_id=tenant.id,
        agent_id="ag_1",
        agent_name="analyst",
        limits=ArtifactPagesSettings(),
        crypto_keys=db.info["crypto_keys"],
        asset_hosts=(),
        content_host=False,
        known_slugs=frozenset({"q3-plan"}),
    )


def test_a_bundle_refuses_unknown_fields_and_unsafe_public_pages() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _page(surprise=1)
    with pytest.raises(ValidationError, match="cannot write data"):
        _page(data="write")
    bad_watch = {
        "legacy_id": "w",
        "instructions": "x",
        "owner": {"discord_id": OWNER},
        "channel_id": "1",
        "place_type": "channel",
        "delivery": "new_thread",
        "where": {"data.kind": "vote"},
    }
    with pytest.raises(ValidationError, match="where"):
        _page(watches=[bad_watch])
    with pytest.raises(ValidationError, match="google_sub goes with an email"):
        _page(people=[{"discord_id": MEMBER, "google_sub": "g"}])
    bundle = {
        "version": 2,
        "guild_id": "900",
        "exported_at": "2026-03-01T00:00:00Z",
        "page": _page().model_dump(mode="json"),
    }
    with pytest.raises(ValidationError):
        Bundle.model_validate(bundle)
    with pytest.raises(ValidationError) as caught:
        PageIn.model_validate({**_page().model_dump(mode="json"), "link_secret": 5})
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "over",
    [
        {"public_id": "abcdefgh23x"},
        {"slug": "Q3 plan!"},
        {"legacy_url": "see https://pages.example.org/a/q3-plan"},
        {"owner": {"discord_id": "10x01"}},
        {"people": [{"email": "a b@example.com"}]},
        {"link_secret": "too-short"},
        {"link_secret": "s3cret-link-value-0123456789&x=1"},
    ],
)
def test_a_bundle_field_must_match_its_pattern_whole(over: dict[str, Any]) -> None:
    """A partial match is a refusal of the file, never a value the database then refuses."""
    with pytest.raises(ValidationError) as caught:
        _page(**over)
    assert SECRET not in str(caught.value), "an error never repeats the secret"


def test_a_page_address_encodes_its_link() -> None:
    url = page_url("https://web.example.com", "abcdefgh23", "q3-plan", "a&b=c")
    assert url.endswith("?k=a%26b%3Dc"), url


def test_the_compat_scan_names_hosts_and_pages_never_keys() -> None:
    html = (
        '<img src="https://tracker.example.com/p.png?key=abc"><script>localStorage.x=1</script>'
        '<iframe src="https://video.example.net/v"></iframe>'
        '<a href="https://pages.example.org/a/other-page?key=zzz">x</a>'
    )
    found = compat_warnings(
        html, asset_hosts=(), content_host=False, legacy_url=LEGACY, known_slugs={"q3-plan"}
    )
    text = "\n".join(found)
    assert "tracker.example.com" in text and "video.example.net" in text, found
    assert "browser storage" in text and "other-page" in text, found
    assert "abc" not in text and "zzz" not in text, "no query values"
    clean = compat_warnings(
        '<a href="https://pages.example.org/a/q3-plan">me</a>',
        asset_hosts=(),
        content_host=True,
        legacy_url=LEGACY,
        known_slugs={"q3-plan"},
    )
    assert clean == []


async def test_an_import_keeps_the_id_and_link_and_resolves_people(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    db = db_session
    target = _target(tenant, db)
    page = _page(
        people=[
            {"discord_id": MEMBER},
            {"email": "Guest@Example.com", "google_sub": "sub-1"},
            {"email": "guest@example.com", "google_sub": "sub-2"},
        ],
        events=True,
        data="off",
        documents=[
            {
                "collection": "_events",
                "doc_id": "e1",
                "data": {"kind": "vote"},
                "created_at": "2026-01-02T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
            }
        ],
        watches=[
            {
                "legacy_id": "w1",
                "instructions": "Tally the votes.",
                "owner": {"discord_id": OWNER},
                "channel_id": "4241",
                "thread_id": "4242",
                "place_type": "thread",
                "delivery": "thread",
                "select": ".data.item",
                "where": {"event": ["vote", "undo"]},
                "max_turns_per_day": 30,
            },
            {
                "legacy_id": "w2",
                "instructions": "Odd filter.",
                "owner": {"discord_id": OWNER},
                "channel_id": "4241",
                "place_type": "channel",
                "delivery": "new_thread",
                "select": ".[] | select(.type == 1)",
            },
        ],
    )
    orphan = await get_or_create_platform_principal(
        db, tenant_id=tenant.id, platform="discord", external_id=OWNER
    )
    await watches.import_callback_watch(
        db,
        ImportedCallbackWatch(
            legacy_id="w1",
            source="callback",
            instructions="old",
            token_sha256=hashlib.sha256(b"t").hexdigest(),
            owner_discord_id=OWNER,
            channel_id="4241",
            place_type="channel",
        ),
        tenant_id=tenant.id,
        agent_id="ag_1",
        agent_name="analyst",
        now=NOW,
    )

    report = await import_page(db, page, target, now=NOW)

    assert (report.action, report.members, report.visitors, report.watches) == ("create", 1, 1, 2)
    assert report.orphans_ended == 1 and report.problems == (), report
    assert any("30 reports a day" in w for w in report.warnings), report.warnings
    assert any("differs from the one this guest" in w for w in report.warnings), report.warnings
    row = await artifact_pages.get_by_public_id(db, "abcdefgh23")
    assert row is not None and row.imported_from == LEGACY
    assert (row.slug, row.current_version, row.data_revision) == ("q3-plan", 2, 7)
    assert row.link_secret == SECRET, "the same link, stored encrypted"
    stored = await db.scalar(select(Artifact.link_secret).where(Artifact.id == row.id))
    assert stored is not None and SECRET.encode() not in stored
    assert len(row.editors) == 1 and len(row.people) == 2
    assert (await artifact_pages.find_imported(db, address=LEGACY)) == ("abcdefgh23", "q3-plan")

    versions = await artifact_pages.list_versions(db, row.id)
    assert [v.version for v in versions] == [2, 1]
    collections = set(
        await db.scalars(
            select(ArtifactDocument.collection).where(ArtifactDocument.artifact_id == row.id)
        )
    )
    assert collections == {"_events"}

    visitor = (
        await db.execute(
            select(WebIdentity.provider, WebIdentity.subject).where(
                WebIdentity.tenant_id == tenant.id
            )
        )
    ).all()
    assert [tuple(v) for v in visitor] == [("google", "sub-1")], "pinned, one visitor"
    listed = {
        w.source_state["legacy_id"]: w
        for w in await watches.page_watches(db, tenant_id=tenant.id, page_id=row.id)
    }
    watch, odd = listed["w1"], listed["w2"]
    assert (watch.source, watch.status, watch.delivery) == ("page_event", "active", "new_thread")
    assert watch.owner_account_id == orphan.account_id
    assert watch.source_state["where"] == {"event": ["vote", "undo"]}
    assert watch.rule == {"projection": ".data.item"}
    assert (odd.status, odd.rule) == ("paused", {}), "a jq select pauses it, unfiltered"
    assert any("w2" in w and "paused" in w for w in report.warnings), report.warnings
    left = await db.scalar(
        select(func.count()).select_from(Watch).where(Watch.source == "callback")
    )
    assert left == 0, "the orphan callback watch ended"


async def test_a_rerun_replaces_the_page_in_place(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    db = db_session
    target = _target(tenant, db)
    watch: dict[str, Any] = {
        "legacy_id": "w1",
        "instructions": "Tally.",
        "owner": {"discord_id": OWNER},
        "channel_id": "4241",
        "place_type": "channel",
        "delivery": "new_thread",
    }
    first = await import_page(db, _page(events=True, data="off", watches=[watch]), target, now=NOW)
    before = await artifact_pages.get_by_public_id(db, "abcdefgh23")
    assert before is not None and first.action == "create"

    second = _page(
        title="Q3 plan v2",
        events=True,
        data="off",
        watches=[watch],
        documents=[],
        versions=[
            {"version": 3, "html": "<p>three</p>", "created_at": "2026-03-01T00:00:00Z"},
        ],
        current_version=3,
    )
    report = await import_page(db, second, target, now=NOW)
    after = await artifact_pages.get_by_public_id(db, "abcdefgh23")

    assert report.action == "replace" and after is not None
    assert after.id == before.id and after.title == "Q3 plan v2"
    assert [v.version for v in await artifact_pages.list_versions(db, after.id)] == [3]
    assert (
        await db.scalar(
            select(func.count())
            .select_from(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == after.id)
        )
        == 1
    )
    assert len(await watches.page_watches(db, tenant_id=tenant.id, page_id=after.id)) == 1
    assert after.data_revision > before.data_revision


Change = Callable[[AsyncSession, ArtifactRow], Awaitable[object]]
_LIMITS = ArtifactPagesSettings()


async def _settings(db: AsyncSession, page: ArtifactRow) -> object:
    return await artifact_pages.update_settings(
        db, page, PageChange(title="Ours"), role="owner", account_id=page.owner_account_id, now=NOW
    )


async def _people(db: AsyncSession, page: ArtifactRow) -> object:
    return await artifact_pages.update_settings(
        db, page, PageChange(people=()), role="owner", account_id=page.owner_account_id, now=NOW
    )


async def _link(db: AsyncSession, page: ArtifactRow) -> object:
    return await artifact_pages.rotate_link(db, page, role="owner", now=NOW)


async def _transfer(db: AsyncSession, page: ArtifactRow) -> object:
    return await artifact_pages.transfer(
        db, page, to_account_id=page.editors[0], role="owner", now=NOW
    )


async def _version(db: AsyncSession, page: ArtifactRow) -> object:
    return await artifact_pages.restore(
        db, page, 1, role="owner", account_id=None, now=NOW, limits=_LIMITS
    )


async def _data(db: AsyncSession, page: ArtifactRow) -> object:
    return await artifact_pages.write_doc(
        db,
        page,
        "votes",
        "b",
        DocumentWrite(data={"n": 2}),
        by="agent",
        account_id=None,
        rule=None,
        now=NOW,
        limits=_LIMITS,
    )


@pytest.mark.parametrize("change", [_settings, _people, _link, _transfer, _version, _data])
async def test_a_rerun_refuses_a_page_changed_here_since_its_import(
    db_session: AsyncSession, tenant: TenantRow, change: Change
) -> None:
    """A replace would revert what was done here, so only an unchanged page is replaced."""
    db = db_session
    target = _target(tenant, db)
    await import_page(db, _page(people=[{"discord_id": MEMBER}]), target, now=NOW)
    again = await import_page(db, _page(people=[{"discord_id": MEMBER}]), target, now=NOW)
    assert again.action == "replace", "an unchanged page is replaced"

    page = await artifact_pages.get_by_public_id(db, "abcdefgh23")
    assert page is not None
    await change(db, page)
    report = await import_page(db, _page(title="Theirs"), target, now=NOW)

    assert report.action == "refused", report
    assert report.problems == ("changed here since import",), report.problems
    kept = await artifact_pages.get_by_public_id(db, "abcdefgh23")
    assert kept is not None and kept.title != "Theirs", "nothing reverted"


async def test_a_page_in_the_way_expired_or_oversized_is_refused_or_skipped(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    db = db_session
    target = _target(tenant, db)
    assert (await import_page(db, _page(), target, now=NOW)).action == "create"

    clash = _page(legacy_url="https://pages.example.org/a/other", public_id="zzzzzzzzzz")
    refused = await import_page(db, clash, target, now=NOW)
    assert refused.action == "refused" and "taken" in refused.problems[0], refused

    moved = _page(slug="renamed")
    assert (await import_page(db, moved, target, now=NOW)).action == "refused"

    big = _page(
        legacy_url="https://pages.example.org/a/big",
        public_id="bbbbbbbbbb",
        slug="big",
        versions=[
            {
                "version": 1,
                "html": "x" * (target.limits.html_bytes + 1),
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
        current_version=1,
    )
    over = await import_page(db, big, target, now=NOW)
    assert over.action == "refused" and "limit" in over.problems[0], over
    assert await artifact_pages.get_by_public_id(db, "bbbbbbbbbb") is None, "nothing written"

    old = _page(
        legacy_url="https://pages.example.org/a/old",
        public_id="cccccccccc",
        slug="old",
        access="workspace",
        expires_at=(NOW - timedelta(days=1)).isoformat(),
    )
    assert (await import_page(db, old, target, now=NOW)).action == "expired"


async def test_an_outsider_editor_refuses_the_page_as_the_store_rule_does(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    """With `audience_hold` on, a Discord member with no internal role counts as a visitor,
    and a visitor never edits a page; the import refuses rather than grants it."""
    db = db_session
    await hold_audience(db, tenant.id)
    for discord_id, internal in ((OWNER, True), (EDITOR, False)):
        account = await make_account(db, tenant=tenant)
        await link_discord_member(
            db, tenant_id=tenant.id, account_id=account.id, internal=internal, discord_id=discord_id
        )

    report = await import_page(db, _page(), _target(tenant, db), now=NOW)

    assert report.action == "refused", report
    assert any("1 of the owner and editors" in p for p in report.problems), report.problems
    assert await artifact_pages.get_by_public_id(db, "abcdefgh23") is None, "nothing written"


def _doc(collection: str, doc_id: str) -> dict[str, Any]:
    return {
        "collection": collection,
        "doc_id": doc_id,
        "data": {"n": 1},
        "created_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("docs", "problem"),
    [
        ([_doc("votes", "a"), _doc("votes", "a")], "votes/a appears twice"),
        ([_doc("_events", "e1"), _doc("_events", "e2")], "the events (2,"),
    ],
)
async def test_a_repeated_document_or_events_over_the_limit_refuse_the_page(
    db_session: AsyncSession, tenant: TenantRow, docs: list[dict[str, Any]], problem: str
) -> None:
    target = replace(_target(tenant, db_session), limits=ArtifactPagesSettings(events=1))
    page = _page(access="workspace", events=True, documents=docs)
    report = await import_page(db_session, page, target, now=NOW)
    assert report.action == "refused" and any(problem in p for p in report.problems), report


async def test_a_page_watch_limit_counts_the_watches_the_page_keeps(
    db_session: AsyncSession, tenant: TenantRow
) -> None:
    target = _target(tenant, db_session)

    def watch(legacy_id: str) -> dict[str, Any]:
        return {
            "legacy_id": legacy_id,
            "instructions": "Tally.",
            "owner": {"discord_id": OWNER},
            "channel_id": "4241",
            "place_type": "channel",
            "delivery": "new_thread",
        }

    kept = [watch(f"w{i}") for i in range(MAX_WATCHES_PER_PAGE)]
    first = _page(events=True, data="off", documents=[], watches=kept)
    assert (await import_page(db_session, first, target, now=NOW)).watches == MAX_WATCHES_PER_PAGE
    again = _page(events=True, data="off", documents=[], watches=[watch("extra")])
    report = await import_page(db_session, again, target, now=NOW)
    assert report.action == "replace" and report.watches == 0, report
    assert any("at most" in w for w in report.warnings), report.warnings

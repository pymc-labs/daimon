"""Importing pages exported by another system: the bundle, the compat scan and the store."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import Artifact, ArtifactDocument, ArtifactVersion, Watch, WebIdentity
from daimon.core.artifact_pages.importing import Bundle, PageIn, compat_warnings
from daimon.core.config import ArtifactPagesSettings
from daimon.core.continuity.watch_imports import ImportedCallbackWatch
from daimon.core.stores import artifact_pages, watches
from daimon.core.stores.artifact_import import ImportTarget, import_page
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.factories import make_tenant
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
LEGACY = "https://pages.example.org/a/q3-plan"
SECRET = "s3cret-link-value"
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
            {"email": "guest@example.com"},
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
                "max_turns_per_day": 30,
            }
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

    assert (report.action, report.members, report.visitors, report.watches) == ("create", 1, 1, 1)
    assert report.orphans_ended == 1 and report.problems == (), report
    assert any("30 reports a day" in w for w in report.warnings), report.warnings
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
    watch = (await watches.page_watches(db, tenant_id=tenant.id, page_id=row.id))[0]
    assert (watch.source, watch.status, watch.delivery) == ("page_event", "active", "new_thread")
    assert watch.owner_account_id == orphan.account_id
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

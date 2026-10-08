from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio
from anthropic.types.beta import BetaSkill as SkillListResponse
from anthropic.types.beta.skills import BetaSkillVersion as VersionCreateResponse
from daimon.core.defaults.reconcile_skills import reconcile_skill
from daimon.core.defaults.report import Action
from daimon.core.stores.seeded_skills import load_seeded_skill
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    """A real tenants row — `seeded_skills.tenant_id` is a FK onto it."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    return tenant.id


def _write_skill(tmp_path: Path, name: str = "brainstorming", body: str = "body\n") -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}")
    return skill_dir


def _existing_skill_row(id_: str, name: str, version: str = "1") -> dict[str, Any]:
    return SkillListResponse(
        id=id_,
        type="custom",
        display_title=name,
        latest_version=version,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")


def _router_with_skills(skills: list[dict[str, Any]]) -> MARouter:
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(skills))
    return router


async def test_reconcile_skill_creates_when_not_on_ma(
    tmp_path: Path,
    tenant_id: uuid.UUID,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No MA match → CREATE path; skills.create called with display_title and zip."""
    skill_dir = _write_skill(tmp_path)
    router = _router_with_skills([])
    create_called = False

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        nonlocal create_called
        create_called = True
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_new",
                type="custom",
                display_title="brainstorming",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    router.add("POST", r"/v1/skills", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=False
    )
    assert outcome.action is Action.CREATED
    assert outcome.anthropic_id == "sk_new"
    assert create_called, "skills.create must have been called"

    recorded = await load_seeded_skill(db_session, tenant_id=tenant_id, name="brainstorming")
    assert recorded is not None, "a create must leave a fingerprint, or the next boot re-uploads"
    assert recorded.anthropic_id == "sk_new"


async def test_reconcile_skill_skips_when_content_matches_the_fingerprint(
    tmp_path: Path,
    tenant_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unchanged content → SKIPPED, and no version is pushed.

    The steady state. The boot sweep runs this for every tenant on every deploy,
    so an unchanged skill must cost reads only.
    """
    skill_dir = _write_skill(tmp_path)
    ma_row = _existing_skill_row("sk_1", f"{str(tenant_id)[:8]}-brainstorming")
    router = _router_with_skills([ma_row])
    router.add(
        "POST",
        r"/v1/skills/[^/]+/versions",
        lambda req, _m: httpx.Response(
            200,
            json=VersionCreateResponse(
                id="skv_1",
                created_at="2026-04-21T00:00:00Z",
                description="d",
                directory="brainstorming",
                name="brainstorming",
                skill_id="sk_1",
                type="skill_version",
                version="1",
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    # First pass records the fingerprint (no local row yet → one upload).
    first = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=False
    )
    assert first.action is Action.UPDATED, (
        "an install with no fingerprint holds unknown content and must be refreshed once"
    )

    # Second pass, same bytes.
    second = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=False
    )
    assert second.action is Action.SKIPPED, "identical content must not push another version"
    assert second.anthropic_id == "sk_1"


async def test_reconcile_skill_pushes_a_new_version_when_the_content_changed(
    tmp_path: Path,
    tenant_id: uuid.UUID,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The defect this exists for: an edit to a seeded SKILL.md must reach a live install.

    A new VERSION of the same skill, not a delete-and-recreate: agents pin
    `version="latest"`, and deleting a referenced skill 400s every one of that
    agent's turns.
    """
    skill_dir = _write_skill(tmp_path, body="original guidance\n")
    ma_row = _existing_skill_row("sk_1", f"{str(tenant_id)[:8]}-brainstorming")
    router = _router_with_skills([ma_row])
    version_posts: list[str] = []

    def on_version_create(req: httpx.Request, _m: object) -> httpx.Response:
        version_posts.append(req.url.path)
        return httpx.Response(
            200,
            json=VersionCreateResponse(
                id="skv_2",
                created_at="2026-04-22T00:00:00Z",
                description="d",
                directory="brainstorming",
                name="brainstorming",
                skill_id="sk_1",
                type="skill_version",
                version="2",
            ).model_dump(mode="json"),
        )

    router.add("POST", r"/v1/skills/[^/]+/versions", on_version_create)
    router.add(
        "DELETE",
        r"/v1/skills/[^/]+",
        lambda req, _m: httpx.Response(500, json={"never": "delete a referenced skill"}),
    )
    client = build_fake_anthropic_http(router.dispatch)

    await reconcile_skill(client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=False)
    async with db_session_factory() as read:
        before = await load_seeded_skill(read, tenant_id=tenant_id, name="brainstorming")
    assert before is not None

    _write_skill(tmp_path, body="corrected guidance\n")
    outcome = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=False
    )

    assert outcome.action is Action.UPDATED, "an edited SKILL.md must be delivered, not skipped"
    assert outcome.anthropic_id == "sk_1", "the skill id must not change — agents pin it"
    assert len(version_posts) == 2, f"one version push per content change, got {version_posts}"
    async with db_session_factory() as read:
        after = await load_seeded_skill(read, tenant_id=tenant_id, name="brainstorming")
    assert after is not None
    assert after.content_hash != before.content_hash, "the fingerprint must track the new content"


async def test_reconcile_skill_deletes_duplicates_keeping_newest(
    tmp_path: Path,
    tenant_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Multiple skills with same display_title → keep newest, delete older(s).

    Mirrors the agent/env dedup pattern. cli-auth duplicated in production with
    two skills 54ms apart (smoke probe finding); reconcile must clean these up
    on next apply.
    """
    skill_dir = _write_skill(tmp_path)
    older = SkillListResponse(
        id="sk_old",
        type="custom",
        display_title=f"{str(tenant_id)[:8]}-brainstorming",
        latest_version="1",
        created_at="2026-04-20T00:00:00Z",
        updated_at="2026-04-20T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")
    newer = SkillListResponse(
        id="sk_new",
        type="custom",
        display_title=f"{str(tenant_id)[:8]}-brainstorming",
        latest_version="1",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")
    router = _router_with_skills([older, newer])

    deleted_skill_ids: list[str] = []

    def on_skill_delete(req: httpx.Request, m: object) -> httpx.Response:
        sk_id = req.url.path.rstrip("/").rsplit("/", 1)[-1]
        deleted_skill_ids.append(sk_id)
        return httpx.Response(200, json={"id": sk_id, "deleted": True})

    # delete_skill_and_versions does: list versions, delete each, then delete skill.
    router.add("GET", r"/v1/skills/[^/]+/versions", lambda req, _m: list_response([]))
    router.add("DELETE", r"/v1/skills/[^/]+", on_skill_delete)
    router.add(
        "POST",
        r"/v1/skills/[^/]+/versions",
        lambda req, _m: httpx.Response(
            200,
            json=VersionCreateResponse(
                id="skv_1",
                created_at="2026-04-21T00:00:00Z",
                description="d",
                directory="brainstorming",
                name="brainstorming",
                skill_id="sk_new",
                type="skill_version",
                version="2",
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=False
    )
    assert outcome.anthropic_id == "sk_new", "newest must be adopted as canonical"
    assert deleted_skill_ids == ["sk_old"], (
        f"only the older duplicate must be deleted, got {deleted_skill_ids}"
    )


async def test_reconcile_skill_dry_run_create(
    tmp_path: Path,
    tenant_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """dry_run=True with no MA match → CREATED action, no write calls, no anthropic_id."""
    skill_dir = _write_skill(tmp_path)
    # No POST handler — router raises if reconcile tries to write.
    router = _router_with_skills([])
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=True
    )
    assert outcome.action is Action.CREATED
    assert outcome.anthropic_id is None


async def test_reconcile_skill_dry_run_reports_a_content_change_without_writing(
    tmp_path: Path,
    tenant_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`defaults verify` must call an edited skill diverged, and must stay read-only.

    No POST handler is registered, so any write attempt trips the router.
    """
    skill_dir = _write_skill(tmp_path)
    router = _router_with_skills(
        [_existing_skill_row("sk_1", f"{str(tenant_id)[:8]}-brainstorming")]
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=True
    )
    assert outcome.action is Action.UPDATED, "unfingerprinted content on MA is not verified in-sync"
    assert outcome.anthropic_id is None


async def test_reconcile_skill_looks_up_prefixed_display_title(
    tmp_path: Path,
    tenant_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """reconcile_skill uses the prefixed title for the MA lookup, not the bare spec name.

    The MA skills list returns one skill with the PREFIXED display_title. If
    reconcile looked up the bare "brainstorming" it would miss and call
    skills.create — which the absent POST /v1/skills handler turns into a
    router AssertionError.
    """
    skill_dir = _write_skill(tmp_path)
    prefixed = f"{str(tenant_id)[:8]}-brainstorming"
    router = _router_with_skills([_existing_skill_row("sk_1", prefixed)])
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=True
    )
    assert outcome.action is Action.UPDATED, (
        f"reconcile must match the prefixed display_title '{prefixed}' on MA; "
        "CREATED means it queried the unprefixed 'brainstorming' and missed"
    )


async def test_reconcile_skill_creates_with_same_prefixed_title(
    tmp_path: Path,
    tenant_id: uuid.UUID,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """skills.create is called with the same prefixed title used for the lookup.

    No MA match exists. The POST /v1/skills handler captures the display_title
    from the multipart body and asserts it equals the prefixed form — confirming
    that lookup and create agree (ISO-04: no spurious duplicate can arise from
    a title mismatch between find and create).
    """
    skill_dir = _write_skill(tmp_path)
    prefixed = f"{str(tenant_id)[:8]}-brainstorming"
    router = _router_with_skills([])

    captured_display_title: list[str] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        # The SDK sends display_title as a multipart field; search the raw body bytes.
        body_text = req.content.decode("latin-1")
        for segment in body_text.split("\r\n"):
            if segment.strip() == prefixed:
                captured_display_title.append(prefixed)
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_created",
                type="custom",
                display_title=prefixed,
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    router.add("POST", r"/v1/skills", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(
        client, db_session_factory, skill_dir, tenant_id=tenant_id, dry_run=False
    )
    assert outcome.action is Action.CREATED, "no MA match → CREATE path"
    assert outcome.anthropic_id == "sk_created"
    assert captured_display_title == [prefixed], (
        f"skills.create must be called with the prefixed display_title '{prefixed}'; "
        f"got {captured_display_title}"
    )

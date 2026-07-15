from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
from anthropic.types.beta import SkillListResponse
from daimon.core.defaults.reconcile_skills import reconcile_skill
from daimon.core.defaults.report import Action
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http

TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _write_skill(tmp_path: Path, name: str = "brainstorming", body: str = "body\n") -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir()
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


async def test_reconcile_skill_creates_when_not_on_ma(tmp_path: Path) -> None:
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

    outcome = await reconcile_skill(client, skill_dir, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.CREATED
    assert outcome.anthropic_id == "sk_new"
    assert create_called, "skills.create must have been called"


async def test_reconcile_skill_skips_when_on_ma(tmp_path: Path) -> None:
    """MA match found → SKIPPED; defaults apply never pushes a new version.

    Per L13 fix: skills are immutable from defaults apply's perspective. Content
    updates flow through `daimon skills sync` explicitly. There is no
    content-addressed carrier on MA to drive idempotency from, and constant
    re-upload churn (33 versions/day in production) is worse than stale content.
    """
    skill_dir = _write_skill(tmp_path)
    router = _router_with_skills(
        [_existing_skill_row("sk_1", f"{str(TENANT_ID)[:8]}-brainstorming")]
    )
    # No POST handler registered for versions — router raises if reconcile
    # tries to push a new version.
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(client, skill_dir, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.SKIPPED, (
        "reconcile must not push a new version when MA match exists"
    )
    assert outcome.anthropic_id == "sk_1"


async def test_reconcile_skill_deletes_duplicates_keeping_newest(tmp_path: Path) -> None:
    """Multiple skills with same display_title → keep newest, delete older(s).

    Mirrors the agent/env dedup pattern. cli-auth duplicated in production with
    two skills 54ms apart (smoke probe finding); reconcile must clean these up
    on next apply.
    """
    skill_dir = _write_skill(tmp_path)
    # Two skills, same display_title. Newest = sk_new (later created_at).
    older = SkillListResponse(
        id="sk_old",
        type="custom",
        display_title=f"{str(TENANT_ID)[:8]}-brainstorming",
        latest_version="1",
        created_at="2026-04-20T00:00:00Z",
        updated_at="2026-04-20T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")
    newer = SkillListResponse(
        id="sk_new",
        type="custom",
        display_title=f"{str(TENANT_ID)[:8]}-brainstorming",
        latest_version="1",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")
    router = _router_with_skills([older, newer])

    deleted_skill_ids: list[str] = []

    # delete_skill_and_versions does: list versions, delete each, then delete skill.
    def on_versions_list(req: httpx.Request, _m: object) -> httpx.Response:
        return list_response([])

    def on_skill_delete(req: httpx.Request, m: object) -> httpx.Response:
        # extract the {skill_id} from the URL — path is /v1/skills/{id}
        sk_id = req.url.path.rstrip("/").rsplit("/", 1)[-1]
        deleted_skill_ids.append(sk_id)
        return httpx.Response(200, json={"id": sk_id, "deleted": True})

    router.add("GET", r"/v1/skills/[^/]+/versions", on_versions_list)
    router.add("DELETE", r"/v1/skills/[^/]+", on_skill_delete)
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(client, skill_dir, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.SKIPPED, "canonical match still skips version push"
    assert outcome.anthropic_id == "sk_new", "newest must be adopted as canonical"
    assert deleted_skill_ids == ["sk_old"], (
        f"only the older duplicate must be deleted, got {deleted_skill_ids}"
    )


async def test_reconcile_skill_dry_run_create(tmp_path: Path) -> None:
    """dry_run=True with no MA match → CREATED action, no write calls, no anthropic_id."""
    skill_dir = _write_skill(tmp_path)
    # No POST handler — router raises if reconcile tries to write.
    router = _router_with_skills([])
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(client, skill_dir, tenant_id=TENANT_ID, dry_run=True)
    assert outcome.action is Action.CREATED
    assert outcome.anthropic_id is None


async def test_reconcile_skill_dry_run_skip(tmp_path: Path) -> None:
    """dry_run=True with MA match → SKIPPED action (no version push planned), no anthropic_id."""
    skill_dir = _write_skill(tmp_path)
    # No POST handler for versions — router raises if reconcile tries to write.
    router = _router_with_skills(
        [_existing_skill_row("sk_1", f"{str(TENANT_ID)[:8]}-brainstorming")]
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(client, skill_dir, tenant_id=TENANT_ID, dry_run=True)
    assert outcome.action is Action.SKIPPED
    assert outcome.anthropic_id is None


async def test_reconcile_skill_looks_up_prefixed_display_title(tmp_path: Path) -> None:
    """reconcile_skill uses the prefixed title for the MA lookup, not the bare spec name.

    The MA skills list returns one skill with the PREFIXED display_title
    (str(TENANT_ID)[:8] + "-brainstorming"). If reconcile looked up the bare
    "brainstorming" it would not match and would call skills.create instead.
    The test registers no POST /v1/skills handler so any create attempt trips the
    router's AssertionError — confirming the lookup used the prefixed form.
    """
    skill_dir = _write_skill(tmp_path)
    prefixed = f"{str(TENANT_ID)[:8]}-brainstorming"
    # Only the prefixed title is on MA. If reconcile queries by bare name it misses
    # and tries to CREATE, tripping the no-POST-handler assertion.
    router = _router_with_skills([_existing_skill_row("sk_1", prefixed)])
    client = build_fake_anthropic_http(router.dispatch)

    outcome = await reconcile_skill(client, skill_dir, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.SKIPPED, (
        f"reconcile must match the prefixed display_title '{prefixed}' on MA; "
        "CREATED means it queried the unprefixed 'brainstorming' and missed"
    )
    assert outcome.anthropic_id == "sk_1", "adopted skill id must be sk_1 (the prefixed-title row)"


async def test_reconcile_skill_creates_with_same_prefixed_title(tmp_path: Path) -> None:
    """skills.create is called with the same prefixed title used for the lookup.

    No MA match exists. The POST /v1/skills handler captures the display_title
    from the multipart body and asserts it equals the prefixed form — confirming
    that lookup and create agree (ISO-04: no spurious duplicate can arise from
    a title mismatch between find and create).
    """
    skill_dir = _write_skill(tmp_path)
    prefixed = f"{str(TENANT_ID)[:8]}-brainstorming"
    router = _router_with_skills([])

    captured_display_title: list[str] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        # The SDK sends display_title as a multipart field; search the raw body bytes.
        body_bytes = req.content
        for part in body_bytes.split(b"\r\n"):
            if part.startswith(b"{"):
                # JSON part — not the display_title field
                continue
        # Decode and find the display_title value in the multipart payload
        body_text = body_bytes.decode("latin-1")
        for segment in body_text.split("\r\n"):
            segment = segment.strip()
            # Candidate value segment that matches our prefixed display_title
            if (
                segment
                and not segment.startswith("--")
                and not segment.startswith("Content-")
                and segment == prefixed
            ):
                captured_display_title.append(segment)
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

    outcome = await reconcile_skill(client, skill_dir, tenant_id=TENANT_ID, dry_run=False)
    assert outcome.action is Action.CREATED, "no MA match → CREATE path"
    assert outcome.anthropic_id == "sk_created"
    assert captured_display_title == [prefixed], (
        f"skills.create must be called with the prefixed display_title '{prefixed}'; "
        f"got {captured_display_title}"
    )

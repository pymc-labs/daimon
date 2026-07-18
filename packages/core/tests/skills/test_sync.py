"""Transport-level tests for sync_skills.

Uses MARouter + build_fake_anthropic_http (httpx.MockTransport backed) so the
real SDK code path runs in full. Response objects are constructed via the real
SDK constructors (validated) and serialised with .model_dump(mode="json").
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import httpx
from anthropic.types.beta import SkillListResponse
from anthropic.types.beta.skills import VersionCreateResponse
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.report import Action
from daimon.core.skills.discover import DiscoveredSkill
from daimon.core.skills.sync import sync_skills
from daimon.core.specs import SkillSpec
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http

_TENANT_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_TENANT_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


def _skill(tmp_path: Path, name: str = "brainstorming") -> DiscoveredSkill:
    """Build a minimal DiscoveredSkill backed by a real skill directory."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody")
    spec = SkillSpec(name=name, description="d")
    return DiscoveredSkill(spec=spec, skill_dir=skill_dir, body="body")


def _skill_row(id_: str, name: str) -> dict[str, Any]:
    return SkillListResponse(
        id=id_,
        type="custom",
        display_title=name,
        latest_version="1",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")


def _parse_display_title_from_multipart(req: httpx.Request) -> str:
    """Extract the display_title field value from a multipart/form-data request."""
    content_type = req.headers.get("content-type", "")
    boundary_match = re.search(r"boundary=([^\s;]+)", content_type)
    if not boundary_match:
        return ""
    boundary = boundary_match.group(1)
    body_str = req.content.decode("latin-1")
    for part in body_str.split(f"--{boundary}"):
        if 'name="display_title"' in part:
            value_start = part.find("\r\n\r\n")
            if value_start != -1:
                return part[value_start + 4 :].strip().strip("\r\n")
    return ""


async def test_sync_creates_new_skill(tmp_path: Path) -> None:
    """No MA match → skills.create called with canonical title; outcome is CREATED."""
    skill = _skill(tmp_path)
    canonical = tenant_scoped_display_title(tenant_id=_TENANT_A, name="brainstorming")
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    create_called = False

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        nonlocal create_called
        create_called = True
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_new",
                type="custom",
                display_title=canonical,
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    router.add("POST", r"/v1/skills", on_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sync_skills(client, [skill], tenant_id=_TENANT_A)

    assert len(outcomes) == 1, "should return one outcome"
    assert outcomes[0].action is Action.CREATED, "action should be CREATED"
    assert outcomes[0].anthropic_id == "sk_new", "should capture anthropic_id"
    assert create_called, "skills.create must have been called"


async def test_sync_updates_existing_skill(tmp_path: Path) -> None:
    """MA match found by canonical title → skills.versions.create called; outcome is UPDATED."""
    skill = _skill(tmp_path)
    canonical = tenant_scoped_display_title(tenant_id=_TENANT_A, name="brainstorming")
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([_skill_row("sk_1", canonical)]))
    version_called = False

    def on_version_create(req: httpx.Request, _m: object) -> httpx.Response:
        nonlocal version_called
        version_called = True
        return httpx.Response(
            200,
            json=VersionCreateResponse(
                id="ver_new",
                skill_id="sk_1",
                version="2",
                type="skill_version",
                name="SKILL.zip",
                directory="/",
                description="",
                created_at="2026-04-21T00:00:00Z",
            ).model_dump(mode="json"),
        )

    router.add("POST", r"/v1/skills/sk_1/versions", on_version_create)
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sync_skills(client, [skill], tenant_id=_TENANT_A)

    assert len(outcomes) == 1, "should return one outcome"
    assert outcomes[0].action is Action.UPDATED, "action should be UPDATED"
    assert outcomes[0].anthropic_id == "sk_1", "should use existing MA skill id"
    assert version_called, "skills.versions.create must have been called"


async def test_sync_records_failed_outcome_on_error(tmp_path: Path) -> None:
    """500 from MA skills.create → FAILED outcome (not an exception)."""
    skill = _skill(tmp_path)
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router.add("POST", r"/v1/skills", lambda req, _m: httpx.Response(500, json={"error": "fail"}))
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sync_skills(client, [skill], tenant_id=_TENANT_A)

    assert len(outcomes) == 1, "should return one outcome"
    assert outcomes[0].action is Action.FAILED, "500 should produce FAILED outcome"
    assert outcomes[0].error is not None, "FAILED outcome should carry error text"


async def test_sync_continues_after_failure(tmp_path: Path) -> None:
    """First skill fails (500, no retries) — second succeeds; batch is not aborted.

    ``max_retries=0`` avoids SDK retry complications with the call-order counter.
    """
    from anthropic import AsyncAnthropic

    skill_a = _skill(tmp_path / "a", name="alpha")
    skill_b = _skill(tmp_path / "b", name="beta")
    canonical_b = tenant_scoped_display_title(tenant_id=_TENANT_A, name="beta")

    # Track POST call order so we can return 500 for alpha and 200 for beta.
    post_calls: list[str] = []

    def on_get(req: httpx.Request, _m: object) -> httpx.Response:
        return list_response([])

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        # Call order: alpha POST then beta POST (sequential processing)
        idx = len(post_calls)
        post_calls.append(str(idx))
        if idx == 0:
            return httpx.Response(500, json={"error": "internal error"})
        return httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_beta",
                type="custom",
                display_title=canonical_b,
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("GET", r"/v1/skills", on_get)
    router.add("POST", r"/v1/skills", on_create)

    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client, max_retries=0)

    outcomes = await sync_skills(client, [skill_a, skill_b], tenant_id=_TENANT_A)

    assert len(outcomes) == 2, "should return two outcomes"
    assert outcomes[0].action is Action.FAILED, "alpha should be FAILED"
    assert outcomes[1].action is Action.CREATED, "beta should be CREATED"
    assert outcomes[1].anthropic_id == "sk_beta", "beta anthropic_id should be captured"


async def test_sync_empty_list_returns_empty(tmp_path: Path) -> None:
    """Empty input list returns empty outcomes list without calling MA."""
    router = MARouter()
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sync_skills(client, [], tenant_id=_TENANT_A)

    assert outcomes == [], "empty skill list should produce empty outcomes"


async def test_sync_creates_distinct_skills_when_two_tenants_sync_same_named_skill(
    tmp_path: Path,
) -> None:
    """Two tenants syncing the same skill name → two distinct prefixed MA skills.

    Tenant A and Tenant B both sync a skill named "brainstorming". Each sees an
    empty MA view. After both syncs, two distinct canonical titles were passed to
    skills.create — one per tenant prefix. The second sync does NOT create a
    version on the first tenant's skill.
    """
    from anthropic import AsyncAnthropic

    canonical_a = tenant_scoped_display_title(tenant_id=_TENANT_A, name="brainstorming")
    canonical_b = tenant_scoped_display_title(tenant_id=_TENANT_B, name="brainstorming")
    assert canonical_a != canonical_b, "distinct tenants must produce distinct canonical titles"

    # Capture the display_title passed to each skills.create call.
    create_request_titles: list[str] = []

    def on_create(req: httpx.Request, _m: object) -> httpx.Response:
        title = _parse_display_title_from_multipart(req)
        create_request_titles.append(title)
        idx = len(create_request_titles) - 1
        return httpx.Response(
            200,
            json=SkillListResponse(
                id=f"sk_{idx}",
                type="custom",
                display_title=title,
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    # Tenant A syncs against empty MA state.
    router_a = MARouter()
    router_a.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router_a.add("POST", r"/v1/skills", on_create)

    transport_a = httpx.MockTransport(router_a.dispatch)
    http_a = httpx.AsyncClient(transport=transport_a, base_url="https://api.anthropic.com")
    client_a = AsyncAnthropic(api_key="test", http_client=http_a, max_retries=0)

    skill_a = _skill(tmp_path / "a", name="brainstorming")
    outcomes_a = await sync_skills(client_a, [skill_a], tenant_id=_TENANT_A)

    assert len(outcomes_a) == 1, "tenant A sync should produce one outcome"
    assert outcomes_a[0].action is Action.CREATED, "tenant A skill should be CREATED"

    # Tenant B syncs against its own empty MA state (isolated view).
    router_b = MARouter()
    router_b.add("GET", r"/v1/skills", lambda req, _m: list_response([]))
    router_b.add("POST", r"/v1/skills", on_create)

    transport_b = httpx.MockTransport(router_b.dispatch)
    http_b = httpx.AsyncClient(transport=transport_b, base_url="https://api.anthropic.com")
    client_b = AsyncAnthropic(api_key="test", http_client=http_b, max_retries=0)

    skill_b = _skill(tmp_path / "b", name="brainstorming")
    outcomes_b = await sync_skills(client_b, [skill_b], tenant_id=_TENANT_B)

    assert len(outcomes_b) == 1, "tenant B sync should produce one outcome"
    assert outcomes_b[0].action is Action.CREATED, "tenant B skill should be CREATED"

    # Both creates must have used distinct canonical titles.
    assert len(create_request_titles) == 2, "exactly two skills.create calls should have been made"
    title_a, title_b = create_request_titles[0], create_request_titles[1]
    assert title_a != title_b, (
        f"two tenants must produce distinct display_titles: {title_a!r} vs {title_b!r}"
    )
    assert title_a == canonical_a, f"tenant A title should be {canonical_a!r}, got {title_a!r}"
    assert title_b == canonical_b, f"tenant B title should be {canonical_b!r}, got {title_b!r}"


async def test_sync_records_failed_outcome_when_list_is_truncated(tmp_path: Path) -> None:
    """Full 100-row page on lookup → SkillsListTruncatedError → FAILED outcome for that skill.

    A full-page response is treated as a truncated view. In a create
    context (on_truncation="raise"), this must NOT silently create a duplicate
    or miss the existing skill — it surfaces as a FAILED outcome.
    """
    from anthropic import AsyncAnthropic
    from daimon.core.defaults.ma_index import (
        _SKILLS_PAGE_LIMIT,  # pyright: ignore[reportPrivateUsage]
    )

    skill = _skill(tmp_path)

    # Return exactly _SKILLS_PAGE_LIMIT rows — triggers truncation detection.
    full_page_rows: list[dict[str, Any]] = [
        _skill_row(f"sk_{i}", f"other-skill-{i}") for i in range(_SKILLS_PAGE_LIMIT)
    ]

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(full_page_rows))

    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    client = AsyncAnthropic(api_key="test", http_client=http_client, max_retries=0)

    outcomes = await sync_skills(client, [skill], tenant_id=_TENANT_A)

    assert len(outcomes) == 1, "should return one outcome"
    assert outcomes[0].action is Action.FAILED, (
        "truncated list on lookup should produce FAILED outcome"
    )
    assert outcomes[0].error is not None, "FAILED outcome should carry error text"

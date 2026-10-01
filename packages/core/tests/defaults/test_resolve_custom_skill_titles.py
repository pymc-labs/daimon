"""Opaque MA skill ids become bare, own-namespace display names — or stay ids."""

from __future__ import annotations

import uuid

import httpx
from anthropic.types.beta import SkillListResponse
from daimon.core.defaults.skills import resolve_custom_skill_titles
from daimon.testing import ma_agent
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response


def _skill(*, id: str, display_title: str) -> dict[str, object]:
    return SkillListResponse(
        id=id,
        display_title=display_title,
        source="custom",
        type="custom",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        latest_version="1",
    ).model_dump(mode="json")


async def test_resolve_custom_skill_titles_strips_the_tenant_prefix_from_own_skills() -> None:
    tenant_id = uuid.uuid4()
    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [_skill(id="skill_auth", display_title=f"{str(tenant_id)[:8]}-cli-auth")]
        ),
    )
    agent = ma_agent(skills=[{"type": "custom", "skill_id": "skill_auth", "version": "1"}])

    titles, truncated = await resolve_custom_skill_titles(
        build_fake_anthropic(router.dispatch), agents=[agent], tenant_id=tenant_id
    )

    assert titles == {"skill_auth": "cli-auth"}, "own-namespace titles must resolve to bare names"
    assert truncated is False, "a short skills page is not truncated"


async def test_resolve_custom_skill_titles_omits_another_tenants_skill() -> None:
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [_skill(id="skill_theirs", display_title=f"{str(other_tenant_id)[:8]}-secret")]
        ),
    )
    agent = ma_agent(skills=[{"type": "custom", "skill_id": "skill_theirs", "version": "1"}])

    titles, _truncated = await resolve_custom_skill_titles(
        build_fake_anthropic(router.dispatch), agents=[agent], tenant_id=tenant_id
    )

    assert titles == {}, "a foreign tenant's title must never reach a rendered skill name"


async def test_resolve_custom_skill_titles_makes_no_call_without_a_custom_skill() -> None:
    calls: list[str] = []

    def on_list(request: httpx.Request, _m: object) -> httpx.Response:
        calls.append(request.url.path)
        return list_response([])

    router = MARouter()
    router.add("GET", r"/v1/skills", on_list)
    agent = ma_agent(skills=[{"type": "anthropic", "skill_id": "pptx", "version": "latest"}])

    titles, truncated = await resolve_custom_skill_titles(
        build_fake_anthropic(router.dispatch), agents=[agent], tenant_id=uuid.uuid4()
    )

    assert (titles, truncated) == ({}, False), (
        "an agent with no custom skill resolves to an empty map and an untruncated listing"
    )
    assert calls == [], "the skills listing must be skipped entirely when nothing needs it"


async def test_resolve_custom_skill_titles_covers_every_agent_in_one_listing() -> None:
    tenant_id = uuid.uuid4()
    calls: list[str] = []

    def on_list(request: httpx.Request, _m: object) -> httpx.Response:
        calls.append(request.url.path)
        return list_response(
            [
                _skill(id="skill_a", display_title=f"{str(tenant_id)[:8]}-alpha"),
                _skill(id="skill_b", display_title=f"{str(tenant_id)[:8]}-beta"),
            ]
        )

    router = MARouter()
    router.add("GET", r"/v1/skills", on_list)
    agents = [
        ma_agent(id="ag_1", skills=[{"type": "custom", "skill_id": "skill_a", "version": "1"}]),
        ma_agent(id="ag_2", skills=[{"type": "custom", "skill_id": "skill_b", "version": "1"}]),
    ]

    titles, _truncated = await resolve_custom_skill_titles(
        build_fake_anthropic(router.dispatch), agents=agents, tenant_id=tenant_id
    )

    assert titles == {"skill_a": "alpha", "skill_b": "beta"}, (
        "one listing must cover every agent's skills"
    )
    assert len(calls) == 1, "a roster of agents must cost one skills listing, not one per agent"

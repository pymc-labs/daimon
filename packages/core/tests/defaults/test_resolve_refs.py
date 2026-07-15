from __future__ import annotations

import uuid

import pytest
from anthropic.types.beta import SkillListResponse
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.skills import resolve_refs
from daimon.core.errors import DefaultsError
from daimon.core.specs import SkillRef
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http

_CREATED_AT = "2026-04-21T00:00:00Z"
_TENANT_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_TENANT_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


def _skill_item(*, id_: str, display_title: str) -> dict[str, object]:
    return SkillListResponse(
        id=id_,
        type="custom",
        display_title=display_title,
        latest_version="1",
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
        source="custom",
    ).model_dump(mode="json")


def _make_client(skills: list[dict[str, object]]) -> object:
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(skills))
    return build_fake_anthropic_http(router.dispatch)


async def test_resolve_refs_returns_empty_list_when_refs_is_empty() -> None:
    client = _make_client([])
    result = await resolve_refs(client, refs=[], tenant_id=_TENANT_A)
    assert result == [], "empty input must produce empty output, not None or error"


async def test_resolve_refs_maps_custom_ref_to_ma_skill_id() -> None:
    canonical = tenant_scoped_display_title(tenant_id=_TENANT_A, name="brainstorming")
    client = _make_client([_skill_item(id_="skill_01abc", display_title=canonical)])
    result = await resolve_refs(
        client,
        refs=[SkillRef(type="custom", skill_id="brainstorming")],
        tenant_id=_TENANT_A,
    )
    assert result == [{"type": "custom", "skill_id": "skill_01abc"}], (
        "resolver must emit SDK BetaManagedAgentsCustomSkillParams shape with "
        "skill_id (MA skill id) from the MA list response"
    )
    assert "version" not in result[0], (
        "custom skill params must not include a version key; MA resolves latest"
    )


async def test_resolve_refs_anthropic_skill() -> None:
    """Anthropic built-in refs pass through without any MA lookup."""
    client = _make_client([])
    result = await resolve_refs(
        client,
        refs=[SkillRef(type="anthropic", skill_id="xlsx")],
        tenant_id=_TENANT_A,
    )
    assert result == [{"type": "anthropic", "skill_id": "xlsx"}], (
        "anthropic refs must be passed through as-is without MA lookup"
    )
    assert "version" not in result[0], "anthropic skill params must not include a version key"


async def test_resolve_refs_mixed() -> None:
    """Mixed custom and anthropic refs resolve in order."""
    canonical = tenant_scoped_display_title(tenant_id=_TENANT_A, name="my-skill")
    client = _make_client([_skill_item(id_="skill_custom_1", display_title=canonical)])
    result = await resolve_refs(
        client,
        refs=[
            SkillRef(type="custom", skill_id="my-skill"),
            SkillRef(type="anthropic", skill_id="xlsx"),
        ],
        tenant_id=_TENANT_A,
    )
    assert result == [
        {"type": "custom", "skill_id": "skill_custom_1"},
        {"type": "anthropic", "skill_id": "xlsx"},
    ], "mixed refs must resolve custom via MA and anthropic as passthrough, in order"
    assert "version" not in result[0]
    assert "version" not in result[1]


async def test_resolve_refs_raises_defaults_error_when_ref_missing() -> None:
    client = _make_client([])
    with pytest.raises(DefaultsError, match="not found"):
        await resolve_refs(
            client,
            refs=[SkillRef(type="custom", skill_id="no-such")],
            tenant_id=_TENANT_A,
        )


async def test_resolve_refs_resolves_bare_name_to_caller_tenant_canonical_skill() -> None:
    """resolve_refs prefixes the bare name internally and matches the canonical prefixed title."""
    canonical = tenant_scoped_display_title(tenant_id=_TENANT_A, name="brainstorming")
    client = _make_client([_skill_item(id_="skill_tenant_a", display_title=canonical)])
    result = await resolve_refs(
        client,
        refs=[SkillRef(type="custom", skill_id="brainstorming")],
        tenant_id=_TENANT_A,
    )
    assert result == [{"type": "custom", "skill_id": "skill_tenant_a"}], (
        "resolve_refs must prefix bare name to tenant canonical title and return that skill's MA id"
    )


async def test_resolve_refs_resolves_caller_tenant_skill_when_two_tenants_share_same_bare_name() -> (
    None
):
    """Two tenants have a skill with same bare name; resolve_refs returns caller's tenant's skill only."""
    title_a = tenant_scoped_display_title(tenant_id=_TENANT_A, name="brainstorming")
    title_b = tenant_scoped_display_title(tenant_id=_TENANT_B, name="brainstorming")
    client = _make_client(
        [
            _skill_item(id_="skill_a", display_title=title_a),
            _skill_item(id_="skill_b", display_title=title_b),
        ]
    )
    result = await resolve_refs(
        client,
        refs=[SkillRef(type="custom", skill_id="brainstorming")],
        tenant_id=_TENANT_A,
    )
    assert result == [{"type": "custom", "skill_id": "skill_a"}], (
        "when two tenants have same-bare-named skills, resolver must return CALLER's tenant skill only"
    )


async def test_resolve_refs_resolves_synced_shaped_bare_name_with_slash() -> None:
    """A synced-shaped bare name like 'daimon/long-skill' resolves through same prefix path."""
    canonical = tenant_scoped_display_title(tenant_id=_TENANT_A, name="daimon/long-skill")
    client = _make_client([_skill_item(id_="skill_synced", display_title=canonical)])
    result = await resolve_refs(
        client,
        refs=[SkillRef(type="custom", skill_id="daimon/long-skill")],
        tenant_id=_TENANT_A,
    )
    assert result == [{"type": "custom", "skill_id": "skill_synced"}], (
        "synced-shaped bare name with '/' must resolve via same prefix path as seeded skills"
    )

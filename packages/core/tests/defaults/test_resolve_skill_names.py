from __future__ import annotations

import uuid

import pytest
from anthropic.types.beta import BetaManagedAgentsSkillParams, SkillListResponse
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.skills import resolve_skill_names
from daimon.core.errors import DefaultsError
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http

_CREATED_AT = "2026-04-21T00:00:00Z"
_TENANT_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_TENANT_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


def _skill_item(*, id_: str, display_title: str, source: str = "custom") -> dict[str, object]:
    return SkillListResponse(
        id=id_,
        type=source,
        display_title=display_title,
        latest_version="1",
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
        source=source,
    ).model_dump(mode="json")


def _make_client(skills: list[dict[str, object]]) -> object:
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(skills))
    return build_fake_anthropic_http(router.dispatch)


async def test_resolve_skill_names_returns_empty_list_when_entries_empty() -> None:
    client = _make_client([])
    result = await resolve_skill_names(client, [], tenant_id=_TENANT_A)
    assert result == [], "empty input must produce empty output, not None or error"


async def test_resolve_skill_names_resolves_all_names_in_order() -> None:
    title_build = tenant_scoped_display_title(tenant_id=_TENANT_A, name="build-models")
    title_compare = tenant_scoped_display_title(tenant_id=_TENANT_A, name="compare-models")
    client = _make_client(
        [
            _skill_item(id_="skill_build", display_title=title_build),
            _skill_item(id_="skill_compare", display_title=title_compare),
        ]
    )
    result = await resolve_skill_names(
        client, ["build-models", "compare-models"], tenant_id=_TENANT_A
    )
    assert result == [
        {"type": "custom", "skill_id": "skill_build"},
        {"type": "custom", "skill_id": "skill_compare"},
    ], "names must resolve to {type:custom, skill_id:<MA id>} in input order"


async def test_resolve_skill_names_passes_anthropic_dict_through_unchanged() -> None:
    """Dict entries with type==anthropic pass through without MA lookup or rejection."""
    client = _make_client([])
    passthrough: BetaManagedAgentsSkillParams = {"type": "anthropic", "skill_id": "pdf"}
    result = await resolve_skill_names(client, [passthrough], tenant_id=_TENANT_A)
    assert result == [{"type": "anthropic", "skill_id": "pdf"}], (
        "anthropic dict entries must pass through unchanged (built-ins need no tenant prefix)"
    )


async def test_resolve_skill_names_raises_with_available_names_when_name_missing() -> None:
    title_build = tenant_scoped_display_title(tenant_id=_TENANT_A, name="build-models")
    title_compare = tenant_scoped_display_title(tenant_id=_TENANT_A, name="compare-models")
    client = _make_client(
        [
            _skill_item(id_="skill_build", display_title=title_build),
            _skill_item(id_="skill_compare", display_title=title_compare),
        ]
    )
    with pytest.raises(DefaultsError) as exc_info:
        await resolve_skill_names(client, ["build-modls"], tenant_id=_TENANT_A)
    message = str(exc_info.value)
    assert "build-modls" in message, "error must name the unresolved skill"
    assert "build-models" in message and "compare-models" in message, (
        "error must list the caller's own available bare skill names"
    )


async def test_resolve_skill_names_raises_when_given_another_tenants_full_canonical_title() -> None:
    """A string that IS another tenant's canonical title gets re-prefixed and misses (D-05/D-09)."""
    # Tenant B's canonical title for "brainstorming"
    tenant_b_canonical = tenant_scoped_display_title(tenant_id=_TENANT_B, name="brainstorming")
    # MA has tenant B's skill only
    client = _make_client(
        [
            _skill_item(id_="skill_b", display_title=tenant_b_canonical),
        ]
    )
    with pytest.raises(DefaultsError) as exc_info:
        # Tenant A tries to attach using tenant B's full canonical title as a string
        await resolve_skill_names(client, [tenant_b_canonical], tenant_id=_TENANT_A)
    message = str(exc_info.value)
    # The error must name the unresolved entry (tenant B's canonical title, as Tenant A gave it)
    assert tenant_b_canonical in message, "error must name the unresolved skill string"
    # The error must NOT expose tenant B's canonical title as an "available" skill
    assert (
        tenant_b_canonical not in message.split("available:")[1]
        if "available:" in message
        else True
    ), "error must not expose another tenant's canonical title in the available list"


async def test_resolve_skill_names_raises_on_raw_custom_skill_id_dict() -> None:
    """Dict entry with type==custom (raw MA id) is rejected per D-09."""
    client = _make_client([])
    raw_id_dict: BetaManagedAgentsSkillParams = {"type": "custom", "skill_id": "skill_01XXX"}
    with pytest.raises(DefaultsError, match="raw skill ids") as exc_info:
        await resolve_skill_names(client, [raw_id_dict], tenant_id=_TENANT_A)
    message = str(exc_info.value)
    assert "raw skill ids" in message, (
        "error must state that raw skill ids cannot be attached; use bare name instead"
    )


async def test_resolve_skill_names_error_excludes_foreign_tenant_titles() -> None:
    """Error listing available skills must show only caller's own bare names, not other tenants'."""
    title_a = tenant_scoped_display_title(tenant_id=_TENANT_A, name="my-skill")
    title_b = tenant_scoped_display_title(tenant_id=_TENANT_B, name="other-skill")
    # Both tenants' skills are in the org-wide MA list
    client = _make_client(
        [
            _skill_item(id_="skill_a", display_title=title_a),
            _skill_item(id_="skill_b", display_title=title_b),
        ]
    )
    with pytest.raises(DefaultsError) as exc_info:
        await resolve_skill_names(client, ["missing-skill"], tenant_id=_TENANT_A)
    message = str(exc_info.value)
    assert "my-skill" in message, "error must list caller's own available bare skill name"
    assert title_b not in message, (
        "error must NOT expose tenant B's canonical title in available list (no cross-tenant leak)"
    )

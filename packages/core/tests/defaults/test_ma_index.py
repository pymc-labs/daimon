from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import httpx
import structlog
from anthropic.types.beta import SkillListResponse
from daimon.core.defaults.ma_index import (
    _SKILLS_PAGE_LIMIT,  # pyright: ignore[reportPrivateUsage]
    find_agent_by_daimon_tag,
    find_skill_by_display_title,
    list_skills_lenient,
    list_skills_strict,
)
from daimon.core.errors import SkillsListTruncatedError
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http
from daimon.testing.ma_models import ma_agent


async def test_find_agent_returns_match() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [
                ma_agent(
                    id="ag_1",
                    name="daimon",
                    model="claude-opus-4-7",
                    tenant_id=tenant_id,
                    created_at=datetime(2026, 4, 21, tzinfo=UTC),
                ).model_dump(mode="json"),
                ma_agent(
                    id="ag_2",
                    name="other",
                    model="claude-opus-4-7",
                    tenant_id=tenant_id,
                    created_at=datetime(2026, 4, 21, tzinfo=UTC),
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)
    match = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name="daimon")
    assert match is not None
    assert match.id == "ag_1"


async def test_find_agent_returns_none_when_missing() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([]))
    client = build_fake_anthropic_http(router.dispatch)
    assert await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name="x") is None


async def test_find_agent_multi_match_returns_most_recent_and_warns() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [
                ma_agent(
                    id="ag_old", name="dupe", model="claude-opus-4-7", tenant_id=tenant_id
                ).model_dump(mode="json"),
                ma_agent(
                    id="ag_new",
                    name="dupe",
                    model="claude-opus-4-7",
                    tenant_id=tenant_id,
                    created_at=datetime(2026, 4, 21, tzinfo=UTC),
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)
    with structlog.testing.capture_logs() as logs:
        match = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name="dupe")
    assert match is not None and match.id == "ag_new"
    assert any(
        r["event"] == "ma_index.multi_match" and r["kind"] == "agents" and r["count"] == 2
        for r in logs
    )


async def test_find_agent_resolver_ambiguous_name_emits_account_aware_warning() -> None:
    """When two agents share the same daimon_name but differ in daimon_account,
    find_agent_by_daimon_tag adopts the newest and emits one resolver_ambiguous_name
    warning with account fields for cross-account collision diagnosis."""
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    account_a = "aaaaaaaa-0000-0000-0000-000000000001"
    account_b = "bbbbbbbb-0000-0000-0000-000000000002"
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [
                ma_agent(
                    id="ag_old",
                    name="dupe",
                    model="claude-opus-4-7",
                    tenant_id=tenant_id,
                    metadata={"daimon_account": account_a},
                ).model_dump(mode="json"),
                ma_agent(
                    id="ag_new",
                    name="dupe",
                    model="claude-opus-4-7",
                    tenant_id=tenant_id,
                    metadata={"daimon_account": account_b},
                    created_at=datetime(2026, 4, 21, tzinfo=UTC),
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)
    with structlog.testing.capture_logs() as logs:
        match = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name="dupe")
    assert match is not None and match.id == "ag_new", "resolver should adopt the newest agent"
    tripwire = [r for r in logs if r["event"] == "ma_index.resolver_ambiguous_name"]
    assert len(tripwire) == 1, "exactly one resolver_ambiguous_name warning should be emitted"
    entry = tripwire[0]
    assert entry["adopted_account"] == account_b, (
        "adopted_account should be the newest agent's daimon_account"
    )
    assert entry["duplicate_accounts"] == [account_a], (
        "duplicate_accounts should list the older agent's daimon_account"
    )


async def test_find_agent_by_daimon_tag_paginates_past_first_page() -> None:
    """Target agent sits on the second page; the helper must follow the
    next_page cursor and return the page-2 match, not the page-1 decoy."""
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    router = MARouter()

    def _agent(ag_id: str, name: str, tag: str) -> dict[str, object]:
        return ma_agent(
            id=ag_id,
            name=name,
            model="claude-opus-4-7",
            tenant_id=tenant_id,
            metadata={"daimon_name": tag},
            created_at=datetime(2026, 4, 21, tzinfo=UTC),
        ).model_dump(mode="json")

    def handle(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        cursor = req.url.params.get("page")
        if cursor is None:
            return httpx.Response(
                200,
                json={
                    "data": [_agent("ag_p1", "other", "other")],
                    "next_page": "cursor-page-2",
                },
            )
        assert cursor == "cursor-page-2", f"unexpected cursor {cursor!r}"
        return httpx.Response(
            200,
            json={
                "data": [_agent("ag_p2", "daimon", "daimon")],
                "next_page": None,
            },
        )

    router.add("GET", r"/v1/agents", handle)
    client = build_fake_anthropic_http(router.dispatch)
    match = await find_agent_by_daimon_tag(client, tenant_id=tenant_id, name="daimon")
    assert match is not None, "target on page 2 must be found after cursor follow"
    assert match.id == "ag_p2", "must adopt the match on page 2, not page 1"


async def test_find_skill_by_display_title() -> None:
    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_1",
                    type="custom",
                    display_title="brainstorming",
                    latest_version="1",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    source="custom",
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)
    match = await find_skill_by_display_title(client, "brainstorming", on_truncation="degrade")
    assert match is not None and match.id == "sk_1"


async def test_find_skill_by_display_title_warns_when_ceiling_hit() -> None:
    """When `skills.list` returns a FULL page with no next_page cursor, the
    helper must emit a ceiling-hit warning so callers know adoption may be
    incomplete. Sized off the constant so raising the limit cannot silently
    turn this into a half-page test that proves nothing."""
    # A full page of filler, no target. `has_more`/`next_page` omitted so
    # async-for stops after the first page (mirrors the MA bug shape).
    filler = _make_filler_skills(_SKILLS_PAGE_LIMIT)
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(filler))
    client = build_fake_anthropic_http(router.dispatch)

    with structlog.testing.capture_logs() as logs:
        match = await find_skill_by_display_title(client, "brainstorming", on_truncation="degrade")

    assert match is None, "target is absent; adoption must return None"
    assert any(
        r["event"] == "ma_index.skills_list_ceiling_hit" and r["limit"] == _SKILLS_PAGE_LIMIT
        for r in logs
    ), f"expected skills_list_ceiling_hit warning, got events: {[r['event'] for r in logs]}"


# ---------------------------------------------------------------------------
# list_skills_strict tests
# ---------------------------------------------------------------------------


def _make_filler_skills(count: int) -> list[dict[str, object]]:
    return [
        SkillListResponse(
            id=f"sk_{i}",
            type="custom",
            display_title=f"unrelated-{i}",
            latest_version="1",
            created_at="2026-04-21T00:00:00Z",
            updated_at="2026-04-21T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        for i in range(count)
    ]


async def test_list_skills_strict_raises_when_page_full() -> None:
    """list_skills_strict must raise SkillsListTruncatedError on a full page."""
    router = MARouter()
    router.add(
        "GET", r"/v1/skills", lambda req, _m: list_response(_make_filler_skills(_SKILLS_PAGE_LIMIT))
    )
    client = build_fake_anthropic_http(router.dispatch)
    import pytest

    with pytest.raises(SkillsListTruncatedError):
        await list_skills_strict(client)


async def test_list_skills_strict_returns_rows_on_partial_page() -> None:
    """list_skills_strict must return rows normally on a one-short-of-full page."""
    router = MARouter()
    partial = _SKILLS_PAGE_LIMIT - 1
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(_make_filler_skills(partial)))
    client = build_fake_anthropic_http(router.dispatch)
    rows = await list_skills_strict(client)
    assert len(rows) == partial, "strict list must return every row from a partial page"


async def test_list_skills_strict_follows_cursor_past_first_full_page() -> None:
    first_page = _make_filler_skills(_SKILLS_PAGE_LIMIT)
    second_page = _make_filler_skills(1)
    second_page[0]["id"] = "sk_after_cursor"
    requested_pages: list[str | None] = []
    router = MARouter()

    def on_list(req: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        cursor = req.url.params.get("page")
        requested_pages.append(cursor)
        if cursor is None:
            return httpx.Response(200, json={"data": first_page, "next_page": "next"})
        assert cursor == "next"
        return httpx.Response(200, json={"data": second_page, "next_page": None})

    router.add("GET", r"/v1/skills", on_list)
    client = build_fake_anthropic_http(router.dispatch)
    rows = await list_skills_strict(client)

    assert len(rows) == _SKILLS_PAGE_LIMIT + 1
    assert rows[-1].id == "sk_after_cursor"
    assert requested_pages == [None, "next"]


# ---------------------------------------------------------------------------
# list_skills_lenient tests
# ---------------------------------------------------------------------------


async def test_list_skills_lenient_truncated_flag_is_true_on_full_page() -> None:
    """list_skills_lenient must return (rows, True) and emit a warning on a full page."""
    router = MARouter()
    router.add(
        "GET", r"/v1/skills", lambda req, _m: list_response(_make_filler_skills(_SKILLS_PAGE_LIMIT))
    )
    client = build_fake_anthropic_http(router.dispatch)
    with structlog.testing.capture_logs() as logs:
        rows, truncated = await list_skills_lenient(client)
    assert truncated is True, "truncated flag must be True when page is full"
    assert len(rows) == _SKILLS_PAGE_LIMIT, "every row must still be returned in lenient mode"
    assert any(r["event"] == "ma_index.skills_list_truncated" for r in logs), (
        f"expected skills_list_truncated warning, got: {[r['event'] for r in logs]}"
    )


async def test_list_skills_lenient_truncated_flag_is_false_on_partial_page() -> None:
    """list_skills_lenient must return (rows, False) on a partial page."""
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(_make_filler_skills(42)))
    client = build_fake_anthropic_http(router.dispatch)
    rows, truncated = await list_skills_lenient(client)
    assert truncated is False, "truncated flag must be False when page is not full"
    assert len(rows) == 42, "all rows must be returned on partial page"


# ---------------------------------------------------------------------------
# find_skill_by_display_title with on_truncation tests
# ---------------------------------------------------------------------------


async def test_find_skill_by_display_title_raises_on_full_page_even_when_match_found() -> None:
    """on_truncation="raise" must raise even when the target title IS in the page."""
    target = SkillListResponse(
        id="sk_target",
        type="custom",
        display_title="target-skill",
        latest_version="1",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")
    # one short of the limit, plus the target = exactly a full page
    skills = _make_filler_skills(_SKILLS_PAGE_LIMIT - 1) + [target]
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(skills))
    client = build_fake_anthropic_http(router.dispatch)
    import pytest

    with pytest.raises(SkillsListTruncatedError):
        await find_skill_by_display_title(client, "target-skill", on_truncation="raise")


async def test_find_skill_by_display_title_degrade_returns_match_on_full_page() -> None:
    """on_truncation="degrade" must return the match even when the page is full."""
    target = SkillListResponse(
        id="sk_target",
        type="custom",
        display_title="target-skill",
        latest_version="1",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        source="custom",
    ).model_dump(mode="json")
    skills = _make_filler_skills(99) + [target]
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(skills))
    client = build_fake_anthropic_http(router.dispatch)
    match = await find_skill_by_display_title(client, "target-skill", on_truncation="degrade")
    assert match is not None and match.id == "sk_target", (
        "degrade mode must return the match from a full page"
    )

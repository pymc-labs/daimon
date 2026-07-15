from __future__ import annotations

import re
import uuid

import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent, SkillListResponse
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_skill_by_display_title,
    list_skills_lenient,
    list_skills_strict,
)
from daimon.core.errors import SkillsListTruncatedError
from daimon.testing.ma import MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http


async def test_find_agent_returns_match() -> None:
    tenant_id = uuid.UUID("70121a77-33ce-566b-a2ee-47d93bc422ae")
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [
                BetaManagedAgentsAgent(
                    id="ag_1",
                    type="agent",
                    name="daimon",
                    model={"id": "claude-opus-4-7"},
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
                    description=None,
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
                ).model_dump(mode="json"),
                BetaManagedAgentsAgent(
                    id="ag_2",
                    type="agent",
                    name="other",
                    model={"id": "claude-opus-4-7"},
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "other"},
                    description=None,
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
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
                BetaManagedAgentsAgent(
                    id="ag_old",
                    type="agent",
                    name="dupe",
                    model={"id": "claude-opus-4-7"},
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "dupe"},
                    description=None,
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
                ).model_dump(mode="json"),
                BetaManagedAgentsAgent(
                    id="ag_new",
                    type="agent",
                    name="dupe",
                    model={"id": "claude-opus-4-7"},
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "dupe"},
                    description=None,
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
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
    """D-09: when two agents share the same daimon_name but differ in daimon_account,
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
                BetaManagedAgentsAgent(
                    id="ag_old",
                    type="agent",
                    name="dupe",
                    model={"id": "claude-opus-4-7"},
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "dupe",
                        "daimon_account": account_a,
                    },
                    description=None,
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
                ).model_dump(mode="json"),
                BetaManagedAgentsAgent(
                    id="ag_new",
                    type="agent",
                    name="dupe",
                    model={"id": "claude-opus-4-7"},
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": "dupe",
                        "daimon_account": account_b,
                    },
                    description=None,
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
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
        return BetaManagedAgentsAgent(
            id=ag_id,
            type="agent",
            name=name,
            model={"id": "claude-opus-4-7"},
            metadata={"daimon_tenant": str(tenant_id), "daimon_name": tag},
            description=None,
            created_at="2026-04-21T00:00:00Z",
            updated_at="2026-04-21T00:00:00Z",
            version=1,
            mcp_servers=[],
            skills=[],
            tools=[],
            system=None,
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
    """When `skills.list` returns a full page of 100 rows with no next_page
    cursor, the helper must emit a ceiling-hit warning so callers know
    adoption may be incomplete."""
    # 100 filler skills, no target. `has_more`/`next_page` omitted so async-for
    # stops after the first page (mirrors the MA bug shape).
    filler = [
        SkillListResponse(
            id=f"sk_{i}",
            type="custom",
            display_title=f"unrelated-{i}",
            latest_version="1",
            created_at="2026-04-21T00:00:00Z",
            updated_at="2026-04-21T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        for i in range(100)
    ]
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(filler))
    client = build_fake_anthropic_http(router.dispatch)

    with structlog.testing.capture_logs() as logs:
        match = await find_skill_by_display_title(client, "brainstorming", on_truncation="degrade")

    assert match is None, "target is absent; adoption must return None"
    assert any(
        r["event"] == "ma_index.skills_list_ceiling_hit" and r["limit"] == 100 for r in logs
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
    """list_skills_strict must raise SkillsListTruncatedError on a 100-row page."""
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(_make_filler_skills(100)))
    client = build_fake_anthropic_http(router.dispatch)
    import pytest

    with pytest.raises(SkillsListTruncatedError):
        await list_skills_strict(client)


async def test_list_skills_strict_returns_rows_on_partial_page() -> None:
    """list_skills_strict must return rows normally on a 99-row page."""
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(_make_filler_skills(99)))
    client = build_fake_anthropic_http(router.dispatch)
    rows = await list_skills_strict(client)
    assert len(rows) == 99, "strict list must return all 99 rows from a partial page"


# ---------------------------------------------------------------------------
# list_skills_lenient tests
# ---------------------------------------------------------------------------


async def test_list_skills_lenient_truncated_flag_is_true_on_full_page() -> None:
    """list_skills_lenient must return (rows, True) and emit a warning on a full page."""
    router = MARouter()
    router.add("GET", r"/v1/skills", lambda req, _m: list_response(_make_filler_skills(100)))
    client = build_fake_anthropic_http(router.dispatch)
    with structlog.testing.capture_logs() as logs:
        rows, truncated = await list_skills_lenient(client)
    assert truncated is True, "truncated flag must be True when page is full"
    assert len(rows) == 100, "all 100 rows must still be returned in lenient mode"
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
    # 99 filler + 1 target = 100 total (full page)
    skills = _make_filler_skills(99) + [target]
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

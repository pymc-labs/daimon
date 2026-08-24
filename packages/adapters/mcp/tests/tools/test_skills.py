from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsCustomSkill,
    SkillListResponse,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.skills import (
    SkillDetail,
    SkillInfo,
    _delete_impl,
    _get_impl,
    _list_impl,
    _sync_impl,
    register_skill_tools,
)
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.scope import DeploymentDefault
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

pytestmark = pytest.mark.asyncio


def _runtime(client: AsyncAnthropic) -> McpRuntime:
    return McpRuntime(
        session_factory=MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


async def test_list_impl_returns_skill_info_list() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_1",
                    display_title=f"{str(tenant_id)[:8]}-my-skill",
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _list_impl(_runtime(client), auth)
    assert isinstance(result, list), "should return a list"
    assert len(result) == 1, "should return one skill"
    assert isinstance(result[0], SkillInfo), "should return SkillInfo items"
    assert result[0].name == "my-skill", "should return bare name stripped of tenant prefix"


async def test_get_impl_returns_skill_detail_with_version_count() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_x",
                    display_title=f"{str(tenant_id)[:8]}-found",
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills/sk_x/versions",
        lambda _req, _m: list_response([{"version": "1"}, {"version": "2"}, {"version": "3"}]),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _get_impl(_runtime(client), auth, "found")
    assert isinstance(result, SkillDetail), "should return a SkillDetail"
    assert result.name == "found", "should return the bare skill name"
    assert result.version_count == 3, "should count all versions"


async def test_get_impl_raises_tool_error_not_found() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda _req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="not found"):
        await _get_impl(_runtime(client), auth, "nope")


async def test_list_impl_excludes_foreign_tenant_skill_and_includes_own_and_builtins() -> None:
    """list_skills returns only the caller's namespace + anthropic built-ins."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    account_id = uuid.uuid4()

    own_skill_display_title = f"{str(tenant_a)[:8]}-my-skill"
    foreign_skill_display_title = f"{str(tenant_b)[:8]}-their-skill"

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_own",
                    display_title=own_skill_display_title,
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json"),
                SkillListResponse(
                    id="sk_foreign",
                    display_title=foreign_skill_display_title,
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json"),
                SkillListResponse(
                    id="sk_builtin",
                    display_title="cli-auth",
                    source="anthropic",
                    type="anthropic",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json"),
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_a, role=Role.ADMIN, is_admin=True)
    result = await _list_impl(_runtime(client), auth)

    result_names = [r.name for r in result]
    assert "my-skill" in result_names, "own-namespace skill must appear with bare name"
    assert "cli-auth" in result_names, "anthropic built-in must appear"
    assert foreign_skill_display_title not in result_names, (
        "foreign tenant's display_title must NOT appear in list result"
    )
    assert not any(foreign_skill_display_title in r.name for r in result), (
        "foreign tenant title must be absent from all result entries"
    )


async def test_list_impl_synced_shaped_skill_displays_as_agent_slash_name() -> None:
    """Synced skills are stored as `{agent}/{name}` body — strip returns `{agent}/{name}`."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_synced",
                    display_title=f"{str(tenant_id)[:8]}-daimon/tool-x",
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    result = await _list_impl(_runtime(client), auth)

    assert len(result) == 1, "synced skill must appear in list"
    assert result[0].name == "daimon/tool-x", (
        "synced skill displays as {agent}/{name} after prefix strip"
    )


async def test_get_impl_foreign_tenant_bare_name_raises_not_found() -> None:
    """get_skill with a bare name belonging to another tenant raises ToolError not-found."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    account_id = uuid.uuid4()

    # Only tenant B's skill is in MA; tenant A asks for "their-skill" bare
    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_b",
                    display_title=f"{str(tenant_b)[:8]}-their-skill",
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_a, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="not found"):
        await _get_impl(_runtime(client), auth, "their-skill")


async def test_delete_impl_calls_delete_skill_and_versions() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    deleted: list[str] = []

    def on_delete_versions(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        return list_response([])

    def on_delete_skill(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        deleted.append(m.group(1))
        return httpx.Response(200)

    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_d",
                    display_title=f"{str(tenant_id)[:8]}-doomed",
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add("GET", r"/v1/skills/sk_d/versions", on_delete_versions)
    router.add("DELETE", r"/v1/skills/([^/]+)", on_delete_skill)
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    await _delete_impl(_runtime(client), auth, "doomed")

    assert deleted == ["sk_d"], "should delete the correct skill ID"


async def test_delete_impl_raises_tool_error_not_found() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add("GET", r"/v1/skills", lambda _req, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)
    with pytest.raises(ToolError, match="not found"):
        await _delete_impl(_runtime(client), auth, "nope")


async def test_sync_impl_returns_outcomes(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    expected_outcome = ResourceOutcome(
        kind="skill",
        name="test-skill",
        action=Action.CREATED,
        anthropic_id="sk_new",
    )

    with (
        patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch,
        patch("daimon.core.skills.pipeline.discover_skills"),
        patch("daimon.core.skills.pipeline.sync_skills") as mock_sync,
    ):
        from daimon.core.skills.fetch import FetchResult

        cleanup_dir = tmp_path / "cleanup"
        cleanup_dir.mkdir()
        mock_fetch.return_value = FetchResult(path=tmp_path, cleanup_dir=cleanup_dir)
        mock_sync.return_value = [expected_outcome]

        # No agent in the tenant attaches the synced skill.
        router = MARouter()
        router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
        client = build_fake_anthropic(router.dispatch)

        auth = AuthIdentity(
            account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
        )
        result = await _sync_impl(
            _runtime(client),
            auth,
            url="https://github.com/org/repo",
            branch="main",
            path="",
        )

    assert result.source_url == "https://github.com/org/repo", "should echo the source repo url"
    assert result.branch == "main", "should echo the synced branch"
    assert result.path == "", "should echo the discovery path"
    assert len(result.outcomes) == 1, "should return one outcome"
    assert result.outcomes[0].name == "test-skill", "should return the skill name from sync"
    assert result.outcomes[0].action == Action.CREATED, "should reflect the created action"
    assert result.registry_count == 1, "one skill landed in the tenant registry"
    assert result.attached_count == 0, "no agent attaches the newly synced skill yet"
    assert not cleanup_dir.exists(), "should clean up the temp directory"


async def test_sync_impl_raises_tool_error_for_invalid_path(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    with patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch:
        from daimon.core.skills.fetch import FetchResult

        cleanup_dir = tmp_path / "cleanup"
        cleanup_dir.mkdir()
        mock_fetch.return_value = FetchResult(path=tmp_path, cleanup_dir=cleanup_dir)

        router = MARouter()
        client = build_fake_anthropic(router.dispatch)

        auth = AuthIdentity(
            account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
        )
        with pytest.raises(ToolError, match="not found in fetched repository"):
            await _sync_impl(
                _runtime(client),
                auth,
                url="https://github.com/org/repo",
                branch="main",
                path="nonexistent/subdir",
            )

    # Cleanup should still happen even when ToolError is raised
    assert not cleanup_dir.exists(), "should clean up temp directory even on error"


async def test_sync_impl_rejects_path_traversal(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    with patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch:
        from daimon.core.skills.fetch import FetchResult

        cleanup_dir = tmp_path / "cleanup"
        cleanup_dir.mkdir()
        mock_fetch.return_value = FetchResult(path=tmp_path, cleanup_dir=cleanup_dir)

        router = MARouter()
        client = build_fake_anthropic(router.dispatch)

        auth = AuthIdentity(
            account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
        )
        with pytest.raises(ToolError, match="escapes the repository root"):
            await _sync_impl(
                _runtime(client),
                auth,
                url="https://github.com/org/repo",
                branch="main",
                path="../../etc",
            )

    assert not cleanup_dir.exists(), "should clean up temp directory even on traversal attempt"


def _agent_json(*, agent_id: str, tenant_id: uuid.UUID, skill_ids: list[str]) -> dict[str, Any]:
    """A minimal MA agent payload tagged for ``tenant_id``, attaching ``skill_ids``."""
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name="agent",
        model={"id": "claude-opus-4-7"},
        # daimon_name too: name lookups go through the metadata tag, not the
        # MA `name` field, so an agent without it is invisible to them.
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "agent"},
        description=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        version=1,
        mcp_servers=[],
        skills=[
            BetaManagedAgentsCustomSkill(skill_id=skill_id, type="custom", version="1")
            for skill_id in skill_ids
        ],
        tools=[],
        system=None,
    ).model_dump(mode="json")


async def test_sync_impl_reports_zero_attached_when_no_agent_attaches_synced_skills(
    tmp_path: Path,
) -> None:
    """Three skills synced into the registry; the tenant's one agent attaches
    none of them — the observed failure case (D-11/D-21) must read
    unmistakably: registry count 3, attached count 0, and the summary names
    both plus the word 'registry'."""
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    outcomes = [
        ResourceOutcome(kind="skill", name="skill-a", action=Action.CREATED, anthropic_id="sk_a"),
        ResourceOutcome(kind="skill", name="skill-b", action=Action.CREATED, anthropic_id="sk_b"),
        ResourceOutcome(kind="skill", name="skill-c", action=Action.UPDATED, anthropic_id="sk_c"),
    ]

    with (
        patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch,
        patch("daimon.core.skills.pipeline.discover_skills"),
        patch("daimon.core.skills.pipeline.sync_skills") as mock_sync,
    ):
        from daimon.core.skills.fetch import FetchResult

        cleanup_dir = tmp_path / "cleanup"
        cleanup_dir.mkdir()
        mock_fetch.return_value = FetchResult(path=tmp_path, cleanup_dir=cleanup_dir)
        mock_sync.return_value = outcomes

        agent_list_calls: list[httpx.Request] = []

        def on_agents_list(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            agent_list_calls.append(req)
            return list_response(
                [_agent_json(agent_id="ag_1", tenant_id=tenant_id, skill_ids=["sk_unrelated"])]
            )

        router = MARouter()
        router.add("GET", r"/v1/agents", on_agents_list)
        client = build_fake_anthropic(router.dispatch)

        auth = AuthIdentity(
            account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
        )
        result = await _sync_impl(
            _runtime(client), auth, url="https://github.com/org/repo", branch="main", path=""
        )

    assert result.registry_count == 3, "all three synced skills land in the tenant registry"
    assert result.attached_count == 0, "none of the synced skills are attached to any agent"
    assert "3" in result.summary and "0" in result.summary, (
        "summary must name both the registry and attached counts"
    )
    assert "librar" in result.summary, "summary must name where the skills landed"
    assert "agent_name" in result.summary, (
        "with no agent named, the summary must say how to attach — describing the gap "
        "without naming the next call is what left skills stranded in the library"
    )
    assert len(agent_list_calls) == 1, "exactly one agent-listing request per sync, not per skill"


async def test_sync_impl_reports_one_attached_when_an_existing_agent_already_has_it(
    tmp_path: Path,
) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    outcomes = [
        ResourceOutcome(kind="skill", name="skill-a", action=Action.CREATED, anthropic_id="sk_a"),
    ]

    with (
        patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch,
        patch("daimon.core.skills.pipeline.discover_skills"),
        patch("daimon.core.skills.pipeline.sync_skills") as mock_sync,
    ):
        from daimon.core.skills.fetch import FetchResult

        cleanup_dir = tmp_path / "cleanup"
        cleanup_dir.mkdir()
        mock_fetch.return_value = FetchResult(path=tmp_path, cleanup_dir=cleanup_dir)
        mock_sync.return_value = outcomes

        router = MARouter()
        router.add(
            "GET",
            r"/v1/agents",
            lambda _req, _m: list_response(
                [_agent_json(agent_id="ag_1", tenant_id=tenant_id, skill_ids=["sk_a"])]
            ),
        )
        client = build_fake_anthropic(router.dispatch)

        auth = AuthIdentity(
            account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
        )
        result = await _sync_impl(
            _runtime(client), auth, url="https://github.com/org/repo", branch="main", path=""
        )

    assert result.registry_count == 1
    assert result.attached_count == 1, "the already-attached synced skill must be counted"
    assert "attached nothing" in result.summary, (
        "an import with no agent_name attaches nothing, so the summary must say so -- "
        "the tenant-wide count alone reads as an attach this call performed"
    )
    assert "already attached" in result.summary, (
        "the summary must attribute the count to prior state, not to this call"
    )


async def test_sync_impl_excludes_failed_outcome_from_both_counts(tmp_path: Path) -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    outcomes = [
        ResourceOutcome(kind="skill", name="skill-ok", action=Action.CREATED, anthropic_id="sk_ok"),
        ResourceOutcome(
            kind="skill", name="skill-bad", action=Action.FAILED, error="boom", anthropic_id=None
        ),
    ]

    with (
        patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch,
        patch("daimon.core.skills.pipeline.discover_skills"),
        patch("daimon.core.skills.pipeline.sync_skills") as mock_sync,
    ):
        from daimon.core.skills.fetch import FetchResult

        cleanup_dir = tmp_path / "cleanup"
        cleanup_dir.mkdir()
        mock_fetch.return_value = FetchResult(path=tmp_path, cleanup_dir=cleanup_dir)
        mock_sync.return_value = outcomes

        router = MARouter()
        router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
        client = build_fake_anthropic(router.dispatch)

        auth = AuthIdentity(
            account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
        )
        result = await _sync_impl(
            _runtime(client), auth, url="https://github.com/org/repo", branch="main", path=""
        )

    assert result.registry_count == 1, "the failed outcome must not inflate the registry count"
    assert result.attached_count == 0
    assert len(result.outcomes) == 2, "the failed outcome still appears in the raw outcomes list"


class _SeedAuthMiddleware(Middleware):
    """Inject an admin AuthIdentity into request state so tool closures that
    read `ctx.get_state("auth")` resolve without the full identity middleware."""

    def __init__(self, auth: AuthIdentity) -> None:
        self._auth = auth

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Any,
    ) -> Any:
        await context.fastmcp_context.set_state("auth", self._auth, serializable=False)
        return await call_next(context)


_DISPATCH_TEST_TENANT_ID = uuid.UUID("00000000-0001-0001-0001-000000000001")


def _skills_one_router() -> MARouter:
    """Router with one own-namespace skill for dispatch identity tests."""
    router = MARouter()
    router.add(
        "GET",
        r"/v1/skills",
        lambda _req, _m: list_response(
            [
                SkillListResponse(
                    id="sk_1",
                    display_title=f"{str(_DISPATCH_TEST_TENANT_ID)[:8]}-my-skill",
                    source="custom",
                    type="custom",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                    latest_version="v1",
                ).model_dump(mode="json")
            ]
        ),
    )
    return router


def _registered_mcp(client: AsyncAnthropic, auth: AuthIdentity) -> FastMCP:
    mcp = FastMCP(name="t")
    mcp.add_middleware(_SeedAuthMiddleware(auth))
    register_skill_tools(mcp, _runtime(client))
    return mcp


async def test_register_skill_tools_registers_verb_first_and_alias_names() -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN, is_admin=True
    )
    mcp = _registered_mcp(build_fake_anthropic(MARouter().dispatch), auth)
    names = {tool.name for tool in await mcp.list_tools()}
    expected = {
        "list_skills",
        "get_skill",
        "sync_skills",
        "delete_skill",
        "skills_list",
        "skills_get",
        "skills_sync",
        "skills_delete",
    }
    assert expected.issubset(names), (
        f"both verb-first and noun-first alias names must be registered; got {names}"
    )


async def test_delete_skill_and_alias_both_carry_admin_tag() -> None:
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN, is_admin=True
    )
    mcp = _registered_mcp(build_fake_anthropic(MARouter().dispatch), auth)
    delete_skill = await mcp.get_tool("delete_skill")
    skills_delete = await mcp.get_tool("skills_delete")
    assert "admin" in delete_skill.tags, "delete_skill must carry the admin tag"
    assert "admin" in skills_delete.tags, "skills_delete alias must carry the admin tag"


async def test_list_skills_and_alias_dispatch_identically() -> None:
    # Use _DISPATCH_TEST_TENANT_ID so the skill prefix in _skills_one_router() matches.
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=_DISPATCH_TEST_TENANT_ID,
        role=Role.ADMIN,
        is_admin=True,
    )
    canonical_mcp = _registered_mcp(build_fake_anthropic(_skills_one_router().dispatch), auth)
    alias_mcp = _registered_mcp(build_fake_anthropic(_skills_one_router().dispatch), auth)

    async with Client(canonical_mcp) as cc, Client(alias_mcp) as ac:
        canonical = await cc.call_tool("list_skills", {})
        alias = await ac.call_tool("skills_list", {})
    assert canonical.structured_content == alias.structured_content, (
        "list_skills and its skills_list alias must dispatch to identical behavior"
    )


async def test_get_skill_and_alias_dispatch_identically() -> None:
    # Use _DISPATCH_TEST_TENANT_ID so tenant_scoped_display_title prefix matches the stub.
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=_DISPATCH_TEST_TENANT_ID,
        role=Role.ADMIN,
        is_admin=True,
    )

    def _router() -> MARouter:
        router = _skills_one_router()
        router.add(
            "GET",
            r"/v1/skills/sk_1/versions",
            lambda _req, _m: list_response([{"version": "1"}]),
        )
        return router

    canonical_mcp = _registered_mcp(build_fake_anthropic(_router().dispatch), auth)
    alias_mcp = _registered_mcp(build_fake_anthropic(_router().dispatch), auth)

    async with Client(canonical_mcp) as cc, Client(alias_mcp) as ac:
        canonical = await cc.call_tool("get_skill", {"name": "my-skill"})
        alias = await ac.call_tool("skills_get", {"name": "my-skill"})
    assert canonical.structured_content == alias.structured_content, (
        "get_skill and its skills_get alias must dispatch to identical behavior"
    )


async def test_sync_impl_attaches_to_the_named_agent_and_preserves_its_existing_skills(
    tmp_path: Path,
) -> None:
    """``agent_name`` closes the import/attach gap in one call.

    Without it, an import left ``attached_count`` structurally 0 — the skill sat
    in the shared library and reached no agent, and nothing in the tool or the
    prompt told the model to make the second call. The union matters as much as
    the attach: an agent that loses its existing skills to gain a new one has
    traded one broken state for another.
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    outcomes = [
        ResourceOutcome(kind="skill", name="skill-a", action=Action.CREATED, anthropic_id="sk_a"),
    ]

    with (
        patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch,
        patch("daimon.core.skills.pipeline.discover_skills"),
        patch("daimon.core.skills.pipeline.sync_skills") as mock_sync,
    ):
        from daimon.core.skills.fetch import FetchResult

        cleanup_dir = tmp_path / "cleanup"
        cleanup_dir.mkdir()
        mock_fetch.return_value = FetchResult(path=tmp_path, cleanup_dir=cleanup_dir)
        mock_sync.return_value = outcomes

        updates: list[dict[str, Any]] = []
        attached_after_update = ["sk_existing"]

        def on_agents_list(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            return list_response(
                [_agent_json(agent_id="ag_1", tenant_id=tenant_id, skill_ids=attached_after_update)]
            )

        def on_agent_get(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            return httpx.Response(
                200,
                json=_agent_json(agent_id="ag_1", tenant_id=tenant_id, skill_ids=["sk_existing"]),
            )

        def on_agent_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            body = json.loads(req.content)
            updates.append(body)
            ids = [s["skill_id"] for s in body["skills"]]
            attached_after_update[:] = ids
            return httpx.Response(
                200, json=_agent_json(agent_id="ag_1", tenant_id=tenant_id, skill_ids=ids)
            )

        router = MARouter()
        router.add("GET", r"/v1/skills", lambda _req, _m: list_response([]))
        router.add("GET", r"/v1/agents$", on_agents_list)
        router.add("GET", r"/v1/agents/ag_1$", on_agent_get)
        router.add("POST", r"/v1/agents/ag_1$", on_agent_update)
        client = build_fake_anthropic(router.dispatch)

        auth = AuthIdentity(
            account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
        )
        result = await _sync_impl(
            _runtime(client),
            auth,
            url="https://github.com/org/repo",
            branch="main",
            path="",
            agent_name="agent",
        )

    assert len(updates) == 1, "the named agent must actually be updated, not just counted"
    attached_ids = {s["skill_id"] for s in updates[0]["skills"]}
    assert "sk_a" in attached_ids, "the freshly imported skill must be attached"
    assert "sk_existing" in attached_ids, (
        "attaching is a union — the agent's existing skills must survive"
    )
    assert result.attached_count == 1, (
        "attached_count must reflect this call's own attach, not the state it saw on the way in"
    )
    assert "agent" in result.summary, "the summary must name the agent it attached to"
    assert "attached nothing" not in result.summary, (
        "an attach did happen here, so the no-agent_name disclaimer must not fire"
    )


async def test_sync_impl_anonymous_404_names_the_credential_remedy() -> None:
    """An anonymous 404 must read as "no credential", not "no such repo".

    GitHub 404s a private repo rather than 403ing it, so the bare status is
    ambiguous. `_sync_impl` knows it sent no token, which makes the private
    case both the likelier read and the only actionable one — and naming the
    remedy is what makes the agent reach for the button instead of
    apologizing for a typo.
    """
    from daimon.core.skills.fetch import GitHubFetchError

    with (
        patch("daimon.adapters.mcp.tools.skills._resolve_sync_token", return_value=None),
        patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch,
    ):
        mock_fetch.side_effect = GitHubFetchError("HTTP 404", status_code=404)

        auth = AuthIdentity(
            account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN, is_admin=True
        )
        with pytest.raises(ToolError, match="request_skill_repo_credential") as excinfo:
            await _sync_impl(
                _runtime(build_fake_anthropic(MARouter().dispatch)),
                auth,
                url="https://github.com/org/private-repo",
                branch="main",
                path="",
            )

    assert "no github credential was available" in str(excinfo.value).lower()


async def test_sync_impl_404_with_a_token_does_not_blame_credentials() -> None:
    """With a token that GitHub accepted, a 404 really is a bad url/branch."""
    from daimon.core.skills.fetch import GitHubFetchError

    with (
        patch("daimon.adapters.mcp.tools.skills._resolve_sync_token", return_value="ghp_x"),
        patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch,
    ):
        mock_fetch.side_effect = GitHubFetchError("HTTP 404", status_code=404)

        auth = AuthIdentity(
            account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN, is_admin=True
        )
        with pytest.raises(ToolError) as excinfo:
            await _sync_impl(
                _runtime(build_fake_anthropic(MARouter().dispatch)),
                auth,
                url="https://github.com/org/repo",
                branch="typo",
                path="",
            )

    assert "request_skill_repo_credential" not in str(excinfo.value), (
        "a 404 on an authenticated fetch is a bad url/branch, not a missing credential"
    )


async def test_sync_impl_refuses_attach_when_registry_skill_collides_with_scoped_mount(
    tmp_path: Path,
) -> None:
    """Attaching a registry skill to an agent owning a same-named scoped skill is refused.

    The create guards stop new colliding skills, but a legacy pair can still be
    joined at attach time — MA would accept the update and then fail every
    session create for the agent. The tool must refuse the attach and say why,
    while the upload half still lands in the registry.
    """
    from daimon.core.defaults.metadata import tenant_scoped_display_title

    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    outcomes = [
        ResourceOutcome(
            kind="skill", name="last30days", action=Action.UPDATED, anthropic_id="sk_reg"
        ),
    ]

    with (
        patch("daimon.core.skills.pipeline.fetch_repo") as mock_fetch,
        patch("daimon.core.skills.pipeline.discover_skills"),
        patch("daimon.core.skills.pipeline.sync_skills") as mock_sync,
    ):
        from daimon.core.skills.fetch import FetchResult

        cleanup_dir = tmp_path / "cleanup"
        cleanup_dir.mkdir()
        mock_fetch.return_value = FetchResult(path=tmp_path, cleanup_dir=cleanup_dir)
        mock_sync.return_value = outcomes

        def on_skills_list(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            return list_response(
                [
                    SkillListResponse(
                        id="sk_reg",
                        type="custom",
                        display_title=tenant_scoped_display_title(
                            tenant_id=tenant_id, name="last30days"
                        ),
                        latest_version="1",
                        created_at="2026-04-21T00:00:00Z",
                        updated_at="2026-04-21T00:00:00Z",
                        source="custom",
                    ).model_dump(mode="json"),
                    SkillListResponse(
                        id="sk_scoped",
                        type="custom",
                        display_title=tenant_scoped_display_title(
                            tenant_id=tenant_id, name="last30days", agent_name="agent"
                        ),
                        latest_version="1",
                        created_at="2026-04-21T00:00:00Z",
                        updated_at="2026-04-21T00:00:00Z",
                        source="custom",
                    ).model_dump(mode="json"),
                ]
            )

        updates: list[dict[str, Any]] = []

        def on_agent_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
            updates.append(json.loads(req.content))
            return httpx.Response(500, json={"error": "must not be called"})

        router = MARouter()
        router.add("GET", r"/v1/skills", on_skills_list)
        router.add(
            "GET",
            r"/v1/agents$",
            lambda _req, _m: list_response(
                [_agent_json(agent_id="ag_1", tenant_id=tenant_id, skill_ids=["sk_scoped"])]
            ),
        )
        router.add(
            "GET",
            r"/v1/agents/ag_1$",
            lambda _req, _m: httpx.Response(
                200, json=_agent_json(agent_id="ag_1", tenant_id=tenant_id, skill_ids=["sk_scoped"])
            ),
        )
        router.add("POST", r"/v1/agents/ag_1$", on_agent_update)
        client = build_fake_anthropic(router.dispatch)

        auth = AuthIdentity(
            account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
        )
        result = await _sync_impl(
            _runtime(client),
            auth,
            url="https://github.com/org/repo",
            branch="main",
            path="",
            agent_name="agent",
        )

    assert updates == [], "agents.update must not run when the merged list has a mount collision"
    assert result.attached_count == 0, "nothing may count as attached when the attach was refused"
    assert "mount" in result.summary, (
        f"the summary must explain the mount collision so the caller can rename; got {result.summary!r}"
    )

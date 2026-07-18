from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
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

        # Provide a minimal client (not used since sync_skills is mocked)
        router = MARouter()
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

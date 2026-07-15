"""Skill tools: sync / list / get / delete.

``register_skill_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.
"""

from __future__ import annotations

import datetime

import httpx
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.defaults.ma_index import find_skill_by_display_title, list_skills_lenient
from daimon.core.defaults.metadata import strip_tenant_prefix, tenant_scoped_display_title
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import get_pat
from daimon.core.ma import delete_skill_and_versions
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.stores.agent_repo_binding import get_bindings_for_repo
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel


class SkillInfo(BaseModel):
    name: str
    id: str
    created_at: datetime.datetime

    @classmethod
    def from_ma(cls, skill: SkillListResponse, *, display_name: str) -> SkillInfo:
        return cls(
            name=display_name,
            id=skill.id,
            created_at=datetime.datetime.fromisoformat(skill.created_at),
        )


class SkillDetail(BaseModel):
    name: str
    id: str
    created_at: datetime.datetime
    version_count: int


class SkillSyncResult(BaseModel):
    """Outcome of a ``skills_sync`` call, carrying the source provenance.

    Synced skills land in the tenant-wide skill registry, so the result echoes
    where they came from (``source_url`` / ``branch`` / ``path``) — the model
    should report that back to the user rather than presenting an opaque list of
    skills with no origin.
    """

    source_url: str
    branch: str
    path: str
    outcomes: list[ResourceOutcome]


async def _resolve_sync_token(
    runtime: McpRuntime,
    auth: AuthIdentity,
    url: str,
) -> str | None:
    """Resolve a GitHub token for syncing ``url``, or None (anonymous fetch).

    The session JWT carries no agent_id claim (SC-4), so the credential is
    resolved from the URL instead: the caller-tenant's ``agent_repo_binding``
    for this repo → that agent's PAT overlay (D-25). Other tenants' bindings
    for the same repo never resolve — no cross-tenant credential bleed.
    """
    if runtime.fernet is None:
        return None
    async with runtime.session_factory() as session:
        bindings = await get_bindings_for_repo(session, repo_url=url)
    for binding in bindings:
        if binding.tenant_id != auth.tenant_id:
            continue
        token = await get_pat(
            principal_id=binding.agent_id,
            agent_id=binding.agent_id,
            sessionmaker=runtime.session_factory,
            fernet=runtime.fernet,
        )
        if token is not None:
            return token
    return None


async def _sync_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    url: str,
    branch: str,
    path: str,
) -> SkillSyncResult:
    _require_admin(auth)
    token = await _resolve_sync_token(runtime, auth, url)
    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            outcomes = await run_skill_sync(
                runtime.client,
                http,
                url=url,
                branch=branch,
                path=path,
                tenant_id=auth.tenant_id,
                token=token,
                max_tarball_bytes=runtime.settings.github.max_tarball_bytes,
                max_tarball_decompressed_bytes=(
                    runtime.settings.github.max_tarball_decompressed_bytes
                ),
            )
        except DaimonError as exc:
            raise ToolError(str(exc)) from exc
    return SkillSyncResult(source_url=url, branch=branch, path=path, outcomes=outcomes)


async def _list_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[SkillInfo]:
    rows, _truncated = await list_skills_lenient(runtime.client)
    result: list[SkillInfo] = []
    for row in rows:
        if row.source == "anthropic":
            # Built-in skills: display by their raw display_title or id (D-11).
            display_name = row.display_title or row.id
            result.append(SkillInfo.from_ma(row, display_name=display_name))
        else:
            bare = strip_tenant_prefix(
                tenant_id=auth.tenant_id, display_title=row.display_title or ""
            )
            if bare is not None:
                # Own-namespace skill: display the bare name (D-10).
                result.append(SkillInfo.from_ma(row, display_name=bare))
            # Foreign-tenant skills are excluded from the result (D-11).
    return result


async def _get_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> SkillDetail:
    canonical = tenant_scoped_display_title(tenant_id=auth.tenant_id, name=name)
    skill = await find_skill_by_display_title(runtime.client, canonical, on_truncation="degrade")
    if skill is None:
        raise ToolError(f"skill '{name}' not found in this server's skills")
    version_count = 0
    async for _ in runtime.client.beta.skills.versions.list(skill.id):
        version_count += 1
    return SkillDetail(
        name=name,
        id=skill.id,
        created_at=datetime.datetime.fromisoformat(skill.created_at),
        version_count=version_count,
    )


async def _delete_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
) -> None:
    _require_admin(auth)
    canonical = tenant_scoped_display_title(tenant_id=auth.tenant_id, name=name)
    skill = await find_skill_by_display_title(runtime.client, canonical, on_truncation="degrade")
    if skill is None:
        raise ToolError(f"skill '{name}' not found in this server's skills")
    await delete_skill_and_versions(runtime.client, skill.id)


def register_skill_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def sync_skills(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        url: str,
        branch: str = "main",
        path: str = "",
    ) -> SkillSyncResult:
        """Sync skills from a GitHub repository. Discovers SKILL.md and creates or updates them.

        LOCAL-FIRST: before syncing an external repo, call ``list_skills`` to see
        what is already installed and prefer an existing skill over pulling a
        near-duplicate. Only sync an external repo the user explicitly asked for.

        Synced skills are added to this tenant's shared skill registry (visible to
        every agent in the tenant), so name where they came from when you report
        back — the returned ``source_url``/``branch``/``path`` echo that provenance.

        Before calling, inspect the repo structure to determine the correct ``path``
        parameter (empty string = repo root). ``branch`` defaults to ``"main"``."""
        return await _sync_impl(runtime, await _auth(ctx), url, branch, path)

    @mcp.tool
    async def list_skills(ctx: Context) -> list[SkillInfo]:  # pyright: ignore[reportUnusedFunction]
        """List all custom skills."""
        return await _list_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def get_skill(ctx: Context, name: str) -> SkillDetail:  # pyright: ignore[reportUnusedFunction]
        """Look up a skill by name. Returns detail including version count."""
        return await _get_impl(runtime, await _auth(ctx), name)

    @mcp.tool(tags={"admin"})
    async def delete_skill(ctx: Context, name: str) -> None:  # pyright: ignore[reportUnusedFunction]
        """Delete a skill and all its versions."""
        await _delete_impl(runtime, await _auth(ctx), name)

    # Back-compat aliases under the old noun-first names. Each delegates to the
    # same ``_*_impl`` so dispatch is identical; the docstring steers search
    # toward the canonical verb-first name.
    @mcp.tool(tags={"admin"})
    async def skills_sync(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        url: str,
        branch: str = "main",
        path: str = "",
    ) -> SkillSyncResult:
        """Sync skills from a GitHub repository (alias of ``sync_skills``).

        Discovers SKILL.md and creates or updates them."""
        return await _sync_impl(runtime, await _auth(ctx), url, branch, path)

    @mcp.tool
    async def skills_list(ctx: Context) -> list[SkillInfo]:  # pyright: ignore[reportUnusedFunction]
        """List all custom skills (alias of ``list_skills``)."""
        return await _list_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def skills_get(ctx: Context, name: str) -> SkillDetail:  # pyright: ignore[reportUnusedFunction]
        """Look up a skill by name (alias of ``get_skill``).

        Returns detail including version count."""
        return await _get_impl(runtime, await _auth(ctx), name)

    @mcp.tool(tags={"admin"})
    async def skills_delete(ctx: Context, name: str) -> None:  # pyright: ignore[reportUnusedFunction]
        """Delete a skill and all its versions (alias of ``delete_skill``)."""
        await _delete_impl(runtime, await _auth(ctx), name)

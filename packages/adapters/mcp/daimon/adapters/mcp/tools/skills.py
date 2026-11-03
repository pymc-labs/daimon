"""Skill tools: sync / list / get / delete.

``register_skill_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.
"""

from __future__ import annotations

import datetime
import time
import uuid

import anthropic
import httpx
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsSkillParams,
    SkillListResponse,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.resource_ports import mcp_scope, walk_skill_versions
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._rule_view import CallerView, load_caller_view, load_skill_owners
from daimon.adapters.mcp.tools.agents import (
    _reject_system_agent,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.github_connect import (
    _github_connect_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import (
    get_chat_origin,
    origin_channel_id,
    resolve_setup_agent,
)
from daimon.core.admin_actions import DelegatedAction
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.ma_index import (
    find_attach_mount_collision,
    find_skill_by_display_title,
    list_agents_by_tenant,
    list_skills_lenient,
)
from daimon.core.defaults.metadata import strip_tenant_prefix, tenant_scoped_display_title
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.spec_merge import merge_skills_with_ma
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import mint_granted_skill_token
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import update_agent
from daimon.core.skills.attachments import delete_skill_unless_attached, still_attached_message
from daimon.core.skills.fetch import GitHubFetchError
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.stores.seeded_skills import list_seeded_skill_names, load_seeded_skill
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
    """Outcome of a ``sync_skills`` call, carrying provenance AND attachment state.

    Synced skills land in the tenant-wide skill registry, so the result echoes
    where they came from (``source_url`` / ``branch`` / ``path``) — the model
    should report that back to the user rather than presenting an opaque list of
    skills with no origin.

    Landing in the library is not the same as being usable by any agent — an
    import with no ``agent_name`` attaches to nothing. This model reports that
    distinction (``registry_count`` / ``attached_count`` / ``summary``) rather
    than the caller inferring it from ``outcomes``' ``action`` values, which
    read as "available now" but say nothing about attachment.

    The counts stayed permanently lopsided while the tool had no way to
    attach: ``attached_count`` was structurally 0 and the summary could only
    describe the gap it could not close. ``agent_name`` closes it.

    ``attached_count`` is tenant-wide, not per-call: re-importing a skill that
    some agent already mounts counts it, even with no ``agent_name``. Only
    ``summary`` distinguishes "this call attached it" from "it was already
    attached", so do not read the count as this call's effect.
    """

    source_url: str
    branch: str
    path: str
    outcomes: list[ResourceOutcome]
    registry_count: int
    """Number of skills created or updated in the tenant's registry by this call."""
    attached_count: int
    """Of ``registry_count``, how many are attached to at least one agent in
    the tenant right now — computed from the tenant's agents at report time,
    not asserted. Recounted after this call's own attach, when one ran."""
    summary: str
    """One-line plain-language statement of both counts, and either what was
    attached or how to attach (e.g. "3 skill(s) imported into the workspace's
    shared skill library; 3 attached to an agent. Attached to 'analyst'.")."""


async def _resolve_sync_token(
    runtime: McpRuntime,
    auth: AuthIdentity,
    url: str,
    http_client: httpx.AsyncClient,
    *,
    agent_id: uuid.UUID | None,
) -> str | None:
    """Use an App token only when the target agent has an active repo grant."""
    async with runtime.session_factory() as session:
        return await mint_granted_skill_token(
            session,
            http_client,
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            repo_url=url,
            app_id=runtime.settings.github_app.app_id,
            app_private_key=runtime.settings.github_app.private_key,
            now=int(time.time()),
        )


async def _attach_synced_skills(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    skill_ids: set[str],
    expected_ma_agent_id: str | None = None,
) -> str:
    """Attach ``skill_ids`` to ``agent_name``, returning a one-line outcome.

    Returns prose rather than raising, because the upload has already
    succeeded by the time this runs: a failure here is partial, not total,
    and the caller must report both halves truthfully rather than lose the
    successful import behind an exception.
    """
    try:
        agent = await resolve_setup_agent(
            runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
        )
    except ToolError as exc:
        return f"Could not attach: {exc} The skills are in the registry."

    new_skills: list[BetaManagedAgentsSkillParams] = [
        {"type": "custom", "skill_id": skill_id} for skill_id in sorted(skill_ids)
    ]

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        merged = merge_skills_with_ma(new_skills, fresh)
        if len(merged) > AGENT_SKILL_CAP:
            raise ToolError(
                f"Cannot attach: the merged skill set ({len(merged)}) exceeds this "
                f"organization's per-agent skill limit ({AGENT_SKILL_CAP})."
            )
        collision = await find_attach_mount_collision(
            runtime.client, tenant_id=auth.tenant_id, skills=merged
        )
        if collision is not None:
            raise ToolError(f"Cannot attach: {collision}")
        return await update_agent(
            runtime.client,
            fresh.id,
            version=fresh.version,
            payload={"skills": merged},
            scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
        )

    try:
        await update_agent_with_version_retry(
            runtime.client,
            agent.id,
            _apply,
            scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
        )
    except (ToolError, DaimonError, anthropic.APIStatusError) as exc:
        return f"Uploaded to the registry, but attaching to '{agent_name}' failed: {exc}"
    return f"Attached to '{agent_name}'."


async def _sync_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    url: str,
    branch: str,
    path: str,
    agent_name: str | None = None,
    expected_ma_agent_id: str | None = None,
    origin_context_id: str | None = None,
) -> SkillSyncResult:
    _require_admin(auth, action=DelegatedAction.SKILLS)
    agent_id: uuid.UUID | None = None
    if agent_name is not None:
        agent = await resolve_setup_agent(
            runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
        )
        # Refused before the import rather than at attach time, so a seeded
        # target leaves nothing half-done behind.
        _reject_system_agent(agent)
        expected_ma_agent_id = agent.id
        agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))
    async with runtime.session_factory() as session:
        seeded_skill_names = await list_seeded_skill_names(session, tenant_id=auth.tenant_id)
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _resolve_sync_token(
            runtime,
            auth,
            url,
            http,
            agent_id=agent_id,
        )
        try:
            outcomes = await run_skill_sync(
                runtime.client,
                http,
                url=url,
                branch=branch,
                path=path,
                tenant_id=auth.tenant_id,
                seeded_skill_names=seeded_skill_names,
                # `_require_admin` above.
                is_admin=True,
                token=token,
                max_tarball_bytes=runtime.settings.github.max_tarball_bytes,
                max_tarball_decompressed_bytes=(
                    runtime.settings.github.max_tarball_decompressed_bytes
                ),
            )
        except GitHubFetchError as exc:
            # A 404 from an ANONYMOUS fetch is ambiguous: GitHub returns it for
            # a private repo as well as a missing one, so that the caller
            # cannot probe for existence. We know `token is None` here, which
            # makes "private, and nothing authorized us" by far the likelier
            # read — and it is the only one the user can act on. Left as the
            # bare "HTTP 404" this reads as "that repo does not exist", which
            # is why the agent used to apologize for a typo instead of asking
            # for a credential.
            if exc.status_code == 404 and token is None:
                if agent_name is not None and origin_context_id is not None:
                    await _github_connect_impl(
                        runtime,
                        auth,
                        origin_context_id=origin_context_id,
                        agent_name=agent_name,
                        expected_ma_agent_id=expected_ma_agent_id,
                        repo_name=url.removeprefix("https://github.com/").removesuffix(".git"),
                        required_ability="read",
                    )
                    raise ToolError(
                        f"No active GitHub App grant for {url!r}. "
                        "The Connect GitHub button was posted."
                    ) from exc
                raise ToolError(
                    f"No GitHub credential was available for {url!r}, and an "
                    "unauthenticated fetch got HTTP 404 — which GitHub returns "
                    "for a private repo as well as a missing one. If this repo "
                    "is private, call `github_connect` for this agent and repo to post "
                    "the Connect GitHub button. If it is public, re-check "
                    "the url and branch."
                ) from exc
            raise ToolError(str(exc)) from exc
        except DaimonError as exc:
            raise ToolError(str(exc)) from exc

    registry_ids = {
        outcome.anthropic_id
        for outcome in outcomes
        if outcome.anthropic_id is not None and outcome.action in (Action.CREATED, Action.UPDATED)
    }
    attach_note = ""
    if agent_name is not None and registry_ids:
        attach_note = " " + await _attach_synced_skills(
            runtime,
            auth,
            agent_name=agent_name,
            skill_ids=registry_ids,
            expected_ma_agent_id=expected_ma_agent_id,
        )

    # Recounted AFTER any attach so this call's own attach is included. Note the
    # count is tenant-wide -- "of what I imported, how much is attached to some
    # agent" -- so it can be non-zero on an import that attached nothing. The
    # summary has to say which of the two it is, or the caller reads a re-import
    # of already-attached skills as an attach it just performed.
    agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    attached_ids = {skill.skill_id for agent in agents for skill in agent.skills}
    attached = registry_ids & attached_ids

    library = f"{len(registry_ids)} skill(s) imported into the shared skill library"
    if agent_name is not None:
        summary = f"{library}; {len(attached)} now attached to an agent." + attach_note
    else:
        summary = (
            f"{library}. This call attached nothing: {len(attached)} of the "
            f"{len(registry_ids)} are already attached to an agent. Pass agent_name "
            "to import and attach in one step."
        )
    return SkillSyncResult(
        source_url=url,
        branch=branch,
        path=path,
        outcomes=outcomes,
        registry_count=len(registry_ids),
        attached_count=len(attached),
        summary=summary,
    )


async def _skill_view(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> CallerView:
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    return await load_caller_view(
        runtime,
        auth,
        location_channel_id=origin_channel_id(origin),
        location_thread_id=origin.thread_id if origin is not None else None,
    )


async def _hidden(
    runtime: McpRuntime,
    auth: AuthIdentity,
    skill: SkillListResponse,
    origin_context_id: str | None = None,
) -> bool:
    """Whether `skill` belongs, or may belong, to an agent across an isolated channel's line."""
    caller = await _skill_view(runtime, auth, origin_context_id)
    owners = await load_skill_owners(runtime, caller, auth.tenant_id)
    body = strip_tenant_prefix(tenant_id=auth.tenant_id, display_title=skill.display_title or "")
    return caller.hides_skill(owners, skill_id=skill.id, body=body or "")


async def _list_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin_context_id: str | None = None,
) -> list[SkillInfo]:
    rows, _truncated = await list_skills_lenient(runtime.client)
    caller = await _skill_view(runtime, auth, origin_context_id)
    owners = await load_skill_owners(runtime, caller, auth.tenant_id)
    result: list[SkillInfo] = []
    for row in rows:
        if row.source == "anthropic":
            # Built-in skills: display by their raw display_title or id.
            display_name = row.display_title or row.id
            result.append(SkillInfo.from_ma(row, display_name=display_name))
        else:
            bare = strip_tenant_prefix(
                tenant_id=auth.tenant_id, display_title=row.display_title or ""
            )
            if bare is not None and not caller.hides_skill(owners, skill_id=row.id, body=bare):
                # Own-namespace skill: display the bare name.
                result.append(SkillInfo.from_ma(row, display_name=bare))
            # Foreign-tenant skills are excluded from the result.
    return result


async def _get_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    name: str,
    origin_context_id: str | None = None,
) -> SkillDetail:
    canonical = tenant_scoped_display_title(tenant_id=auth.tenant_id, name=name)
    skill = await find_skill_by_display_title(runtime.client, canonical, on_truncation="degrade")
    if skill is None or await _hidden(runtime, auth, skill, origin_context_id):
        raise ToolError(f"skill '{name}' not found in this server's skills")
    version_count = 0
    async for _ in walk_skill_versions(runtime.client, skill.id, scope=mcp_scope(auth)):
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
    _require_admin(auth, action=DelegatedAction.SKILLS)
    # A seeded skill is mounted by the seeded agents, and deleting a skill an
    # agent references fails every one of that agent's turns.
    async with runtime.session_factory() as session:
        seeded = await load_seeded_skill(session, tenant_id=auth.tenant_id, name=name)
    if seeded is not None:
        raise ToolError(
            f"skill '{name}' is one of Daimon's default skills and cannot be "
            "deleted from chat. Use remove_skill to detach it from an agent you own."
        )
    canonical = tenant_scoped_display_title(tenant_id=auth.tenant_id, name=name)
    skill = await find_skill_by_display_title(runtime.client, canonical, on_truncation="degrade")
    if skill is None or await _hidden(runtime, auth, skill):
        raise ToolError(f"skill '{name}' not found in this server's skills")
    # A skill an agent pins or a channel rule adds is resolved at every session
    # creation there; deleting it would fail each of those turns. Chat never
    # detaches in bulk: each removal goes through its own tool and its checks.
    async with runtime.session_factory() as session, session.begin():
        outcome = await delete_skill_unless_attached(
            runtime.client,
            session,
            skill_id=skill.id,
            detach=False,
            scope=resource_scope(tenant_id=str(auth.tenant_id), account_id=str(auth.account_id)),
        )
    if not outcome.deleted:
        raise ToolError(
            still_attached_message(
                name,
                outcome.attachments,
                agent_detach="remove it from each agent with remove_skill",
                channel_detach="remove it from each channel with remove_channel_skill",
                force_hint="Then delete it again.",
                viewer_tenant_id=auth.tenant_id,
            )
        )


def register_skill_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def sync_skills(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        url: str,
        branch: str = "main",
        path: str = "",
        agent_name: str | None = None,
        expected_ma_agent_id: str | None = None,
        origin_context_id: str | None = None,
    ) -> SkillSyncResult:
        """Install skills from a GitHub repo into the workspace's shared skill library.

        First use ``list_skills`` and prefer existing skills; import only a repository
        the user requested. For a private repository, pass ``agent_name`` and
        ``origin_context_id`` so a missing App grant posts Connect GitHub.

        Discovers SKILL.md files and imports or updates skills. Pass ``agent_name`` to
        also attach them, preserving existing skills; omit it only to stock the library.
        Importing alone attaches nothing. Report returned source, branch, path and
        summary. Inspect the repo to choose ``path`` (empty means root); ``branch``
        defaults to main. Importing is admin-only."""
        return await _sync_impl(
            runtime,
            await _auth(ctx),
            url,
            branch,
            path,
            agent_name,
            expected_ma_agent_id,
            origin_context_id,
        )

    @mcp.tool
    async def list_skills(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str | None = None
    ) -> list[SkillInfo]:
        """List visible custom skills. Pass turn_controls.origin_context_id so member
        reads include skills belonging to this conversation's local responders."""
        return await _list_impl(runtime, await _auth(ctx), origin_context_id)

    @mcp.tool
    async def get_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, name: str, origin_context_id: str | None = None
    ) -> SkillDetail:
        """Look up a visible skill by name, including version count. Pass
        turn_controls.origin_context_id for skills of local responders."""
        return await _get_impl(runtime, await _auth(ctx), name, origin_context_id)

    @mcp.tool(tags={"admin"})
    async def delete_skill(ctx: Context, name: str) -> None:  # pyright: ignore[reportUnusedFunction]
        """Delete a skill such as eda from the workspace, destroying all versions for every agent.

        Refused while any agent or channel still uses the skill; the refusal names them.
        Detach with ``remove_skill`` and ``remove_channel_skill`` first. For detaching
        one agent's reference, use ``remove_skill`` instead. Admin-only."""
        await _delete_impl(runtime, await _auth(ctx), name)

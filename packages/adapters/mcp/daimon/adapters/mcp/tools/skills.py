"""Skill tools: sync / list / get / delete.

``register_skill_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.
"""

from __future__ import annotations

import datetime
import time

import anthropic
import httpx
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsSkillParams,
    SkillListResponse,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_attach_mount_collision,
    find_skill_by_display_title,
    list_agents_by_tenant,
    list_skills_lenient,
)
from daimon.core.defaults.metadata import strip_tenant_prefix, tenant_scoped_display_title
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.spec_merge import merge_skills_with_ma
from daimon.core.errors import DaimonError
from daimon.core.github_app_auth import build_app_jwt, get_installation_id_for_repo
from daimon.core.github_credentials import get_pat
from daimon.core.github_repo_auth import InstallationLookup, resolve_skill_sync_token
from daimon.core.ma import delete_skill_and_versions, update_agent_with_version_retry
from daimon.core.skills.fetch import GitHubFetchError
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.stores.agent_repo_binding import get_bindings_for_repo
from daimon.core.stores.domain import RepoProofKind
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, SecretStr


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
    """Outcome of a ``skills_sync`` call, carrying provenance AND attachment state.

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
) -> str | None:
    """Resolve a GitHub token for syncing ``url``, or None (anonymous fetch).

    The session JWT carries no agent_id claim (SC-4), so the credential is
    resolved from the URL instead: the caller-tenant's ``agent_repo_binding``
    rows for this repo → that agent's PAT overlay, and (independently) any
    recorded proof of access this tenant established for the same repo.
    Other tenants' bindings for the same repo never resolve a per-agent PAT
    and never count as this tenant's proof — no cross-tenant credential
    bleed. Because the loop returns on the first non-``None`` overlay PAT,
    an agent whose overlay resolves short-circuits the loop — correct, since
    only one per-agent credential is needed.

    The full precedence decision (per-agent token -> GitHub App installation
    -> operator fallback -> anonymous) is delegated to
    ``daimon.core.github_repo_auth.resolve_skill_sync_token``, so this
    resolver and the per-agent skill-sync pipeline's credential path cannot
    drift from each other — see that function (and its
    ``select_skill_sync_auth`` pure decision table) for the ordering itself,
    not restated here. Critically, the App installation tier is gated on
    ``proof_kind``: a tenant that has never bound this repo (with proof)
    itself is never eligible for the App tier, even when the deployment's
    App happens to cover it for some unrelated tenant's own bound repo —
    App coverage is installed by repo owners for their own use, not by the
    tenant requesting this sync, so it cannot stand in for a demonstrated
    access check. This is the interactive caller, so the installation
    lookup built below is a LIVE GitHub lookup rather than a cached read of
    the installations table — that split is deliberate (see
    ``resolve_skill_sync_token``'s docstring).
    """
    if runtime.fernet is None:
        return None
    fallback_pat = (
        runtime.settings.github.fallback_pat.get_secret_value()
        if runtime.settings.github.fallback_pat is not None
        else None
    )
    async with runtime.session_factory() as session:
        bindings = await get_bindings_for_repo(session, repo_url=url)
    tenant_bindings = [binding for binding in bindings if binding.tenant_id == auth.tenant_id]
    proof_kind: RepoProofKind | None = next(
        (binding.proof_kind for binding in tenant_bindings if binding.proof_kind is not None),
        None,
    )
    per_agent_pat: str | None = None
    for binding in tenant_bindings:
        token = await get_pat(
            principal_id=binding.agent_id,
            agent_id=binding.agent_id,
            sessionmaker=runtime.session_factory,
            fernet=runtime.fernet,
            allow_service_default=False,
            fallback_pat=None,
        )
        if token is not None:
            per_agent_pat = token
            break

    github_app_id = runtime.settings.github.app_id
    github_app_private_key = runtime.settings.github.app_private_key
    installation_lookup: InstallationLookup | None = None
    if github_app_id is not None and github_app_private_key is not None:
        # Interactive path — a live GitHub lookup is the right freshness
        # choice here (an unattended batch caller would inject a cached read
        # of the installations table instead).
        resolved_app_id: str = github_app_id
        resolved_app_private_key: SecretStr = github_app_private_key

        async def _live_installation_lookup(owner: str, repo: str) -> int | None:
            app_jwt = build_app_jwt(
                resolved_app_private_key.get_secret_value(),
                resolved_app_id,
                now=int(time.time()),
            )
            return await get_installation_id_for_repo(
                http_client, jwt=app_jwt, owner=owner, repo=repo
            )

        installation_lookup = _live_installation_lookup

    return await resolve_skill_sync_token(
        http_client,
        repo_url=url,
        per_agent_pat=per_agent_pat,
        proof_kind=proof_kind,
        fallback_pat=fallback_pat,
        app_id=github_app_id,
        app_private_key=github_app_private_key,
        installation_lookup=installation_lookup,
        now=int(time.time()),
    )


async def _attach_synced_skills(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    skill_ids: set[str],
) -> str:
    """Attach ``skill_ids`` to ``agent_name``, returning a one-line outcome.

    Returns prose rather than raising, because the upload has already
    succeeded by the time this runs: a failure here is partial, not total,
    and the caller must report both halves truthfully rather than lose the
    successful import behind an exception.
    """
    agent = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=agent_name
    )
    if agent is None:
        return f"Could not attach: agent '{agent_name}' not found. The skills are in the registry."

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
        return await runtime.client.beta.agents.update(
            fresh.id, version=fresh.version, skills=merged
        )

    try:
        await update_agent_with_version_retry(runtime.client, agent.id, _apply)
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
) -> SkillSyncResult:
    _require_admin(auth)
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _resolve_sync_token(runtime, auth, url, http)
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
                raise ToolError(
                    f"No GitHub credential was available for {url!r}, and an "
                    "unauthenticated fetch got HTTP 404 — which GitHub returns "
                    "for a private repo as well as a missing one. If this repo "
                    "is private, call `request_skill_repo_credential` with this "
                    "url/branch/path to post a button the user can paste a "
                    "token into; it syncs on submit. If it is public, re-check "
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
            runtime, auth, agent_name=agent_name, skill_ids=registry_ids
        )

    # Recounted AFTER any attach, so attached_count reflects this call's own
    # effect rather than the state it observed on the way in.
    agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    attached_ids = {skill.skill_id for agent in agents for skill in agent.skills}
    attached = registry_ids & attached_ids

    summary = (
        f"{len(registry_ids)} skill(s) imported into the workspace's shared skill "
        f"library; {len(attached)} attached to an agent."
        + (
            attach_note
            or " Importing and attaching are separate steps — pass agent_name to do both."
        )
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


async def _list_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[SkillInfo]:
    rows, _truncated = await list_skills_lenient(runtime.client)
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
            if bare is not None:
                # Own-namespace skill: display the bare name.
                result.append(SkillInfo.from_ma(row, display_name=bare))
            # Foreign-tenant skills are excluded from the result.
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
        agent_name: str | None = None,
    ) -> SkillSyncResult:
        """Import skills from a GitHub repo into this workspace's shared skill library.

        Discovers SKILL.md files and creates or updates them. This is an IMPORT,
        not a per-agent change: the library is visible to every agent in the
        workspace, and importing alone attaches nothing.

        Pass ``agent_name`` to also attach everything imported to that agent —
        which is almost always what someone means by "add this skill to my
        agent". Omit it only to stock the library without touching any agent.
        Existing skills on the agent are preserved; the attach is a union.

        LOCAL-FIRST: before importing an external repo, call ``list_skills`` to
        see what is already in the library and prefer an existing skill over
        pulling a near-duplicate. Only import a repo the user explicitly asked for.

        Report where the skills came from — the returned
        ``source_url``/``branch``/``path`` echo that provenance, and ``summary``
        states both what was imported and what was attached.

        Before calling, inspect the repo structure to determine the correct ``path``
        parameter (empty string = repo root). ``branch`` defaults to ``"main"``.

        IF THIS FAILS BECAUSE THE REPO IS PRIVATE AND UNREADABLE, call
        ``request_repo_binding`` for that repo — do not report the sync as
        blocked and do not ask the user to paste a token here. This tool
        deliberately has no token parameter: the credential is supplied
        out-of-band through a button and modal so it never passes through tool
        arguments. Once the binding exists, the per-agent tier short-circuits
        every later sync of that repo with zero GitHub I/O. Credential
        precedence is documented on ``_resolve_skill_sync_credentials`` above,
        which delegates to ``resolve_skill_sync_token``; a missing GitHub App
        installation is NOT a blocker, it is just a later tier."""
        return await _sync_impl(runtime, await _auth(ctx), url, branch, path, agent_name)

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
        agent_name: str | None = None,
    ) -> SkillSyncResult:
        """Import skills from a GitHub repo into the shared library (alias of ``sync_skills``).

        Discovers SKILL.md and creates or updates them. Pass ``agent_name`` to
        also attach them to that agent; importing alone attaches nothing."""
        return await _sync_impl(runtime, await _auth(ctx), url, branch, path, agent_name)

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

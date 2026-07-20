"""Write-path helpers for the /agent-setup panel.

Wraps the existing reconcile_agent + archive paths; adds tenant-shared roster
loading (all non-archived MA agents in the tenant) and a display-only
mask helper for PAT/MCP-token last-4 surfaces.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, Final

import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.constants import ALLOWED_MODEL_IDS
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_agents_by_daimon_tag,
    list_agents_by_tenant,
    list_skills_lenient,
)
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    build_metadata,
    strip_tenant_prefix,
)
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.skills import resolve_refs
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import (
    build_multifernet,
    get_github_login,
    get_pat,
    upsert_credential_encrypted,
)
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.skill_sync import SyncReport, sync_agent_skills
from daimon.core.specs import (
    AgentSpec,
    SkillRepo,
    dump_agent_spec,
    merge_default_agent_toolset,
)
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.agent_repo_binding import get_binding, set_binding
from pydantic import ValidationError

from .state import PanelState, RosterEntry

_log = structlog.get_logger()

_FORK_COPY_FIELDS: Final = frozenset(
    {"name", "model", "description", "system", "tools", "mcp_servers", "skills", "metadata"}
)

if TYPE_CHECKING:
    from cryptography.fernet import MultiFernet


def validate_model_id(model: str) -> str | None:
    """Return an error message if `model` is not in the allow-list; None if valid.

    UX-25-03: Discord modals cannot contain Select components, so we validate
    free-text input at submit time against ALLOWED_MODEL_IDS.
    """
    if model not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        return f"Model `{model}` is not allowed. Choose one of: {allowed}"
    return None


def mask_tail(secret: str) -> str:
    """Display-only mask. Never call from a logger that records `secret` plain."""
    if len(secret) < 4:
        return "****"
    return f"****{secret[-4:]}"


def _build_roster_entry(
    agent: BetaManagedAgentsAgent, *, custom_skill_titles: dict[str, str]
) -> RosterEntry:
    """Build a panel RosterEntry from the full MA agent response.

    Hydrates `mcp_servers`, `skills`, and `tools` so that downstream
    actions (Fork in particular) carry the full agent shape forward
    rather than dropping back to a name+model+system minimal spec.

    `SkillRef.skill_id` is authoring-time identity (bare authoring name for
    custom skills, anthropic skill id for built-ins). MA's agent payload only
    carries the resolved MA skill id, so we translate via `custom_skill_titles`
    (id → bare-name map built by the caller; only this tenant's skills are
    present). The save path re-prefixes bare names to canonical titles via
    `resolve_refs(tenant_id=...)` (chokepoint).
    Without the translation, `reconcile_agent`'s `resolve_refs` would try to
    look up the MA id as a bare name and fail.

    model_id, mcp_servers, skills, and tools all flow through
    `AgentSpec.model_validate` so the SDK's Literal / discriminator
    validation runs at the boundary — malformed shapes raise SpecError
    instead of leaking into a later reconcile.
    """
    mcp_servers = [server.model_dump(mode="python") for server in agent.mcp_servers] or None
    skills: list[dict[str, str]] = []
    for skill in agent.skills:
        if skill.type == "custom":
            title = custom_skill_titles.get(skill.skill_id)
            if title is None:
                # Dangling skill ref: MA's agents endpoint still lists a skill_id
                # that skills.list no longer returns (orphan/GC'd skill, or a
                # skills.list scope/pagination drop). Skip the ref rather than
                # crashing — otherwise the agent becomes un-editable from the
                # panel forever. The user sees one fewer skill in the embed; a
                # subsequent reconcile will re-attach if they re-add the repo.
                _log.warning(
                    "panel.skill_ref_dropped",
                    agent_name=agent.name,
                    skill_id=skill.skill_id,
                    reason="not in skills.list",
                )
                continue
            skills.append({"type": "custom", "skill_id": title})
        else:
            skills.append({"type": skill.type, "skill_id": skill.skill_id})
    tools = [tool.model_dump(mode="python") for tool in agent.tools] or None
    try:
        spec = AgentSpec.model_validate(
            {
                "name": agent.name,
                "model": agent.model.id,
                "system": agent.system,
                "mcp_servers": mcp_servers,
                "skills": skills,
                "tools": tools,
            }
        )
    except ValidationError as err:
        raise DaimonError(f"Cannot rebuild AgentSpec for {agent.name!r}: {err}") from err
    return RosterEntry(name=agent.name, model=agent.model.id, spec=spec, ma_agent_id=str(agent.id))


async def _build_custom_skill_title_map(
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
) -> dict[str, str]:
    """Return MA-skill-id → bare authoring name for this tenant's custom skills.

    Uses list_skills_lenient for the single skills.list call so
    truncation is observable (structlog warning + Sentry) but non-fatal in
    this read context. Only includes skills whose display_title carries this
    tenant's prefix — foreign-tenant and unprefixed-legacy skills are excluded.
    Their skill_ids produce a None title in _build_roster_entry, which hits the
    existing dangling-ref skip branch (panel.skill_ref_dropped warning).

    The map value is the BARE authoring name (prefix stripped), not the canonical
    title. This means SkillRef.skill_id in panel state holds bare names; the save
    path (replace_agent_resources_for_panel) re-prefixes via resolve_refs(tenant_id=...).
    """
    rows, _truncated = await list_skills_lenient(anthropic)
    titles: dict[str, str] = {}
    for sk in rows:
        if sk.source == "custom" and sk.display_title is not None:
            bare = strip_tenant_prefix(tenant_id=tenant_id, display_title=sk.display_title)
            if bare is not None:
                titles[sk.id] = bare
    return titles


async def load_tenant_roster(
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
) -> list[RosterEntry]:
    """Return the full tenant roster — all non-archived agents in the tenant.

    Every member of the guild sees the same roster (guild-account-owned,
    legacy user-stamped, and unstamped system agents alike). The per-user
    account filter is retired.

    Defaults-managed agents (daimon_managed="true") carry is_system=True so the
    panel gates edit/delete on that flag; guild seed also stamps daimon_account
    on these agents (defaults/_reconcile.py), so account presence alone cannot
    distinguish them from forks. Everyone sees seeded agents but nobody owns
    them for editing purposes — Fork is the edit path (#160).
    """
    all_agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    custom_skill_titles = await _build_custom_skill_title_map(anthropic, tenant_id=tenant_id)
    out: list[RosterEntry] = []
    for agent in all_agents:
        entry = _build_roster_entry(agent, custom_skill_titles=custom_skill_titles)
        # is_system keys off the reconciler's own provenance marker
        # (daimon_managed="true"), not daimon_account: seeded agents ARE
        # account-stamped by the guild seed path, so the account stamp alone
        # cannot discriminate seeded vs. forked agents. Panel forks explicitly
        # stamp managed=False (call_reconcile_for_panel above) so they stay
        # editable. Reconciler behavior is unchanged — scalar edits to seeded
        # agents would still be reverted on the next `defaults apply`; the
        # panel now simply never offers Edit on them.
        is_system = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
        out.append(dataclasses.replace(entry, is_system=is_system))
    return out


async def load_selected_github_login(
    runtime: DiscordRuntime, *, tenant_id: uuid.UUID, entry: RosterEntry | None
) -> str | None:
    """Return the GitHub login linked to ``entry``'s agent, or None.

    Reads the per-agent overlay (overlay-only — no principal-default
    bleed). Display only: the token is never read or decrypted. None when
    nothing is selected, the agent isn't created yet (New/Fork), or no
    credential is linked for that agent.
    """
    if entry is None or not entry.ma_agent_id:
        return None
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=entry.ma_agent_id)
    return await get_github_login(
        principal_id=agent_uuid,
        agent_id=agent_uuid,
        sessionmaker=runtime.sessionmaker,
    )


async def call_reconcile_for_panel(
    runtime: DiscordRuntime, state: PanelState, *, tenant_id: uuid.UUID
) -> ResourceOutcome:
    """Reconcile the currently-selected agent.

    Propagates `account_id` (per-user metadata stamp) and `public_url`
    (default-MCP merge). Raises DaimonError if nothing is selected.
    """
    if state.selected is None:
        raise DaimonError("No agent selected; cannot reconcile.")
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    return await reconcile_agent(
        runtime.anthropic,
        state.selected.spec,
        tenant_id=tenant_id,
        dry_run=False,
        account_id=state.guild_account_id,  # SC-2: stamp guild account, not personal
        public_url=public_url,
        # User-owned forks must NOT be stamped daimon_managed=true — that
        # marker is what makes the sweep eligible to archive them on the
        # next defaults apply (live repro: panel edit → fork archived).
        managed=False,
    )


async def replace_agent_resources_for_panel(
    runtime: DiscordRuntime, state: PanelState, *, tenant_id: uuid.UUID
) -> ResourceOutcome:
    """Authoritatively replace the selected agent's mcp_servers/skills/tools.

    For REMOVALS only. The panel's remove reducers (`remove_mcp_at`,
    `remove_skill_at`) already produce the full desired set — the MCP and its
    `mcp_toolset`, or the skill, dropped from the in-memory spec. Routing that
    through `reconcile_agent` would re-add the removed entry, because reconcile's
    update path unconditionally unions the spec with MA's current state
    (`merge_mcp_servers_with_ma` / `merge_skills_with_ma`). That union exists to
    stop `defaults apply` from clobbering user-attached resources — the exact
    opposite of an explicit user removal. So removals bypass reconcile and send
    the reduced set verbatim; MA's per-field partial update replaces each array,
    and the removed entry is actually gone.

    Skills carry authoring-time identity (`skill_id` == display_title for custom
    skills), so they're resolved to MA ids before sending — same as reconcile.
    Existing metadata is preserved as-is (ownership/stamp unchanged).
    """
    if state.selected is None:
        raise DaimonError("No agent selected; cannot update.")
    spec = state.selected.spec
    ma_agent = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=spec.name
    )
    if ma_agent is None:
        raise DaimonError(f"Agent {spec.name!r} not found on MA; cannot update.")
    resolved_skills = await resolve_refs(
        runtime.anthropic, refs=list(spec.skills), tenant_id=tenant_id
    )
    payload = dump_agent_spec(spec)
    # Removal is authoritative, and MA's update is a per-field PARTIAL merge: a
    # field present in the body is replaced, an ABSENT field is preserved. When
    # the removed entry was the last one, the reduced spec's `mcp_servers` is
    # None and `dump_agent_spec(exclude_none=True)` drops the key — so MA would
    # preserve the old (non-empty) `mcp_servers` while the sent `tools` drops the
    # toolset, leaving an orphaned server → "mcp_servers <name> declared but no
    # mcp_toolset references them" (400). Force both keys present (empty list when
    # emptied) so MA replaces rather than preserves.
    payload["mcp_servers"] = payload.get("mcp_servers") or []
    payload["tools"] = payload.get("tools") or []

    # `payload` and `resolved_skills` are spec-derived; only version and metadata
    # are agent-derived state and must come from `fresh` to avoid resending a
    # stale copy that would undo a concurrent metadata change (#144-2).
    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        return await runtime.anthropic.beta.agents.update(
            fresh.id,
            version=fresh.version,
            **payload,
            skills=resolved_skills,
            metadata=fresh.metadata,  # type: ignore[arg-type]  # Dict[str,str] satisfies Dict[str,str|None]; invariance false positive
        )

    updated = await update_agent_with_version_retry(runtime.anthropic, ma_agent.id, _apply)
    return ResourceOutcome(
        kind="agent", name=spec.name, action=Action.UPDATED, anthropic_id=updated.id
    )


async def create_blank_agent(
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    name: str,
    system: str | None,
    model: str,
    account_id: uuid.UUID,
) -> ResourceOutcome:
    """Build a blank AgentSpec from modal fields and reconcile.

    Tenant-scoped name uniqueness: rejects if `name` already exists anywhere in
    this tenant, regardless of owner. Agent names are tenant-wide identity —
    reconcile dedup and the resolver key on (tenant, name) only.
    """
    collisions = await find_agents_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if collisions:
        raise DaimonError(
            f"This server already has an agent named **{name}**. Pick a different name."
        )
    try:
        spec = AgentSpec.model_validate({"name": name, "model": model, "system": system})
    except ValidationError as err:
        raise DaimonError(f"Spec validation failed: {err}") from err
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    return await reconcile_agent(
        runtime.anthropic,
        spec,
        tenant_id=tenant_id,
        dry_run=False,
        account_id=account_id,
        public_url=public_url,
        # New agents created from the panel are user-owned, NOT seeded
        # resources — managed=True would stamp daimon_managed=true and make
        # them sweep-eligible, so the next defaults apply (every boot/deploy)
        # archives them because they aren't in the seeded spec list. Mirror
        # call_reconcile_for_panel's managed=False.
        managed=False,
    )


async def fork_agent(
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    source_spec: AgentSpec,
    new_name: str,
    account_id: uuid.UUID,
) -> None:
    """Create a new MA agent seeded from `source_spec`'s MA agent.

    Direct `agents.create` — does NOT route through reconcile. Reconcile's
    name-as-identity semantics turn a second fork-with-same-name into a SKIPPED
    no-op (or worse, an UPDATE of the existing copy), which is the opposite of
    what fork means. Mirrors the CLI's `agents fork` path.

    Rejects if `new_name` exists ANYWHERE in this tenant, regardless of owner —
    agent names are tenant-wide identity. Reconcile dedup and the resolver key
    on (tenant, name) only, so a same-name agent from any owner would collide
    at the identity layer.
    """
    collisions = await find_agents_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=new_name
    )
    if collisions:
        raise DaimonError(
            f"This server already has an agent named **{new_name}**. Pick a different name."
        )

    source = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=source_spec.name
    )
    if source is None:
        raise DaimonError(f"Source agent {source_spec.name!r} not found on MA.")
    source_ma = await runtime.anthropic.beta.agents.retrieve(source.id)
    params = source_ma.model_dump(mode="json")
    fork_params: dict[str, object] = {k: params[k] for k in _FORK_COPY_FIELDS if k in params}
    fork_params["name"] = new_name
    fork_params["metadata"] = build_metadata(
        tenant_id=tenant_id, name=new_name, account_id=account_id
    )
    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    fork_params["mcp_servers"] = merge_default_mcp_server(
        fork_params.get("mcp_servers"),  # type: ignore[arg-type]
        public_url,
    )
    fork_params["tools"] = merge_default_mcp_toolset(
        fork_params.get("tools"),  # type: ignore[arg-type]
        public_url,
    )
    # Fork copies raw MA state and bypasses dump_agent_spec — guarantee the
    # base toolset here so forking a legacy pre-guarantee agent doesn't
    # propagate the skills-unusable hole.
    fork_params["tools"] = merge_default_agent_toolset(
        fork_params.get("tools"),  # type: ignore[arg-type]
    )
    created = await runtime.anthropic.beta.agents.create(**fork_params)  # type: ignore[arg-type]

    fork_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(created.id))
    source_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(source.id))
    await _copy_credential_and_repo_binding(
        runtime,
        tenant_id=tenant_id,
        source_agent_uuid=source_agent_uuid,
        fork_agent_uuid=fork_agent_uuid,
    )


async def _copy_credential_and_repo_binding(
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    source_agent_uuid: uuid.UUID,
    fork_agent_uuid: uuid.UUID,
) -> None:
    """Re-key the source's per-agent GitHub credential onto the fork and copy
    its repo binding.

    Generic per-server copy: today the only credential-backed MCP
    server mechanism is the per-agent GitHub PAT overlay driving the repo
    clone at session-create time; this helper is the single place that
    mechanism is re-keyed, so adding a second credential-backed kind later is
    additive here rather than a new github-specific branch in `fork_agent`.

    The fork's credential is written under `principal_id=fork_agent_uuid`
    — never aliased to the source principal — mirroring `store_inline_pat`.
    The repo binding is copied with `ma_secret_ref` rewritten to the
    fork's own `inline-pat:` ref for private repos; `anon:` (and any other
    non-inline-pat ref) is copied verbatim.
    A source binding backed by `inline-pat:` with no resolvable/
    decryptable source credential fails the fork loud.
    """
    async with runtime.sessionmaker() as session:
        source_binding = await get_binding(session, tenant_id=tenant_id, agent_id=source_agent_uuid)
    if source_binding is None:
        return

    if source_binding.ma_secret_ref.startswith("inline-pat:"):
        fernet = _build_runtime_fernet(runtime)
        source_pat = await get_pat(
            principal_id=source_agent_uuid,
            agent_id=source_agent_uuid,
            sessionmaker=runtime.sessionmaker,
            fernet=fernet,
        )
        if source_pat is None:
            raise DaimonError(
                "Fork failed: the source agent's github git-proxy has no resolvable "
                "credential to copy — reconnect GitHub on the source agent and try again."
            )
        await upsert_credential_encrypted(
            sessionmaker=runtime.sessionmaker,
            fernet=fernet,
            principal_id=fork_agent_uuid,
            github_login="(inline-pat)",
            plaintext_token=source_pat,
            scopes=tuple(runtime.settings.github.oauth_scopes),
        )
        async with runtime.sessionmaker.begin() as session:
            await set_agent_github_binding(
                session, agent_id=fork_agent_uuid, principal_id=fork_agent_uuid
            )
        fork_secret_ref = f"inline-pat:{fork_agent_uuid}"
    else:
        # anon: (or any other non-per-agent-secret ref) carries no secret — copy verbatim.
        fork_secret_ref = source_binding.ma_secret_ref

    async with runtime.sessionmaker.begin() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=fork_agent_uuid,
            repo_url=source_binding.repo_url,
            default_branch=source_binding.default_branch,
            ma_secret_ref=fork_secret_ref,
        )


async def delete_agent(runtime: DiscordRuntime, *, tenant_id: uuid.UUID, name: str) -> None:
    """Archive the MA agent matching `name` under the given tenant."""
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise DaimonError(f"No agent named **{name}** found.")
    await runtime.anthropic.beta.agents.archive(agent.id)
    await archive_memory_store_for_agent(
        runtime.anthropic,
        runtime.sessionmaker,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
    )


def _build_runtime_fernet(runtime: DiscordRuntime) -> MultiFernet:
    """Build a MultiFernet from `runtime.settings.crypto.keys`."""
    keys = tuple(secret.get_secret_value() for secret in runtime.settings.crypto.keys)
    return build_multifernet(keys)


async def store_inline_pat(
    runtime: DiscordRuntime,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    plaintext_pat: str,
) -> str:
    """Fernet-encrypt the inline PAT and write a per-agent credential overlay.

    The credential is stored under principal_id=agent_id (per-agent principal),
    and an agent_github_binding(agent_id -> agent_id) overlay row is written so that
    get_pat(agent_id=agent_id) resolves exactly this credential. Connecting GitHub for
    Agent A does not let Agent B resolve the PAT.

    Returns the `ma_secret_ref` string used by `agent_repo_binding.set_binding`.
    """
    fernet = _build_runtime_fernet(runtime)
    # Write the per-agent credential (principal = agent_id) and the overlay binding.
    # After this, get_pat(agent_id=agent_id) resolves exactly this token.
    await upsert_credential_encrypted(
        sessionmaker=runtime.sessionmaker,
        fernet=fernet,
        principal_id=agent_id,
        github_login="(inline-pat)",
        plaintext_token=plaintext_pat,
        scopes=tuple(runtime.settings.github.oauth_scopes),
    )
    async with runtime.sessionmaker.begin() as session:
        await set_agent_github_binding(session, agent_id=agent_id, principal_id=agent_id)
    _log.info("repo_auth.pat_stored", masked=mask_tail(plaintext_pat))
    return f"inline-pat:{agent_id}"


async def kick_off_skill_sync(
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_name: str,
    repo_url: str,
) -> SyncReport:
    """Invoke `sync_agent_skills` for one repo + the selected agent.

    The caller wraps in `asyncio.create_task` to fire-and-forget. Builds a
    fresh `httpx.AsyncClient` (closed when the task completes) so the orchestrator
    can fetch the GitHub tarball.
    """
    fernet = _build_runtime_fernet(runtime)
    repos = [SkillRepo(url=repo_url, branch="main", path="", split=True)]
    async with httpx.AsyncClient() as http_client:
        return await sync_agent_skills(
            principal_id=account_id,
            tenant_id=tenant_id,
            agent_name=agent_name,
            repos=repos,
            sessionmaker=runtime.sessionmaker,
            fernet=fernet,
            http_client=http_client,
            anthropic_client=runtime.anthropic,
        )

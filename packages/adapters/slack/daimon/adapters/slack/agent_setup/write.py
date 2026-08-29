"""Write-path helpers for the /agent-setup panel (Slack adapter).

Ports the Discord agent_setup write + scope_default logic, swapping the
platform-specific runtime type and audit-display helper for their Slack
equivalents. All core saga/store calls are reused UNCHANGED. No cross-adapter
imports (import-linter contract).

GitHub OAuth platform-keying (RESEARCH A3): the state row is keyed to
``platform="slack"`` with a string Slack user ID (e.g. ``"U123456"``). The
callback resolver routes via the ``platform`` column — this prevents
cross-platform state reuse (T-83-09).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import TYPE_CHECKING, Final

import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import Fernet
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core import agent_lifecycle
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_agents_by_daimon_tag,
)
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, build_metadata
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.skills import resolve_refs
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import (
    build_multifernet,
    get_pat,
    upsert_credential_encrypted,
)
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    ScopeRef,
    TenantConfigRow,
    TenantScopeRef,
)
from daimon.core.skill_sync import SyncReport, sync_agent_skills
from daimon.core.specs import (
    AgentSpec,
    SkillRepo,
    dump_agent_spec,
    merge_default_agent_toolset,
)
from daimon.core.stores.agent_github_binding import set_agent_github_binding
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import clear_agent_references, set_fields, unset_fields
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from cryptography.fernet import MultiFernet

_log = structlog.get_logger()

_FORK_COPY_FIELDS: Final = frozenset(
    {"name", "model", "description", "system", "tools", "mcp_servers", "skills", "metadata"}
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def mask_tail(secret: str) -> str:
    """Display-only mask. Never call from a logger that records ``secret`` plain."""
    if len(secret) < 4:
        return "****"
    return f"****{secret[-4:]}"


def owner_repo_from_url(url: str) -> str:
    """Extract canonical ``owner/repo`` from a GitHub URL or short path.

    Must stay byte-identical to
    ``daimon.core.stores.agent_repo_binding._normalize_owner_repo`` — a probe
    run against a differently-canonicalized string would verify a different
    repo than the one the binding actually records.
    """
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


# ---------------------------------------------------------------------------
# Scope propagation (port of Discord scope_default.py, verbatim logic)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PropagateResult:
    """What ``do_propagate`` returns so the caller can render an overwrite display.

    ``prior_agent_name`` and ``prior_actor_account_id`` are the values that
    existed on the row BEFORE the write — both None on a clean propagation,
    populated on an overwrite.
    """

    prior_agent_name: str | None
    prior_actor_account_id: uuid.UUID | None


async def do_propagate(
    session: AsyncSession,
    *,
    scope: ChannelScopeRef | TenantScopeRef,
    tenant_id: uuid.UUID,
    agent_name: str | None = None,
    actor_account_id: uuid.UUID,
) -> PropagateResult:
    """Stamp agent_name at scope (mode='agent', last-write-wins).

    Returns the prior agent_name + actor so the caller can render an
    overwrite line ('replaced X → Y'). Both None on a clean write.
    """
    from daimon.core.errors import StoreError

    if not agent_name:
        raise StoreError("propagate requires agent_name")
    prior_scope_ref: ScopeRef = scope
    prior_row = await get_scope(session, scope=prior_scope_ref)
    prior_agent_name: str | None = None
    prior_actor: uuid.UUID | None = None
    if isinstance(prior_row, (ChannelConfigRow, TenantConfigRow)):
        prior_agent_name = prior_row.agent_name
        prior_actor = prior_row.agent_name_set_by_account_id
    await set_fields(
        session,
        scope=scope,
        tenant_id=tenant_id,
        agent_name=agent_name,
        mode="agent",
        actor_account_id=actor_account_id,
    )
    return PropagateResult(prior_agent_name=prior_agent_name, prior_actor_account_id=prior_actor)


async def do_unpropagate(
    session: AsyncSession,
    *,
    scope: ScopeRef,
    actor_account_id: uuid.UUID,
) -> None:
    """Clear agent_name at scope; the row auto-deletes if it ends fully NULL."""
    await unset_fields(
        session, scope=scope, fields=["agent_name"], actor_account_id=actor_account_id
    )


# ---------------------------------------------------------------------------
# Agent mutation wrappers (port of Discord write.py)
# ---------------------------------------------------------------------------


def _build_runtime_fernet(runtime: SlackRuntime) -> MultiFernet:
    """Build a MultiFernet from ``runtime.settings.crypto.keys``."""
    keys = tuple(secret.get_secret_value() for secret in runtime.settings.crypto.keys)
    return build_multifernet(keys)


def _build_fork_fernet(runtime: SlackRuntime) -> MultiFernet:
    """Build the fernet ``copy_credential_and_repo_binding`` requires, tolerating
    an unconfigured deployment (``settings.crypto.keys == ()``).

    ``copy_credential_and_repo_binding`` only reads/writes through ``fernet``
    when the source has an ``inline-pat:`` binding — which cannot exist in a
    deployment with no crypto keys configured (storing one requires crypto
    too, via ``store_inline_pat``). The anon:/unbound-source paths never touch
    the value, so a throwaway single-use key keeps the primitives-only
    interface satisfied without forcing every fork to require crypto config.
    """
    if not runtime.settings.crypto.keys:
        return build_multifernet((Fernet.generate_key().decode(),))
    return _build_runtime_fernet(runtime)


async def create_blank_agent(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    name: str,
    system: str | None,
    model: str,
    account_id: uuid.UUID,
) -> ResourceOutcome:
    """Build a blank AgentSpec from modal fields and reconcile.

    Tenant-scoped name uniqueness: rejects if ``name`` already exists anywhere
    in this tenant, regardless of owner. Agent names are tenant-wide identity —
    reconcile dedup and the resolver key on (tenant, name) only.
    """
    collisions = await find_agents_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if collisions:
        raise DaimonError(
            f"This workspace already has an agent named *{name}*. Pick a different name."
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
        managed=False,
    )


async def fork_agent(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    source_name: str,
    new_name: str,
    account_id: uuid.UUID,
) -> None:
    """Create a new MA agent seeded from ``source_name``'s MA agent.

    Direct ``agents.create`` — does NOT route through reconcile. Rejects if
    ``new_name`` exists ANYWHERE in this tenant, regardless of owner.
    """
    collisions = await find_agents_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=new_name
    )
    if collisions:
        raise DaimonError(
            f"This workspace already has an agent named *{new_name}*. Pick a different name."
        )
    source = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=source_name
    )
    if source is None:
        raise DaimonError(f"Source agent {source_name!r} not found on MA.")
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
    fork_params["tools"] = merge_default_agent_toolset(
        fork_params.get("tools"),  # type: ignore[arg-type]
    )
    created = await runtime.anthropic.beta.agents.create(**fork_params)  # type: ignore[arg-type]

    fork_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(created.id))
    source_agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(source.id))
    await agent_lifecycle.copy_credential_and_repo_binding(
        anthropic=runtime.anthropic,
        sessionmaker=runtime.sessionmaker,
        fernet=_build_fork_fernet(runtime),
        oauth_scopes=tuple(runtime.settings.github.oauth_scopes),
        tenant_id=tenant_id,
        source_agent_uuid=source_agent_uuid,
        fork_agent_uuid=fork_agent_uuid,
    )


async def delete_agent(runtime: SlackRuntime, *, tenant_id: uuid.UUID, name: str) -> None:
    """Archive the MA agent matching ``name`` under the given tenant.

    Channel and workspace scope rows naming the agent are cleared as part of the
    delete, so turn resolution falls through the cascade instead of resolving to
    a deleted agent.
    """
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise DaimonError(f"No agent named *{name}* found.")
    if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
        # Server-side refusal, and on Slack the ONLY one: RosterEntry carries no
        # managed flag, so the panel offers Delete on a seeded agent exactly as
        # it does on a user agent, and the click branch checks only admin.
        # Archiving here would take the deployment's built-in agent and its
        # memory store with it.
        raise DaimonError(
            f"*{name}* is a built-in agent and cannot be deleted. "
            "Fork it first, then delete the fork."
        )
    await runtime.anthropic.beta.agents.archive(agent.id)
    await agent_lifecycle.archive_memory_store_best_effort(
        anthropic=runtime.anthropic,
        sessionmaker=runtime.sessionmaker,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        log_context={"tenant_id": str(tenant_id), "agent_name": name, "ma_agent_id": agent.id},
    )
    # After the MA archive, never before: a failure here leaves an archived
    # agent with stale scope rows rather than a live agent with cleared ones.
    # Deliberately unguarded — a failure must reach the action's error boundary
    # rather than degrade silently.
    async with runtime.sessionmaker.begin() as session:
        await clear_agent_references(session, tenant_id=tenant_id, agent_name=name)


async def replace_agent_resources_for_panel(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    spec: AgentSpec,
) -> ResourceOutcome:
    """Authoritatively replace the selected agent's mcp_servers/skills/tools.

    For REMOVALS only. Routes around reconcile because reconcile's merge
    semantics would re-add the removed entry.
    """
    ma_agent = await find_agent_by_daimon_tag(
        runtime.anthropic, tenant_id=tenant_id, name=spec.name
    )
    if ma_agent is None:
        raise DaimonError(f"Agent {spec.name!r} not found on MA; cannot update.")
    resolved_skills = await resolve_refs(
        runtime.anthropic, refs=list(spec.skills), tenant_id=tenant_id
    )
    payload = dump_agent_spec(spec)
    payload["mcp_servers"] = payload.get("mcp_servers") or []
    payload["tools"] = payload.get("tools") or []

    async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
        return await runtime.anthropic.beta.agents.update(
            fresh.id,
            version=fresh.version,
            **payload,
            skills=resolved_skills,
            metadata=fresh.metadata,  # type: ignore[arg-type]
        )

    updated = await update_agent_with_version_retry(runtime.anthropic, ma_agent.id, _apply)
    return ResourceOutcome(
        kind="agent", name=spec.name, action=Action.UPDATED, anthropic_id=updated.id
    )


async def call_reconcile_for_panel(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    spec: AgentSpec,
    guild_account_id: uuid.UUID,
) -> ResourceOutcome:
    """Reconcile the currently-selected agent.

    Propagates ``account_id`` (per-user metadata stamp) and ``public_url``
    (default-MCP merge).
    """
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
        account_id=guild_account_id,
        public_url=public_url,
        managed=False,
    )


async def load_agent_inline_pat(runtime: SlackRuntime, *, agent_id: uuid.UUID) -> str | None:
    """Return the inline PAT ``core/sessions.py`` would resolve for ``agent_id``, or None.

    This is the exact credential ``resolve_clone_token``'s ``per_agent_pat``
    short-circuit will use to clone any repo later bound to this agent,
    regardless of which repo that PAT was originally verified against — which
    is why a caller binding a *different* repo must re-verify this value
    against it before writing a binding.

    Returns None (no crypto call at all) when ``runtime.settings.crypto.keys``
    is empty: no inline PAT can exist on a deployment that has never
    configured crypto (storing one requires crypto too, via
    ``store_inline_pat``), and calling ``_build_runtime_fernet`` unconditionally
    here would raise ``ValueError`` on such a deployment, breaking a
    previously-working bind path (same tolerance rationale as
    ``_build_fork_fernet``).

    Passes the service-default opt-in as disabled and no fallback token
    explicitly: the operator's shared service PAT must never be treated as
    this agent's own clone credential — letting it through here would gate
    every re-verification on whether the shared public-read token covers the
    repo, breaking private App-covered binds.
    """
    if not runtime.settings.crypto.keys:
        return None
    return await get_pat(
        principal_id=agent_id,
        agent_id=agent_id,
        sessionmaker=runtime.sessionmaker,
        fernet=_build_runtime_fernet(runtime),
        allow_service_default=False,
        fallback_pat=None,
    )


async def store_inline_pat(
    runtime: SlackRuntime,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    plaintext_pat: str,
) -> str:
    """Fernet-encrypt the inline PAT and write a per-agent credential overlay.

    Stored under principal_id=agent_id (per-agent principal). Connecting
    GitHub for Agent A does not let Agent B resolve the PAT.

    Returns the ``ma_secret_ref`` string used by ``agent_repo_binding.set_binding``.
    """
    fernet = _build_runtime_fernet(runtime)
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
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_name: str,
    repo_url: str,
) -> SyncReport:
    """Invoke ``sync_agent_skills`` for one repo + the selected agent.

    The caller wraps in ``asyncio.create_task`` to fire-and-forget. Builds a
    fresh ``httpx.AsyncClient`` (closed when the task completes).
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

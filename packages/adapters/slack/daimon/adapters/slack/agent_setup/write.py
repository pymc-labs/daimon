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
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.constants import ALLOWED_MODEL_IDS
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_agents_by_daimon_tag,
)
from daimon.core.defaults.mcp_merge import merge_default_mcp_server, merge_default_mcp_toolset
from daimon.core.defaults.metadata import build_metadata
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.defaults.skills import resolve_refs
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import (
    build_multifernet,
    upsert_credential_encrypted,
)
from daimon.core.ma import update_agent_with_version_retry
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
from daimon.core.stores.identity import get_slack_principal_for_account
from daimon.core.stores.scoped_config_read import get_scope, list_propagations_for_tenant
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
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


def validate_model_id(model: str) -> str | None:
    """Return an error message if ``model`` is not in the allow-list; None if valid."""
    if model not in ALLOWED_MODEL_IDS:
        allowed = ", ".join(ALLOWED_MODEL_IDS)
        return f"Model `{model}` is not allowed. Choose one of: {allowed}"
    return None


def mask_tail(secret: str) -> str:
    """Display-only mask. Never call from a logger that records ``secret`` plain."""
    if len(secret) < 4:
        return "****"
    return f"****{secret[-4:]}"


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


async def list_workspace_propagations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
) -> tuple[TenantConfigRow | None, list[ChannelConfigRow]]:
    """Thin adapter-tier wrapper over the core store's cross-tenant scan.

    Exists so panel handlers have a stable adapter-local name; the raw ORM
    query lives behind the store boundary.
    """
    return await list_propagations_for_tenant(session, tenant_id=tenant_id)


async def resolve_account_display(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> str:
    """Canonical attribution-handle resolver for audit display.

    On hit: ``<@{slack_external_id}>`` (renders as a Slack mention).
    On miss: ``account {first8_of_uuid}``. This is the single place that joins
    audit account_id to a display string.
    """
    external_id = await get_slack_principal_for_account(session, account_id=account_id)
    if external_id is not None:
        return f"<@{external_id}>"
    return f"account {str(account_id)[:8]}"


# ---------------------------------------------------------------------------
# Agent mutation wrappers (port of Discord write.py)
# ---------------------------------------------------------------------------


def _build_runtime_fernet(runtime: SlackRuntime) -> MultiFernet:
    """Build a MultiFernet from ``runtime.settings.crypto.keys``."""
    keys = tuple(secret.get_secret_value() for secret in runtime.settings.crypto.keys)
    return build_multifernet(keys)


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
    await runtime.anthropic.beta.agents.create(**fork_params)  # type: ignore[arg-type]


async def delete_agent(runtime: SlackRuntime, *, tenant_id: uuid.UUID, name: str) -> None:
    """Archive the MA agent matching ``name`` under the given tenant."""
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
    if agent is None:
        raise DaimonError(f"No agent named *{name}* found.")
    await runtime.anthropic.beta.agents.archive(agent.id)


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
    (Phase 34 default-MCP merge).
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


async def store_inline_pat(
    runtime: SlackRuntime,
    *,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    plaintext_pat: str,
) -> str:
    """Fernet-encrypt the inline PAT and write a per-agent credential overlay.

    D-25: stored under principal_id=agent_id (per-agent principal). Connecting
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

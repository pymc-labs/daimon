"""resync_bound_repo — webhook-triggered skill resync bridge.

Routes a GitHub push webhook payload to sync_agent_skills for every binding
on the pushed repo:

  push webhook (repo_full_name + ref)
    -> get_bindings_for_repo (install-agnostic, all tenants)
    -> should_resync branch filter (only default_branch)
    -> bridge resolution: binding.agent_id (uuid5) -> agent_name + principal_id
       via re-derive-and-compare across the tenant's MA agents
    -> credential selection (priority order, per-agent isolation):
         App installation token (preferred when App-installed)
         -> per-agent PAT overlay-only (never principal-default)
         -> anon (public repos only)
    -> sync_agent_skills (one-element repos list)
    -> update_last_sync (success + error paths)

Per architecture rule: no module-level singletons; collaborators injected.
Error propagation: per-binding failures (bridge resolution, credential select,
sync, last-sync persist) are caught at the per-binding named boundary in
_resync_one_binding, recorded in last_sync_error, and do NOT crash the batch.
The batch-level setup (binding fetch + owned-client construction in
resync_bound_repo) is intentionally OUTSIDE that boundary: a failure there
surfaces only in logs (Starlette logs the BackgroundTask exception), NOT in any
binding's last_sync_error — there is no specific binding to attribute it to.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

import httpx
import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet  # noqa: I001
from daimon.core.config import GithubSettings
from daimon.core.github_app_auth import build_app_jwt, mint_installation_token
from daimon.core.github_credentials import get_pat
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_sync.orchestrator import sync_agent_skills
from daimon.core.specs import SkillRepo
from daimon.core.stores import agent_repo_binding as binding_store
from daimon.core.stores import github_app_installations as install_store
from daimon.core.stores.domain import AgentRepoBindingRow
from daimon.core.stores.identity import get_or_create_cli_principal
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------

_RESYNC_MAX_ATTEMPTS = 3
_RESYNC_BACKOFF_BASE = 1.0  # seconds (doubles each retry)


def should_resync(ref: str, default_branch: str) -> bool:
    """Return True iff the push ref targets the binding's default branch.

    Args:
        ref: The Git ref from the push webhook (e.g. 'refs/heads/main').
        default_branch: The binding's configured default branch (e.g. 'main').

    Returns:
        True only when ref == 'refs/heads/<default_branch>'.
        Tags and any other branch return False.
    """
    return ref == f"refs/heads/{default_branch}"


# ---------------------------------------------------------------------------
# Bridge resolution helpers
# ---------------------------------------------------------------------------


async def _resolve_agent_name_and_principal(
    *,
    session: AsyncSession,
    binding: AgentRepoBindingRow,
    anthropic_client: AsyncAnthropic,
) -> tuple[str, uuid.UUID] | None:
    """Resolve (agent_name, principal_id) from a binding row.

    Uses the PROVEN-CORRECT re-derive-and-compare bridge (Plan 56-01 OQ1):
    iterate the tenant's MA agents, re-derive uuid5 for each, match the one
    whose derive_agent_uuid(tenant_id, ma_agent.id) == binding.agent_id,
    then read daimon_name from metadata.

    principal_id is resolved via get_or_create_cli_principal (the tenant's
    webhook system account).

    Returns None when the MA agent is not found (logs a warning and skips).
    """
    tenant_id = binding.tenant_id

    # Local import breaks the ma.py <-> defaults circular dependency and routes
    # the listing through the tenant-filtered home (T4: no raw agents.list here).
    from daimon.core.defaults.ma_index import list_agents_by_tenant  # noqa: PLC0415

    resolved_agent_name: str | None = None
    for ma_agent in await list_agents_by_tenant(anthropic_client, tenant_id=tenant_id):
        candidate_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
        if candidate_uuid == binding.agent_id:
            daimon_name = (ma_agent.metadata or {}).get("daimon_name")
            resolved_agent_name = daimon_name or ma_agent.name
            break

    if resolved_agent_name is None:
        _log.warning(
            "github.resync.agent_not_found",
            tenant_id=str(tenant_id),
            agent_id=str(binding.agent_id),
        )
        return None

    principal = await get_or_create_cli_principal(
        session,
        tenant_id=tenant_id,
        os_user="webhook",
    )
    return resolved_agent_name, principal.account_id


# ---------------------------------------------------------------------------
# Credential selection (priority order, per-agent isolation)
# ---------------------------------------------------------------------------


async def _select_credential(
    *,
    repo_full_name: str,
    binding: AgentRepoBindingRow,
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    http_client: httpx.AsyncClient,
    github_settings: GithubSettings | None,
) -> str | None:
    """Select the best available credential per the priority order with per-agent isolation.

    Priority:
    1. App installation token (preferred when an installation exists AND App is configured)
    2. Per-agent PAT overlay ONLY (binding.agent_id keyed; never falls back to principal-default)
    3. None (anon — public repos only)

    Never resolves agent_id=None (principal-default). That is the credential-bleed vector.
    """
    # Tier 1: App installation token
    if github_settings is not None and github_settings.app_id is not None:
        private_key = github_settings.app_private_key
        if private_key is not None:
            installation = await install_store.get_for_repo(session, repo_full_name=repo_full_name)
            if installation is not None:
                jwt = build_app_jwt(
                    private_key.get_secret_value(),
                    github_settings.app_id,
                    now=int(time.time()),
                )
                token = await mint_installation_token(
                    http_client,
                    jwt=jwt,
                    installation_id=installation.installation_id,
                )
                return token

    # Tier 2: per-agent PAT overlay — agent_id given, NEVER agent_id=None
    # get_pat with agent_id resolves the overlay-only path; returns None if no overlay row
    pat = await get_pat(
        principal_id=binding.agent_id,  # per-agent path: principal_id not used when agent_id is set
        agent_id=binding.agent_id,
        sessionmaker=sessionmaker,
        fernet=fernet,
    )
    return pat  # may be None (anon)


# ---------------------------------------------------------------------------
# Resync orchestration shell
# ---------------------------------------------------------------------------


async def resync_bound_repo(
    *,
    repo_full_name: str,
    ref: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    anthropic_client: AsyncAnthropic,
    http_client: httpx.AsyncClient | None = None,
    github_settings: GithubSettings | None = None,
) -> None:
    """Resync skills for all bindings of the pushed repo.

    For each binding on repo_full_name:
      - Branch-filter: skip if ref doesn't target binding.default_branch.
      - Bridge-resolve: binding.agent_id (uuid5) -> agent_name + principal_id.
      - Credential-select: App token > per-agent PAT overlay > anon.
      - sync_agent_skills with a one-element repos list.
      - Persist last_sync_at/last_sync_error regardless of outcome.

    Per-binding errors are caught, recorded, and do NOT crash the batch.
    Logs carry ids + outcomes only — never secrets, tokens, or PATs.

    Background lifecycle note: when called from a Starlette BackgroundTask, the caller
    cannot easily inject a long-lived httpx.AsyncClient that outlives the request.
    Pass http_client=None (the default) to have this function create its own client
    internally with a per-call async context manager. Pass an explicit client only
    from tests that need to inject a mock transport.

    Args:
        repo_full_name: Repository identifier as 'owner/repo' (webhook payload shape).
        ref: Git ref from the push webhook (e.g. 'refs/heads/main').
        sessionmaker: Async sessionmaker (injected; no module-level singleton).
        fernet: MultiFernet for per-agent PAT decryption.
        anthropic_client: Async Anthropic client for MA bridge resolution + sync.
        http_client: Optional injected HTTP client. When None, creates its own
            AsyncClient internally. Callers (e.g. tests) may inject a mock transport.
        github_settings: Optional GitHub App config for installation token minting.
            When None (or partial), App-token tier is skipped.
    """
    async with sessionmaker() as session:
        bindings = await binding_store.get_bindings_for_repo(session, repo_url=repo_full_name)

    if http_client is not None:
        # Caller-owned client (e.g. test injection) — use directly, don't close.
        for binding in bindings:
            if not should_resync(ref, binding.default_branch):
                _log.info(
                    "github.resync.branch_skipped",
                    repo=repo_full_name,
                    ref=ref,
                    default_branch=binding.default_branch,
                    tenant_id=str(binding.tenant_id),
                    agent_id=str(binding.agent_id),
                )
                continue
            await _resync_one_binding(
                binding=binding,
                repo_full_name=repo_full_name,
                sessionmaker=sessionmaker,
                fernet=fernet,
                http_client=http_client,
                anthropic_client=anthropic_client,
                github_settings=github_settings,
            )
    else:
        # Self-owned client — create and close around the full batch.
        async with httpx.AsyncClient(timeout=120.0) as owned_client:
            for binding in bindings:
                if not should_resync(ref, binding.default_branch):
                    _log.info(
                        "github.resync.branch_skipped",
                        repo=repo_full_name,
                        ref=ref,
                        default_branch=binding.default_branch,
                        tenant_id=str(binding.tenant_id),
                        agent_id=str(binding.agent_id),
                    )
                    continue
                await _resync_one_binding(
                    binding=binding,
                    repo_full_name=repo_full_name,
                    sessionmaker=sessionmaker,
                    fernet=fernet,
                    http_client=owned_client,
                    anthropic_client=anthropic_client,
                    github_settings=github_settings,
                )


async def _resync_one_binding(
    *,
    binding: AgentRepoBindingRow,
    repo_full_name: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    http_client: httpx.AsyncClient,
    anthropic_client: AsyncAnthropic,
    github_settings: GithubSettings | None,
) -> None:
    """Attempt to resync a single binding. Records last_sync_at + last_sync_error."""
    now = datetime.now(UTC)
    last_sync_error: str | None = None

    try:
        async with sessionmaker() as session:
            resolved = await _resolve_agent_name_and_principal(
                session=session,
                binding=binding,
                anthropic_client=anthropic_client,
            )
            if resolved is None:
                last_sync_error = "agent not found in MA (bridge resolution failed)"
                return

            agent_name, principal_id = resolved

            credential = await _select_credential(
                repo_full_name=repo_full_name,
                binding=binding,
                session=session,
                sessionmaker=sessionmaker,
                fernet=fernet,
                http_client=http_client,
                github_settings=github_settings,
            )

        # Bounded retry — only transient failures (httpx.TransportError, 5xx)
        for attempt in range(1, _RESYNC_MAX_ATTEMPTS + 1):
            try:
                # WR-01/WR-02: pass the single selected credential as an override so
                # sync_agent_skills does NOT re-resolve a per-agent PAT (which would
                # shadow the App installation token). No transport wrapper / extra
                # client is created — the credential threads cleanly through the param.
                # Thread github_settings.max_tarball_bytes so an operator cap
                # override (including 0-disables) reaches the fetcher on this edge.
                # When github_settings is None, omit the kwarg so the safe
                # sync_agent_skills default (50 MiB) applies.
                if github_settings is not None:
                    await sync_agent_skills(
                        principal_id=principal_id,
                        tenant_id=binding.tenant_id,
                        agent_name=agent_name,
                        repos=[SkillRepo(url=repo_full_name, branch=binding.default_branch)],
                        sessionmaker=sessionmaker,
                        fernet=fernet,
                        http_client=http_client,
                        anthropic_client=anthropic_client,
                        credential_override=credential,
                        max_tarball_bytes=github_settings.max_tarball_bytes,
                    )
                else:
                    await sync_agent_skills(
                        principal_id=principal_id,
                        tenant_id=binding.tenant_id,
                        agent_name=agent_name,
                        repos=[SkillRepo(url=repo_full_name, branch=binding.default_branch)],
                        sessionmaker=sessionmaker,
                        fernet=fernet,
                        http_client=http_client,
                        anthropic_client=anthropic_client,
                        credential_override=credential,
                    )
                _log.info(
                    "github.resync.success",
                    repo=repo_full_name,
                    tenant_id=str(binding.tenant_id),
                    agent_id=str(binding.agent_id),
                    agent_name=agent_name,
                )
                return  # success path — finally block persists last_sync_error=None

            except (httpx.TransportError, httpx.HTTPStatusError) as err:
                response = getattr(err, "response", None)
                is_5xx = (
                    response is not None
                    and hasattr(response, "status_code")
                    and response.status_code >= 500
                )
                is_transient = isinstance(err, httpx.TransportError) or is_5xx
                if not is_transient or attempt >= _RESYNC_MAX_ATTEMPTS:
                    raise
                backoff = _RESYNC_BACKOFF_BASE * (2 ** (attempt - 1))
                _log.warning(
                    "github.resync.retry",
                    repo=repo_full_name,
                    agent_id=str(binding.agent_id),
                    attempt=attempt,
                    backoff=backoff,
                )
                await asyncio.sleep(backoff)

    except Exception as err:  # noqa: BLE001 — named boundary; per-binding failures captured
        last_sync_error = str(err)
        _log.warning(
            "github.resync.failed",
            repo=repo_full_name,
            tenant_id=str(binding.tenant_id),
            agent_id=str(binding.agent_id),
            error=last_sync_error,
        )

    finally:
        try:
            async with sessionmaker.begin() as persist_session:
                await binding_store.update_last_sync(
                    persist_session,
                    tenant_id=binding.tenant_id,
                    agent_id=binding.agent_id,
                    last_sync_at=now,
                    last_sync_error=last_sync_error,
                )
        except Exception as persist_err:
            _log.error(
                "github.resync.persist_failed",
                repo=repo_full_name,
                agent_id=str(binding.agent_id),
                error=str(persist_err),
            )

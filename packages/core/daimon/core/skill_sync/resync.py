"""resync_bound_repo — webhook-triggered skill resync bridge.

Routes a GitHub push webhook payload to sync_agent_skills for every binding
on the pushed repo:

  push webhook (repo_full_name + ref)
    -> get_bindings_for_repo (install-agnostic, all tenants)
    -> should_resync branch filter (only default_branch)
    -> bridge resolution: binding.agent_id (uuid5) -> agent_name + principal_id
       via re-derive-and-compare across the tenant's MA agents
    -> credential selection: the SAME precedence table the interactive clone
       path uses (github_repo_auth.select_clone_auth) — a per-agent PAT
       overlay always wins, an App installation token requires the binding
       recorded proof of access, the operator fallback PAT requires
       specifically a verified-public proof, otherwise the binding is
       unauthorized. The decision itself lives in one shared pure function;
       this module only resolves its inputs (per-agent PAT overlay, the
       cached installation lookup, the recorded proof) and maps its mode
       back onto a token.
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
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.config import GithubSettings
from daimon.core.errors import DaimonError
from daimon.core.github_app_auth import build_app_jwt, mint_installation_token
from daimon.core.github_credentials import get_pat
from daimon.core.github_repo_auth import select_clone_auth
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_sync.fetcher import GitHubRateLimitError
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


@dataclass(frozen=True)
class ResyncReport:
    """Binding errors and retryable bindings from one repository pass."""

    failed_bindings: int
    retryable_bindings: int = 0
    retry_after: datetime | None = None


@dataclass(frozen=True)
class _BindingOutcome:
    failed: bool
    retryable: bool
    retry_after: datetime | None = None


def _is_retryable_error(err: Exception) -> bool:
    """Classify transient GitHub, MA, and transport failures for queue retry."""
    if isinstance(
        err,
        (GitHubRateLimitError, httpx.TransportError, anthropic.APIConnectionError, TimeoutError),
    ):
        return True
    if isinstance(err, httpx.HTTPStatusError):
        status_code = err.response.status_code
        return status_code == 429 or status_code >= 500
    if isinstance(err, anthropic.APIStatusError):
        return err.status_code == 429 or err.status_code >= 500
    return False


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
) -> tuple[str, uuid.UUID, str] | None:
    """Resolve (agent_name, principal_id, MA agent ID) from a binding row.

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
    from daimon.core.defaults.ma_index import list_agents_by_tenant

    tenant_agents = await list_agents_by_tenant(anthropic_client, tenant_id=tenant_id)
    resolved_agent_name: str | None = None
    resolved_ma_agent_id: str | None = None
    for ma_agent in tenant_agents:
        candidate_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
        if candidate_uuid == binding.agent_id:
            daimon_name = (ma_agent.metadata or {}).get("daimon_name")
            resolved_agent_name = daimon_name or ma_agent.name
            resolved_ma_agent_id = str(ma_agent.id)
            break

    if resolved_agent_name is None:
        _log.warning(
            "github.resync.agent_not_found",
            tenant_id=str(tenant_id),
            agent_id=str(binding.agent_id),
        )
        return None

    duplicate_ids = [
        str(agent.id)
        for agent in tenant_agents
        if ((agent.metadata or {}).get("daimon_name") or agent.name) == resolved_agent_name
        and str(agent.id) != resolved_ma_agent_id
    ]
    if duplicate_ids:
        raise DaimonError(
            f"multiple MA agents share tenant {tenant_id} and name {resolved_agent_name!r}; "
            "archive duplicates before resyncing this binding"
        )

    principal = await get_or_create_cli_principal(
        session,
        tenant_id=tenant_id,
        os_user="webhook",
    )
    assert resolved_ma_agent_id is not None
    return resolved_agent_name, principal.account_id, resolved_ma_agent_id


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
    """Select the credential for this binding via the shared precedence table.

    Delegates the precedence DECISION to select_clone_auth — the same pure
    function the interactive clone path (resolve_clone_token) calls — so the
    two paths cannot drift again: a per-agent PAT overlay always wins, an App
    installation token requires the binding recorded proof of access, and
    the operator fallback PAT requires specifically a verified-public proof.

    Only the App-installed lookup stays distinct from the clone path: this
    reads the cached github_app_installations table rather than a live
    GitHub call, because the unattended batch wants the cheap read and once
    proof gates the tier, live-vs-cached is a freshness tradeoff, not a
    correctness one.

    A per-agent token now taking precedence over an App installation token
    (rather than the reverse) matches the interactive path. The earlier
    concern this reverses was that sync_agent_skills re-resolved a per-agent
    PAT internally and shadowed the App token; that is prevented
    structurally by credential_override below, which is still passed and
    still the single authority, so exactly one credential reaches the fetch
    either way.

    The operator fallback is no longer reachable for a binding that merely
    has no per-agent credential — it now requires a verified-public proof.
    This also closes the case where a binding whose stored credential was
    deleted quietly fell through to the shared operator token.

    Returns None only for the legitimate anonymous-fetch case: a binding
    that recorded a verified-public proof on a deployment with no fallback
    token configured. This caller's contract allows None for an
    unauthenticated fetch, and refusing that case would break public skill
    sync on any deployment that never configured a fallback PAT.

    Raises DaimonError for every other case where no credential is
    authorized (including a binding with no recorded proof at all), so the
    existing per-binding boundary in _resync_one_binding records the reason
    in last_sync_error and the batch continues onto the next binding. A
    silent anonymous fetch is exactly the degradation mode this gate exists
    to remove, so this never returns None for an unauthorized binding.
    """
    per_agent_pat = await get_pat(
        principal_id=binding.agent_id,  # per-agent path: principal_id not used when agent_id is set
        agent_id=binding.agent_id,
        sessionmaker=sessionmaker,
        fernet=fernet,
        allow_service_default=False,
        fallback_pat=None,
    )
    fallback_pat = (
        github_settings.fallback_pat.get_secret_value()
        if github_settings is not None and github_settings.fallback_pat is not None
        else None
    )
    installation = None
    if (
        github_settings is not None
        and github_settings.app_id is not None
        and github_settings.app_private_key is not None
    ):
        installation = await install_store.get_for_repo(session, repo_full_name=repo_full_name)

    mode = select_clone_auth(
        has_per_agent_pat=per_agent_pat is not None,
        app_installed=installation is not None,
        proof_kind=binding.proof_kind,
        has_fallback_pat=fallback_pat is not None,
    )

    if mode == "pat":
        return per_agent_pat
    if mode == "app":
        assert (
            github_settings is not None
            and github_settings.app_id is not None
            and github_settings.app_private_key is not None
        )
        assert installation is not None  # narrows: app_installed implies a cached row
        jwt = build_app_jwt(
            github_settings.app_private_key.get_secret_value(),
            github_settings.app_id,
            now=int(time.time()),
        )
        # Narrowed to the pushed repo, read-only: resync only fetches it.
        return await mint_installation_token(
            http_client,
            jwt=jwt,
            installation_id=installation.installation_id,
            repository=repo_full_name.split("/", 1)[1],
            permissions={"contents": "read"},
        )
    if mode == "public":
        assert fallback_pat is not None  # narrows: has_fallback_pat implies this is set
        return fallback_pat

    # mode == "none". A verified-public binding with no fallback PAT configured
    # is the legitimate anonymous case (see docstring) — anything else is a
    # binding no credential is authorized for.
    if binding.proof_kind == "public":
        return None
    raise DaimonError(
        f"No credential is authorized to sync {repo_full_name} for this binding — "
        "it has no recorded proof of access."
    )


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
) -> ResyncReport:
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

    async def _resync_all(
        client: httpx.AsyncClient,
    ) -> tuple[int, int, datetime | None]:
        # Shared per-binding loop body (D-01): http_client is the only input
        # that varies between the caller-owned and self-owned client paths.
        failed_bindings = 0
        retryable_bindings = 0
        retry_after: datetime | None = None
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
            outcome = await _resync_one_binding(
                binding=binding,
                repo_full_name=repo_full_name,
                sessionmaker=sessionmaker,
                fernet=fernet,
                http_client=client,
                anthropic_client=anthropic_client,
                github_settings=github_settings,
            )
            if outcome.failed:
                failed_bindings += 1
            if outcome.retryable:
                retryable_bindings += 1
            if outcome.retry_after is not None:
                retry_after = (
                    max(retry_after, outcome.retry_after) if retry_after else outcome.retry_after
                )
                # A rate limit may be shared across credentials. Stop this batch
                # and let the durable queue retry every remaining binding later.
                break
        return failed_bindings, retryable_bindings, retry_after

    if http_client is not None:
        # Caller-owned client (e.g. test injection) — use directly, don't close.
        failed_bindings, retryable_bindings, retry_after = await _resync_all(http_client)
    else:
        # Self-owned client — create and close around the full batch.
        async with httpx.AsyncClient(timeout=120.0) as owned_client:
            failed_bindings, retryable_bindings, retry_after = await _resync_all(owned_client)
    return ResyncReport(
        failed_bindings=failed_bindings,
        retryable_bindings=retryable_bindings,
        retry_after=retry_after,
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
) -> _BindingOutcome:
    """Attempt to resync a single binding. Records last_sync_at + last_sync_error."""
    now = datetime.now(UTC)
    last_sync_error: str | None = None
    failed = False
    retryable = False
    retry_after: datetime | None = None

    try:
        async with sessionmaker() as session:
            resolved = await _resolve_agent_name_and_principal(
                session=session,
                binding=binding,
                anthropic_client=anthropic_client,
            )
            if resolved is None:
                raise DaimonError("agent not found in MA (bridge resolution failed)")
            agent_name, principal_id, ma_agent_id = resolved
            credential = await _select_credential(
                repo_full_name=repo_full_name,
                binding=binding,
                session=session,
                sessionmaker=sessionmaker,
                fernet=fernet,
                http_client=http_client,
                github_settings=github_settings,
            )

        # Retry transient network/provider errors a few times before the
        # durable queue applies its longer backoff.
        for attempt in range(1, _RESYNC_MAX_ATTEMPTS + 1):
            try:
                # Pass the single selected credential as an override so
                # sync_agent_skills does not re-resolve and shadow an App token.
                # Thread operator tarball caps through this edge; when settings
                # are absent, the safe defaults remain in force.
                if github_settings is not None:
                    report = await sync_agent_skills(
                        principal_id=principal_id,
                        tenant_id=binding.tenant_id,
                        agent_name=agent_name,
                        target_ma_agent_id=ma_agent_id,
                        repos=[SkillRepo(url=repo_full_name, branch=binding.default_branch)],
                        sessionmaker=sessionmaker,
                        fernet=fernet,
                        http_client=http_client,
                        anthropic_client=anthropic_client,
                        credential_override=credential,
                        max_tarball_bytes=github_settings.max_tarball_bytes,
                        max_tarball_decompressed_bytes=github_settings.max_tarball_decompressed_bytes,
                    )
                else:
                    report = await sync_agent_skills(
                        principal_id=principal_id,
                        tenant_id=binding.tenant_id,
                        agent_name=agent_name,
                        target_ma_agent_id=ma_agent_id,
                        repos=[SkillRepo(url=repo_full_name, branch=binding.default_branch)],
                        sessionmaker=sessionmaker,
                        fernet=fernet,
                        http_client=http_client,
                        anthropic_client=anthropic_client,
                        credential_override=credential,
                    )
                failures = [
                    *(f"{repo}: {reason}" for repo, reason in report.skipped_repos),
                    *(f"{name}: {reason}" for name, reason in report.failed_uploads),
                    *(
                        f"{name}: attach failed: {reason}"
                        for name, reason in report.attach_failures
                    ),
                ]
                if failures:
                    failed = True
                    retryable = report.retryable_failure
                    last_sync_error = "; ".join(failures)
                    _log.warning(
                        "github.resync.partial_failure",
                        repo=repo_full_name,
                        tenant_id=str(binding.tenant_id),
                        agent_id=str(binding.agent_id),
                        agent_name=agent_name,
                        failure_count=len(failures),
                        retryable=retryable,
                    )
                else:
                    _log.info(
                        "github.resync.success",
                        repo=repo_full_name,
                        tenant_id=str(binding.tenant_id),
                        agent_id=str(binding.agent_id),
                        agent_name=agent_name,
                    )
                break
            except Exception as err:
                if isinstance(err, GitHubRateLimitError):
                    raise
                if not _is_retryable_error(err) or attempt >= _RESYNC_MAX_ATTEMPTS:
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

    except asyncio.CancelledError:
        # Cancellation bypasses Exception but still runs finally; preserve an
        # observable failure rather than clearing an earlier error as success.
        last_sync_error = "resync cancelled"
        raise
    except Exception as err:  # named boundary; per-binding failures captured
        failed = True
        retryable = _is_retryable_error(err)
        if isinstance(err, GitHubRateLimitError):
            retry_after = err.retry_after
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
            failed = True
            retryable = True
            _log.error(
                "github.resync.persist_failed",
                repo=repo_full_name,
                agent_id=str(binding.agent_id),
                error=str(persist_err),
            )
    return _BindingOutcome(failed=failed, retryable=retryable, retry_after=retry_after)

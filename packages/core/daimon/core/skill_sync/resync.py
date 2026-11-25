"""Resync imported skills on GitHub pushes using active App grants."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.agent_editing import AgentEditDeps
from daimon.core.config import GithubAppSettings, GithubSettings
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.github_rate_limit import GitHubRateLimitError
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.skill_repos import resync_repo_mirrors
from daimon.core.skill_sync.orchestrator import sync_agent_skills
from daimon.core.specs import SkillRepo
from daimon.core.stores.github_access import active_grant_for_repo
from daimon.core.stores.user_skills import list_repo_skill_sources
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ResyncReport:
    failed_bindings: int
    retryable_bindings: int = 0
    retry_after: datetime | None = None


def should_resync(ref: str, default_branch: str) -> bool:
    """A push syncs the branch from which skills were imported."""
    return ref == f"refs/heads/{default_branch}"


def _retryable(error: Exception) -> bool:
    if isinstance(
        error,
        (GitHubRateLimitError, httpx.TransportError, anthropic.APIConnectionError, TimeoutError),
    ):
        return True
    if isinstance(error, (httpx.HTTPStatusError, anthropic.APIStatusError)):
        status = (
            error.response.status_code
            if isinstance(error, httpx.HTTPStatusError)
            else error.status_code
        )
        return status == 429 or status >= 500
    return False


async def _resync_agent_repos(
    *,
    repo_full_name: str,
    ref: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    anthropic_client: AsyncAnthropic,
    http_client: httpx.AsyncClient | None = None,
    github_settings: GithubSettings | None = None,
    github_app: GithubAppSettings | None = None,
) -> ResyncReport:
    """Resync each imported repo source whose agent still has an App grant."""
    if github_app is None or github_app.app_id is None or github_app.private_key is None:
        return ResyncReport(failed_bindings=0)
    async with sessionmaker() as session:
        sources = await list_repo_skill_sources(session)
    failed = retryable = 0

    async def run(client: httpx.AsyncClient) -> None:
        nonlocal failed, retryable
        for tenant_id, principal_id, agent_name, source_url, branch, path in sources:
            if normalize_owner_repo(
                source_url
            ).casefold() != repo_full_name.casefold() or not should_resync(ref, branch):
                continue
            agent_id: uuid.UUID | None = None
            try:
                agents = await list_agents_by_tenant(anthropic_client, tenant_id=tenant_id)
                agent = next(
                    (
                        item
                        for item in agents
                        if (item.metadata or {}).get("daimon_name", item.name) == agent_name
                    ),
                    None,
                )
                if agent is None:
                    continue
                agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
                async with sessionmaker() as session:
                    grant = await active_grant_for_repo(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_name=repo_full_name,
                    )
                if grant is None:
                    continue
                report = await sync_agent_skills(
                    principal_id=principal_id,
                    tenant_id=tenant_id,
                    agent_name=agent_name,
                    target_ma_agent_id=str(agent.id),
                    repos=[SkillRepo(url=source_url, branch=branch, path=path)],
                    sessionmaker=sessionmaker,
                    fernet=fernet,
                    http_client=client,
                    anthropic_client=anthropic_client,
                    app_id=github_app.app_id,
                    app_private_key=github_app.private_key,
                    max_tarball_bytes=github_settings.max_tarball_bytes
                    if github_settings
                    else 50 * 1024 * 1024,
                    max_tarball_decompressed_bytes=github_settings.max_tarball_decompressed_bytes
                    if github_settings
                    else 200 * 1024 * 1024,
                )
                if report.skipped_repos or report.failed_uploads or report.attach_failures:
                    failed += 1
                    retryable += int(report.retryable_failure)
            except Exception as error:
                failed += 1
                retryable += int(_retryable(error))
                _log.warning(
                    "github.resync.failed",
                    repo=repo_full_name,
                    tenant_id=str(tenant_id),
                    agent_id=str(agent_id),
                    error_type=type(error).__name__,
                )

    if http_client is None:
        async with httpx.AsyncClient(timeout=120.0) as client:
            await run(client)
    else:
        await run(http_client)
    return ResyncReport(failed_bindings=failed, retryable_bindings=retryable)


async def resync_bound_repo(
    *,
    repo_full_name: str,
    ref: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    anthropic_client: AsyncAnthropic,
    http_client: httpx.AsyncClient | None = None,
    github_settings: GithubSettings | None = None,
    github_app: GithubAppSettings | None = None,
) -> ResyncReport:
    """Resync a pushed repository: agents' imported skills, then the library's repo mirrors."""
    agents = await _resync_agent_repos(
        repo_full_name=repo_full_name,
        ref=ref,
        sessionmaker=sessionmaker,
        fernet=fernet,
        anthropic_client=anthropic_client,
        http_client=http_client,
        github_settings=github_settings,
        github_app=github_app,
    )

    async def mirrors(client: httpx.AsyncClient) -> tuple[int, int]:
        return await resync_repo_mirrors(
            repo_full_name=repo_full_name,
            ref=ref,
            sessionmaker=sessionmaker,
            edit=AgentEditDeps(
                client=anthropic_client, session_factory=sessionmaker, public_url=None
            ),
            http=client,
            app_id=None if github_app is None else github_app.app_id,
            app_private_key=None if github_app is None else github_app.private_key,
            now=lambda: datetime.now(UTC),
        )

    if http_client is None:
        async with httpx.AsyncClient(timeout=120.0) as client:
            failed, retryable = await mirrors(client)
    else:
        failed, retryable = await mirrors(http_client)
    return ResyncReport(
        failed_bindings=agents.failed_bindings + failed,
        retryable_bindings=agents.retryable_bindings + retryable,
        retry_after=agents.retry_after,
    )

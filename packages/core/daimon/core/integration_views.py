"""How a workspace's integrations stand, for the admin pages: the chat install, GitHub, MCP.

Server admins only (`authorize(MANAGE_INTEGRATIONS)`). Every view here is read only and
carries no credential: a token, a refresh token and an encrypted column are never read,
only whether one exists and, for a sign-in, when it lapses.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from anthropic.types.beta.vaults.beta_managed_agents_mcp_oauth_auth_response import (
    BetaManagedAgentsMCPOAuthAuthResponse,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.errors import UserFacingError
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.mux_backend import platform_scope
from daimon.core.mux_compat import list_credentials, list_vaults
from daimon.core.stores import github_access_requests as requests_store
from daimon.core.stores import integration_reads as reads
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores import slack_bot_tokens as slack_store
from daimon.core.stores import teams_installations as teams_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores.github_access_requests import AccessRequest
from daimon.core.stores.integration_reads import (
    GrantedRepoRow,
    InstallationUseRow,
    SyncTroubleRow,
)
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

__all__ = [
    "AgentRepos",
    "AgentServers",
    "ExpiredSignIn",
    "GithubOverview",
    "IntegrationsRefused",
    "McpAgentUse",
    "McpServerUse",
    "InstallStatus",
    "TeamInstall",
    "WorkspaceInstall",
    "check_mcp_expiry",
    "load_github_overview",
    "load_mcp_catalogue",
    "load_workspace_install",
]

_OPEN: Final = TenantAccessPolicy()
#: Vaults read at once when checking sign-ins; the provider is asked one list per vault.
_VAULT_READS: Final = 4
#: Sign-ins checked in one pass: a pass lists one vault per (person, agent) pair.
MAX_VAULTS_CHECKED: Final = 200


class IntegrationsRefused(UserFacingError):
    """The caller may not see or change integrations."""


def require_admin(subject: Subject) -> None:
    if not authorize(_OPEN, subject=subject, action=Action.MANAGE_INTEGRATIONS):
        raise IntegrationsRefused("Ask an admin or an owner.")


@dataclass(frozen=True)
class TeamInstall:
    id: str
    name: str | None


InstallStatus = Literal["connected", "pending", "failed", "archived", "disconnected"]


@dataclass(frozen=True)
class WorkspaceInstall:
    """The chat workspace this one lives in, and whether Daimon is still installed there."""

    platform: str
    name: str
    status: InstallStatus
    problem: bool
    teams: tuple[TeamInstall, ...] = ()


async def load_workspace_install(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    workspace_name: str,
    now: dt.datetime,
) -> WorkspaceInstall | None:
    """None when the workspace has no chat install (a web or CLI one)."""
    require_admin(subject)
    tenant = await tenants_store.get_tenant(session, tenant_id)
    if tenant is None or tenant.platform not in ("discord", "slack", "teams"):
        return None
    status: InstallStatus = "connected"
    if tenant.archived_at is not None:
        status = "archived"
    elif tenant.provision_status in ("pending", "failed"):
        status = "pending" if tenant.provision_status == "pending" else "failed"
    teams: tuple[TeamInstall, ...] = ()
    if tenant.platform == "slack":
        token = await slack_store.get_slack_bot_token(session, team_id=tenant.external_id)
        lapsed = (
            token is not None
            and token.expires_at is not None
            and token.expires_at <= now
            and token.refresh_token is None
        )
        if token is None or lapsed:
            status = "disconnected"
    elif tenant.platform == "teams":
        installed = await teams_store.list_teams_installations(session, tenant_id=tenant_id)
        teams = tuple(TeamInstall(id=t.team_id, name=t.name) for t in installed)
    return WorkspaceInstall(
        platform=tenant.platform,
        name=workspace_name,
        status=status,
        problem=tenant.last_reconcile_error is not None,
        teams=teams,
    )


@dataclass(frozen=True)
class AgentRepos:
    agent_id: uuid.UUID
    repos: tuple[GrantedRepoRow, ...]


@dataclass(frozen=True)
class GithubOverview:
    installations: tuple[InstallationUseRow, ...]
    agents: tuple[AgentRepos, ...]
    requests: tuple[AccessRequest, ...]
    trouble: tuple[SyncTroubleRow, ...]


async def load_github_overview(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    agent_ids: Collection[uuid.UUID],
    now: dt.datetime,
) -> GithubOverview:
    """Installations, each agent's repos, requests waiting on an admin, and failed syncs."""
    require_admin(subject)
    granted = await reads.list_granted_repos(session, tenant_id=tenant_id)
    by_agent: dict[uuid.UUID, list[GrantedRepoRow]] = {}
    for row in granted:
        by_agent.setdefault(row.agent_id, []).append(row)
    waiting = await requests_store.list_waiting(
        session, tenant_id=tenant_id, visible_agent_ids=frozenset(agent_ids), now=now
    )
    return GithubOverview(
        installations=tuple(await reads.installations_in_use(session, tenant_id=tenant_id)),
        agents=tuple(
            AgentRepos(agent_id=agent_id, repos=tuple(by_agent.get(agent_id, ())))
            for agent_id in agent_ids
        ),
        requests=tuple(waiting),
        trouble=tuple(await reads.list_sync_trouble(session, tenant_id=tenant_id)),
    )


@dataclass(frozen=True)
class AgentServers:
    """One agent and the MCP servers it is set up with."""

    agent_id: uuid.UUID
    name: str
    servers: tuple[tuple[str, str], ...]
    """`(server name, url)` pairs."""


@dataclass(frozen=True)
class McpAgentUse:
    agent_id: uuid.UUID
    name: str
    shared_token: bool
    signed_in: int


@dataclass(frozen=True)
class McpServerUse:
    url: str
    name: str
    agents: tuple[McpAgentUse, ...] = field(default=())

    @property
    def signed_in(self) -> int:
        return sum(a.signed_in for a in self.agents)


async def load_mcp_catalogue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    agents: Sequence[AgentServers],
) -> list[McpServerUse]:
    """Each server any agent uses, the agents that use it, and how each is signed in to."""
    require_admin(subject)
    shared = {
        (c.agent_id, canonical_mcp_url(c.server_url))
        for c in await reads.list_shared_credentials(
            session, tenant_id=tenant_id, agent_ids=[a.agent_id for a in agents]
        )
    }
    counts = await reads.sign_in_counts(session, tenant_id=tenant_id)
    names: dict[str, str] = {}
    users: dict[str, list[McpAgentUse]] = {}
    for agent in agents:
        for name, url in agent.servers:
            key = canonical_mcp_url(url)
            names.setdefault(key, name)
            users.setdefault(key, []).append(
                McpAgentUse(
                    agent_id=agent.agent_id,
                    name=agent.name,
                    shared_token=(agent.agent_id, key) in shared,
                    signed_in=counts.get((agent.agent_id, key), 0),
                )
            )
    return sorted(
        (McpServerUse(url=url, name=names[url], agents=tuple(used)) for url, used in users.items()),
        key=lambda s: s.name.casefold(),
    )


@dataclass(frozen=True)
class ExpiredSignIn:
    """A person's sign-in the server can no longer use, and nothing will renew."""

    agent_id: uuid.UUID
    account_id: uuid.UUID
    server_url: str


async def check_mcp_expiry(
    client: AsyncAnthropic,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    server_urls: Collection[str],
    now: dt.datetime,
) -> tuple[list[ExpiredSignIn], bool]:
    """People's finished sign-ins whose stored grant has lapsed with no refresh behind it.

    Reads each person's vault for the agent, so it is slower than the catalogue and is asked
    for on its own. The second value is False when more sign-ins exist than one pass reads
    or a vault could not be read; the list then holds only what was seen.
    """
    require_admin(subject)
    grants = await flows_store.list_completed_grants(
        session, tenant_id=tenant_id, server_urls=server_urls
    )
    pairs = sorted({(g.account_id, g.agent_id) for g in grants}, key=str)
    complete = len(pairs) <= MAX_VAULTS_CHECKED
    pairs = pairs[:MAX_VAULTS_CHECKED]
    if not pairs:
        return [], True
    scope = platform_scope("integration_views.check_mcp_expiry")
    wanted = {f"daimon-mcp:{account}:{agent}" for account, agent in pairs}
    vaults: dict[str, BetaManagedAgentsVault] = {}
    try:
        async for vault in list_vaults(client, scope=scope):
            if vault.archived_at is None and vault.display_name in wanted:
                oldest = vaults.get(vault.display_name)
                if oldest is None or vault.created_at < oldest.created_at:
                    vaults[vault.display_name] = vault
    except APIError as exc:
        log.warning("integrations.vault_list_failed", error_type=type(exc).__name__)
        return [], False

    by_pair: dict[tuple[uuid.UUID, uuid.UUID], set[str]] = {}
    for grant in grants:
        by_pair.setdefault((grant.account_id, grant.agent_id), set()).add(
            canonical_mcp_url(grant.mcp_server_url)
        )
    gate = asyncio.Semaphore(_VAULT_READS)
    expired: list[ExpiredSignIn] = []
    seen_all = True

    async def read(account_id: uuid.UUID, agent_id: uuid.UUID) -> None:
        nonlocal seen_all
        vault = vaults.get(f"daimon-mcp:{account_id}:{agent_id}")
        if vault is None:
            return
        vault_id = vault.id
        urls = by_pair[(account_id, agent_id)]
        async with gate:
            try:
                async for credential in list_credentials(client, vault_id, scope=scope):
                    auth = credential.auth
                    if not isinstance(auth, BetaManagedAgentsMCPOAuthAuthResponse):
                        continue
                    url = canonical_mcp_url(auth.mcp_server_url)
                    if (
                        url in urls
                        and auth.expires_at is not None
                        and auth.expires_at <= now
                        and auth.refresh is None
                    ):
                        expired.append(
                            ExpiredSignIn(agent_id=agent_id, account_id=account_id, server_url=url)
                        )
            except APIError as exc:
                seen_all = False
                log.warning("integrations.vault_read_failed", error_type=type(exc).__name__)

    await asyncio.gather(*(read(account, agent) for account, agent in pairs))
    return expired, complete and seen_all

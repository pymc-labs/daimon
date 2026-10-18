"""Agent-scoped GitHub App credentials for one Managed Agents session."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta.session_create_params import Resource
from anthropic.types.beta.vaults.beta_managed_agents_environment_variable_auth_response import (
    BetaManagedAgentsEnvironmentVariableAuthResponse,
)
from anthropic.types.beta.vaults.beta_managed_agents_static_bearer_auth_response import (
    BetaManagedAgentsStaticBearerAuthResponse,
)
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_auth import (
    PERMISSION_PROFILES,
    BotIdentity,
    PermissionProfile,
    build_app_jwt,
    get_bot_identity,
    group_repository_access,
    mint_installation_token,
)
from daimon.core.github_requester_access import PermissionCache
from daimon.core.mcp_vault import (
    GITHUB_COPILOT_MCP_URL,
    add_github_copilot_credential,
    chat_mcp_credential,
)
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import (
    archive_session,
    archive_vault,
    create_vault,
    delete_credential,
    list_credentials,
    rotate_session_repo_token,
    store_credential,
    update_credential,
)
from daimon.core.session_snapshot import SessionSnapshot, TokenScope, token_scope_parts
from daimon.core.stores.github_access import (
    AgentGrant,
    AuthorizedRepo,
    list_agent_grants,
    list_authorized_repos,
    list_live_grant_repositories,
    lock_agent_grants,
)
from daimon.core.stores.github_app_installations import get as get_installation
from daimon.core.stores.github_installation_reconciliation import enqueue as enqueue_reconciliation
from daimon.core.stores.github_issued_tokens import (
    GitHubTokenRowClosedError,
    create_pending,
    decrypt_issued_token,
    finish_headless_app_session,
    keep_failed_rotation_tokens,
    list_session_tokens,
    mark_delivered,
    mark_headless_app_session_closed,
    mark_revoked,
    mark_session_tokens_superseded,
    restore_session_tokens,
    select_stale_tokens,
    set_session_id,
    store_token,
)
from daimon.core.stores.security_audit import append_github_token_event
from daimon.core.stores.thread_sessions import (
    live_effective_config_for_ma_session,
    record_app_token_refresh,
)
from daimon.core.turn_origin import current_origin_id
from mux.contracts.ids import Scope
from mux.contracts.resources import WorkspaceSource
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = logging.getLogger(__name__)
MAX_INSTALLATIONS_PER_AGENT = 12
MAX_SESSION_VAULT_CREDENTIALS = 15


def installation_credential_name(installation_id: int, chunk: int) -> str:
    """Stable vault name for one exact-scope token chunk."""
    if installation_id <= 0 or chunk < 0:
        raise ValueError("invalid installation token chunk")
    return f"GH_TOKEN_I{installation_id}_{chunk}"


def token_scopes_for_groups(
    groups: list[tuple[int, PermissionProfile, tuple[int, ...]]],
    *,
    multi_installation: bool | None = None,
) -> tuple[TokenScope, ...]:
    """Leave every single-installation snapshot and fingerprint unchanged."""
    if multi_installation is None:
        multi_installation = len({installation_id for installation_id, _, _ in groups}) > 1
    if not multi_installation:
        return ()
    chunks: dict[int, int] = {}
    scopes: list[TokenScope] = []
    for installation_id, _, repo_ids in sorted(groups):
        chunk = chunks.get(installation_id, 0)
        chunks[installation_id] = chunk + 1
        scopes.append((installation_id, chunk, tuple(sorted(repo_ids)), None, None))
    return tuple(sorted(scopes, key=lambda scope: (scope[0], scope[1])))


def token_scopes_for_tokens(
    tokens: tuple[AppToken, ...], *, include: bool
) -> tuple[TokenScope, ...]:
    if not include:
        return ()
    if any(token.chunk is None or token.issued_at is None for token in tokens):
        raise ValueError("multi-installation token identity is incomplete")
    return tuple(
        sorted(
            (
                token.installation_id,
                token.chunk,
                tuple(sorted(token.repo_ids)),
                token.issued_at,
                str(token.token_id),
            )
            for token in tokens
            if token.chunk is not None and token.issued_at is not None
        )
    )


def projected_vault_credential_count(
    groups: list[tuple[int, PermissionProfile, tuple[int, ...]]],
    *,
    other_credentials: int = 1,
    include_alias: bool = True,
    multi_installation: bool | None = None,
) -> int:
    """Count vault token variables and other credentials after sync."""
    if other_credentials < 0:
        raise ValueError("vault credential count cannot be negative")
    if multi_installation is None:
        multi_installation = len({installation_id for installation_id, _, _ in groups}) > 1
    token_variables = len(groups) + int(include_alias) if multi_installation else int(bool(groups))
    return token_variables + int(bool(groups) and include_alias) + other_credentials


def require_vault_budget(
    groups: list[tuple[int, PermissionProfile, tuple[int, ...]]],
    *,
    other_credentials: int = 1,
    include_alias: bool = True,
    multi_installation: bool | None = None,
) -> None:
    if len({installation_id for installation_id, _, _ in groups}) > MAX_INSTALLATIONS_PER_AGENT:
        raise ValueError("GitHub App access exceeds 12 installations per agent")
    if projected_vault_credential_count(
        groups,
        other_credentials=other_credentials,
        include_alias=include_alias,
        multi_installation=multi_installation,
    ) > (MAX_SESSION_VAULT_CREDENTIALS):
        raise ValueError("GitHub App access exceeds the 15-credential session vault limit")


def _working_alias(
    tokens: tuple[AppToken, ...], working_token: str | None, *, multi_installation: bool
) -> str | None:
    if working_token is not None and any(token.token == working_token for token in tokens):
        return working_token
    if tokens and not multi_installation:
        return tokens[0].token
    return None


@dataclass(frozen=True)
class AppToken:
    token_id: uuid.UUID
    token: str
    installation_id: int
    repo_ids: tuple[int, ...]
    profile: PermissionProfile
    credential_name: str
    issued_at: int | None = None
    chunk: int | None = None


@dataclass(frozen=True)
class AppSessionAccess:
    resources: tuple[Resource, ...]
    tokens: tuple[AppToken, ...]
    working_token: str | None
    excluded_repo_urls: tuple[str, ...] = ()
    token_repo_urls: tuple[str, ...] = ()
    token_scopes: tuple[TokenScope, ...] = ()
    repo_urls_by_id: tuple[tuple[int, str], ...] = ()
    multi_installation: bool = False
    working_repo_id: int | None = None
    repo_credential_map: tuple[tuple[str, str], ...] = ()


class GitHubTokenMintUnavailable(RuntimeError):
    """A retryable App token exchange failure, without GitHub response content."""


REQUESTER_CACHE = PermissionCache()
type EffectiveAppRows = tuple[tuple[AgentGrant, AuthorizedRepo, PermissionProfile], ...]
type EffectiveAppGrantView = tuple[EffectiveAppRows, int | None, int | None]

#: Bot identity per App slug. The numeric bot id never changes for a slug, so a
#: process asks GitHub once; a failed lookup is not cached and is retried by the
#: next session.
_BOT_IDENTITIES: dict[str, BotIdentity] = {}


async def resolve_bot_identity(
    client: httpx.AsyncClient, *, slug: str | None
) -> BotIdentity | None:
    """The App's bot commit identity, or None when it cannot be resolved.

    Best-effort: a missing slug, an unknown bot user or a GitHub failure leaves
    the session without a preset identity rather than failing it.
    """
    if slug is None:
        return None
    cached = _BOT_IDENTITIES.get(slug)
    if cached is not None:
        return cached
    try:
        identity = await get_bot_identity(client, slug=slug)
    except (httpx.HTTPError, ValueError):
        _log.warning("Could not resolve the GitHub App bot identity for %s", slug, exc_info=True)
        return None
    if identity is not None:
        _BOT_IDENTITIES[slug] = identity
    return identity


async def effective_repo_urls(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    config: GithubAppSettings,
    fernet: MultiFernet | None,
    mounted_only: bool = False,
) -> tuple[str, ...]:
    """Every repo the agent can reach, or with `mounted_only` just the mounted working repo."""
    urls, _ = await effective_repo_state(
        sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        config=config,
        fernet=fernet,
        mounted_only=mounted_only,
    )
    return urls


async def effective_repo_url_sets(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    config: GithubAppSettings,
    fernet: MultiFernet | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[TokenScope, ...]]:
    """The legacy three-field view of the effective repository plan."""
    every, working, scopes, _ = await effective_repo_plan(
        sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        config=config,
        fernet=fernet,
    )
    return every, working, scopes


async def effective_repo_plan(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    config: GithubAppSettings,
    fernet: MultiFernet | None,
    excluded_repo_urls: tuple[str, ...] = (),
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[TokenScope, ...],
    tuple[tuple[str, str], ...],
]:
    """One grant view determines scope, mount and non-secret helper map."""
    rows, _, _ = await effective_app_grant_view(
        sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        fernet=fernet,
    )
    if not rows:
        return (), (), (), ()
    every = tuple(sorted(f"https://github.com/{repo.repo_full_name}" for _, repo, _ in rows))
    working = {
        f"https://github.com/{repo.repo_full_name}"
        for grant, repo, _ in rows
        if grant.is_working_repo
    }
    multi_installation = len({repo.installation_id for _, repo, _ in rows}) > 1
    excluded = set(excluded_repo_urls)
    groups = group_repository_access(
        [
            (repo.installation_id, repo.repo_id, profile)
            for _, repo, profile in rows
            if f"https://github.com/{repo.repo_full_name}" not in excluded
        ],
        sort_repo_ids=multi_installation,
    )
    scopes = token_scopes_for_groups(groups, multi_installation=multi_installation)
    names = {repo.repo_id: repo.repo_full_name for _, repo, _ in rows}
    helper_entries = tuple(
        sorted(
            (
                names[repo_id],
                installation_credential_name(installation_id, chunk),
            )
            for installation_id, chunk, repo_ids, _, _ in map(token_scope_parts, scopes)
            if chunk is not None
            for repo_id in repo_ids
        )
    )
    return (
        every,
        tuple(url for url in every if url in working),
        scopes,
        helper_entries,
    )


def repo_credential_entries(access: AppSessionAccess) -> tuple[tuple[str, str], ...]:
    """Map granted repos to variables; an unminted variable fails closed in sh."""
    return access.repo_credential_map if access.multi_installation else ()


async def effective_repo_state(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    config: GithubAppSettings,
    fernet: MultiFernet | None,
    mounted_only: bool = False,
) -> tuple[tuple[str, ...], dict[int, dict[str, str]]]:
    rows, _, _ = await effective_app_grant_view(
        sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        fernet=fernet,
    )
    mounted = _mounted_repo_ids(
        [repo.repo_id for _, repo, _ in rows],
        working_ids={grant.repo_id for grant, _, _ in rows if grant.is_working_repo},
    )
    return (
        tuple(
            sorted(
                f"https://github.com/{repo.repo_full_name}"
                for _, repo, _ in rows
                if not mounted_only or repo.repo_id in mounted
            )
        ),
        {repo.repo_id: dict(PERMISSION_PROFILES[profile]) for _, repo, profile in rows},
    )


async def create_session_vault(
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    public_url: str | None,
    jwt_secret: bytes | None,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> str:
    scope = resource_scope(
        tenant_id=str(tenant_id),
        account_id=str(account_id) if account_id is not None else "service",
    )
    vault = await create_vault(anthropic, f"github-session:{uuid.uuid4()}", scope=scope)
    try:
        if public_url is not None and jwt_secret is not None and account_id is not None:
            async with sessionmaker() as session, session.begin():
                payload = await chat_mcp_credential(
                    session,
                    account_id=account_id,
                    agent_id=agent_id,
                    jwt_secret=jwt_secret,
                    public_url=public_url,
                    now=datetime.now(UTC),
                )
                await store_credential(anthropic, vault.id, payload, scope=scope)
    except BaseException:
        await archive_vault(anthropic, vault.id, scope=scope)
        raise
    return vault.id


async def effective_app_grant_view(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    fernet: MultiFernet | None,
) -> EffectiveAppGrantView:
    async with sessionmaker.begin() as session:
        await lock_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
        live = await list_live_grant_repositories(session, tenant_id=tenant_id, agent_id=agent_id)
    if not live:
        return (), None, None
    if fernet is None:
        raise ValueError("GitHub App mode requires encryption")
    rows: list[tuple[AgentGrant, AuthorizedRepo, PermissionProfile]] = []
    for grant, repo in live:
        profile: PermissionProfile = (
            "write" if grant.ceiling_access == "write" and repo.max_access == "write" else "read"
        )
        rows.append((grant, repo, profile))
    # Existing single-installation read grants remain valid in M1. The #1005
    # multi-installation path is write-only.
    if len({repo.installation_id for _, repo, _ in rows}) > 1 and any(
        profile != "write" for _, _, profile in rows
    ):
        raise ValueError("multi-installation App grants require write access")
    return tuple(rows), None, None


async def revoke_token(client: httpx.AsyncClient, token: str) -> None:
    response = await client.delete(
        "https://api.github.com/installation/token",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if response.status_code not in (204, 401, 404):
        response.raise_for_status()


async def _declared_repositories(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[tuple[AgentGrant, AuthorizedRepo]]:
    async with sessionmaker() as session:
        return await list_live_grant_repositories(session, tenant_id=tenant_id, agent_id=agent_id)


async def _enqueue_mint_failure_reconciliation(
    session: AsyncSession, *, installation_id: int, now: datetime
) -> None:
    """Queue once per installation snapshot, even if several groups fail."""
    installation = await get_installation(session, installation_id=installation_id)
    snapshot = (
        installation.github_refreshed_at.isoformat()
        if installation is not None and installation.github_refreshed_at is not None
        else "unrefreshed"
    )
    await enqueue_reconciliation(
        session,
        installation_id=installation_id,
        delivery_id=f"mint-failure:{installation_id}:{snapshot}",
        event="mint_failure",
        deleted=False,
        now=now,
    )


async def declared_app_repositories(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> tuple[WorkspaceSource, ...]:
    """Local declared access only; requester authorization/materialization stays unresolved."""
    rows = await _declared_repositories(sessionmaker, tenant_id=tenant_id, agent_id=agent_id)
    return tuple(
        WorkspaceSource(
            kind="repository",
            target_path=grant.mount_path or f"/workspace/{repo.repo_full_name}",
            repository_url=f"https://github.com/{repo.repo_full_name}",
            credential_ref=f"app-repo:{tenant_id}:{agent_id}:{repo.repo_id}:{grant.version}:{repo.version}",
        )
        for grant, repo in rows
    )


async def prepare_app_access(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    provisional_session_id: str,
    config: GithubAppSettings,
    fernet: MultiFernet | None,
    cache: PermissionCache,
    excluded_repo_urls: tuple[str, ...] = (),
    installation_ids: frozenset[int] | None = None,
    grant_view: EffectiveAppGrantView | None = None,
    anthropic: AsyncAnthropic | None = None,
    vault_id: str | None = None,
) -> AppSessionAccess:
    """Mint only this session's effective repository groups, recording every token."""
    rows, user_id, link_generation = grant_view or await effective_app_grant_view(
        sessionmaker,
        tenant_id=tenant_id,
        agent_id=agent_id,
        fernet=fernet,
    )
    if not rows:
        return AppSessionAccess((), (), None)
    if fernet is None or config.app_id is None or config.private_key is None:
        raise ValueError("GitHub App mode requires App credentials and encryption")
    repo_urls = {repo.repo_id: f"https://github.com/{repo.repo_full_name}" for _, repo, _ in rows}
    skipped_repo_ids = {
        repo.repo_id for _, repo, _ in rows if repo_urls[repo.repo_id] in excluded_repo_urls
    }
    multi_installation = len({repo.installation_id for _, repo, _ in rows}) > 1
    active_groups = group_repository_access(
        [
            (repo.installation_id, repo.repo_id, access)
            for _, repo, access in rows
            if repo.repo_id not in skipped_repo_ids
        ],
        sort_repo_ids=multi_installation,
    )
    active_groups.sort(key=lambda group: (group[0], group[1], group[2]))
    other_credentials = 1
    if anthropic is not None and vault_id is not None:
        scope = resource_scope(
            tenant_id=str(tenant_id),
            account_id=str(account_id) if account_id is not None else "service",
        )
        existing = [cred async for cred in list_credentials(anthropic, vault_id, scope=scope)]
        other_credentials = sum(
            not (
                isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse)
                and cred.auth.secret_name.startswith("GH_TOKEN")
            )
            and not (
                isinstance(cred.auth, BetaManagedAgentsStaticBearerAuthResponse)
                and cred.auth.mcp_server_url == GITHUB_COPILOT_MCP_URL
            )
            for cred in existing
        )
    active_working = any(
        grant.is_working_repo and repo.repo_id not in skipped_repo_ids for grant, repo, _ in rows
    )
    include_alias = active_working or (bool(active_groups) and not multi_installation)
    require_vault_budget(
        active_groups,
        other_credentials=other_credentials,
        include_alias=include_alias,
        multi_installation=multi_installation,
    )
    mint_groups = [
        group for group in active_groups if installation_ids is None or group[0] in installation_ids
    ]
    jwt = build_app_jwt(
        config.private_key.get_secret_value(), config.app_id, now=int(datetime.now(UTC).timestamp())
    )
    tokens: list[AppToken] = []
    excluded_repo_ids: set[int] = set(skipped_repo_ids)
    pending_token_id: uuid.UUID | None = None
    chunk_by_installation: dict[int, int] = {}
    failed_installations: set[int] = set()
    repo_ids_by_installation = {
        installation_id: {
            repo_id
            for group_id, _, ids in active_groups
            if group_id == installation_id
            for repo_id in ids
        }
        for installation_id, _, _ in active_groups
    }
    try:
        for installation_id, profile, repo_ids in mint_groups:
            if installation_id in failed_installations:
                continue
            chunk = chunk_by_installation.get(installation_id, 0)
            chunk_by_installation[installation_id] = chunk + 1
            versions = {
                key: version
                for grant, repo, _ in rows
                if repo.repo_id in repo_ids
                for key, version in (
                    (f"grant:{repo.repo_id}", grant.version),
                    (f"authorization:{repo.repo_id}", repo.version),
                )
            }
            expires_at = datetime.now(UTC) + timedelta(minutes=55)
            async with sessionmaker.begin() as session:
                pending = await create_pending(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=provisional_session_id,
                    installation_id=installation_id,
                    repo_ids=list(repo_ids),
                    permissions=dict(PERMISSION_PROFILES[profile]),
                    grant_versions=versions,
                    expires_at=expires_at,
                    requester_account_id=account_id if user_id is not None else None,
                    github_user_id=user_id,
                    link_generation=link_generation,
                    chunk=chunk if multi_installation else None,
                )
            pending_token_id = pending.token_id
            try:
                token = await mint_installation_token(
                    client,
                    jwt=jwt,
                    installation_id=installation_id,
                    repository_ids=list(repo_ids),
                    profile=profile,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (403, 404, 422):
                    raise GitHubTokenMintUnavailable(
                        "GitHub access is temporarily unavailable. Try again."
                    ) from exc
                async with sessionmaker.begin() as session:
                    await mark_revoked(session, token_id=pending.token_id)
                    await _enqueue_mint_failure_reconciliation(
                        session, installation_id=installation_id, now=datetime.now(UTC)
                    )
                pending_token_id = None
                if multi_installation:
                    failed_installations.add(installation_id)
                    excluded_repo_ids.update(repo_ids_by_installation[installation_id])
                    previous_chunks = tuple(
                        item for item in tokens if item.installation_id == installation_id
                    )
                    if previous_chunks:
                        await revoke_app_access(
                            sessionmaker,
                            client,
                            AppSessionAccess((), previous_chunks, None),
                        )
                        tokens = [
                            item for item in tokens if item.installation_id != installation_id
                        ]
                else:
                    excluded_repo_ids.update(repo_ids)
                _log.warning(
                    "GitHub App token mint could not cover installation %s (HTTP %s)",
                    installation_id,
                    exc.response.status_code,
                )
                continue
            except Exception as exc:
                raise GitHubTokenMintUnavailable(
                    "GitHub access is temporarily unavailable. Try again."
                ) from exc
            pending_token_id = None
            try:
                async with sessionmaker.begin() as session:
                    await store_token(
                        session, token_id=pending.token_id, token=token, fernet=fernet
                    )
                    await append_github_token_event(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        account_id=account_id,
                        kind="github_token_mint",
                        outcome="allowed",
                        reason="effective App grant",
                        token_id=pending.token_id,
                        session_id=provisional_session_id,
                        installation_id=installation_id,
                        repo_ids=list(repo_ids),
                        permissions=dict(PERMISSION_PROFILES[profile]),
                        expires_at=expires_at,
                        grant_versions=versions,
                        turn_origin_id=current_origin_id.get(),
                    )
            except GitHubTokenRowClosedError:
                await revoke_token(client, token)
                raise
            except Exception:
                await revoke_token(client, token)
                raise
            credential_name = installation_credential_name(installation_id, chunk)
            tokens.append(
                AppToken(
                    pending.token_id,
                    token,
                    installation_id,
                    repo_ids,
                    profile,
                    credential_name,
                    int(pending.created_at.timestamp()),
                    chunk if multi_installation else None,
                )
            )
            async with sessionmaker() as session:
                stale = await select_stale_tokens(session, include_live_version_bumps=True)
            if any(row.token_id == pending.token_id for row in stale):
                raise ValueError("GitHub access changed during token mint")
    except Exception:
        if pending_token_id is not None:
            try:
                async with sessionmaker.begin() as session:
                    await mark_revoked(session, token_id=pending_token_id)
            except Exception:
                _log.exception("Failed to close pending GitHub token row %s", pending_token_id)
        for issued in tokens:
            try:
                await revoke_token(client, issued.token)
                async with sessionmaker.begin() as session:
                    await mark_revoked(session, token_id=issued.token_id)
            except Exception:
                _log.exception("Failed to revoke partially minted GitHub token %s", issued.token_id)
        raise
    by_repo = {repo_id: item.token for item in tokens for repo_id in item.repo_ids}
    resources: list[Resource] = []
    available_rows = [
        (grant, repo, profile) for grant, repo, profile in rows if repo.repo_id in by_repo
    ]
    mounted = _mounted_repo_ids(
        [repo.repo_id for _, repo, _ in available_rows],
        working_ids={grant.repo_id for grant, _, _ in rows if grant.is_working_repo},
    )
    for grant, repo, _ in available_rows:
        if repo.repo_id not in mounted:
            continue
        owner, name = repo.repo_full_name.split("/", 1)
        resources.append(
            {
                "type": "github_repository",
                "url": f"https://github.com/{repo.repo_full_name}",
                "authorization_token": by_repo[repo.repo_id],
                "mount_path": grant.mount_path or f"/workspace/{owner}/{name}",
            }
        )
    working_repo_id = next((grant.repo_id for grant, _, _ in rows if grant.is_working_repo), None)
    working = next(
        (by_repo[grant.repo_id] for grant, _, _ in available_rows if grant.is_working_repo), None
    )
    excluded = tuple(
        sorted(
            f"https://github.com/{repo.repo_full_name}"
            for _, repo, _ in rows
            if repo.repo_id in excluded_repo_ids
        )
    )
    token_repo_urls = tuple(
        sorted(f"https://github.com/{repo.repo_full_name}" for _, repo, _ in available_rows)
    )
    return AppSessionAccess(
        tuple(resources),
        tuple(tokens),
        _working_alias(tuple(tokens), working, multi_installation=multi_installation),
        excluded,
        token_repo_urls,
        token_scopes_for_tokens(tuple(tokens), include=multi_installation),
        tuple(sorted((repo_id, repo_urls[repo_id]) for repo_id in by_repo)),
        multi_installation,
        working_repo_id,
        tuple(
            sorted(
                (
                    repo_urls[repo_id].removeprefix("https://github.com/"),
                    token.credential_name,
                )
                for token in tokens
                for repo_id in token.repo_ids
            )
        )
        if multi_installation
        else (),
    )


#: The sandbox holds at most one repo: the agent's working repo, chosen in setup.
#: Every other granted repo is reachable through the vault tokens only and is
#: cloned on demand (Derick, 2026-10-10). Mounting all grants made Managed Agents
#: fail provisioning at 75 repos.
MAX_MOUNTED_REPOS = 1


def _mounted_repo_ids(repo_ids: list[int], *, working_ids: set[int]) -> set[int]:
    """Only the working repo is mounted; none when the agent has no working repo."""
    return {repo_id for repo_id in dict.fromkeys(repo_ids) if repo_id in working_ids}


async def revoke_app_access(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    access: AppSessionAccess,
) -> None:
    for issued in access.tokens:
        try:
            await revoke_token(client, issued.token)
            async with sessionmaker.begin() as session:
                row = await mark_revoked(session, token_id=issued.token_id)
                await append_github_token_event(
                    session,
                    tenant_id=row.tenant_id,
                    agent_id=row.agent_id,
                    account_id=row.requester_account_id,
                    kind="github_token_revoke",
                    outcome="allowed",
                    reason="session creation failed",
                    token_id=row.token_id,
                    session_id=row.session_id,
                    installation_id=row.installation_id,
                    repo_ids=row.repo_ids,
                    permissions=row.permissions,
                    expires_at=row.expires_at,
                    grant_versions=row.grant_versions,
                )
        except Exception:
            _log.exception("Failed to revoke GitHub token for failed session %s", issued.token_id)


async def add_app_credentials(
    anthropic: AsyncAnthropic,
    *,
    vault_id: str,
    access: AppSessionAccess,
    on_mutation: Callable[[], None] | None = None,
    on_delivered: Callable[[str], None] | None = None,
    unchanged_credential_names: frozenset[str] = frozenset(),
    scope: Scope | None = None,
) -> None:
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.github_app_session:add_app_credentials"
    )
    multi_installation = (
        access.multi_installation or len({token.installation_id for token in access.tokens}) > 1
    )
    working_token = _working_alias(
        access.tokens, access.working_token, multi_installation=multi_installation
    )
    desired = (
        {issued.credential_name: issued.token for issued in access.tokens}
        if multi_installation
        else {}
    )
    if working_token is not None:
        desired["GH_TOKEN"] = working_token
    all_credentials = [cred async for cred in list_credentials(anthropic, vault_id, scope=scope)]
    existing = {
        cred.auth.secret_name: cred.id
        for cred in all_credentials
        if isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse)
        and cred.auth.secret_name.startswith("GH_TOKEN")
    }
    copilot_ids = [
        cred.id
        for cred in all_credentials
        if isinstance(cred.auth, BetaManagedAgentsStaticBearerAuthResponse)
        and cred.auth.mcp_server_url == GITHUB_COPILOT_MCP_URL
    ]
    other_count = sum(
        cred.id not in set(existing.values())
        and not (
            isinstance(cred.auth, BetaManagedAgentsStaticBearerAuthResponse)
            and cred.auth.mcp_server_url == GITHUB_COPILOT_MCP_URL
        )
        for cred in all_credentials
    )
    require_vault_budget(
        [(item.installation_id, item.profile, item.repo_ids) for item in access.tokens],
        other_credentials=other_count,
        include_alias=working_token is not None,
        multi_installation=multi_installation,
    )
    for name, credential_id in existing.items():
        if name not in desired:
            await delete_credential(anthropic, vault_id, credential_id, scope=scope)
            if on_mutation is not None:
                on_mutation()
    for credential_id in copilot_ids[1:] if working_token is not None else copilot_ids:
        await delete_credential(anthropic, vault_id, credential_id, scope=scope)
        if on_mutation is not None:
            on_mutation()
    for name, token in desired.items():
        if name in existing and name in unchanged_credential_names:
            continue
        if name in existing:
            await update_credential(
                anthropic,
                vault_id,
                existing[name],
                {"auth": {"type": "environment_variable", "secret_value": token}},
                scope=scope,
            )
        else:
            await store_credential(
                anthropic,
                vault_id,
                {
                    "auth": {
                        "type": "environment_variable",
                        "secret_name": name,
                        "secret_value": token,
                        "networking": {
                            "type": "limited",
                            "allowed_hosts": ["api.github.com", "github.com", "uploads.github.com"],
                        },
                        "injection_location": {"header": True, "body": False},
                    }
                },
                scope=scope,
            )
        if on_mutation is not None:
            on_mutation()
        if on_delivered is not None:
            on_delivered(token)
    copilot_exists = bool(copilot_ids)
    if working_token is not None and not (
        "GH_TOKEN" in unchanged_credential_names and copilot_exists
    ):
        await add_github_copilot_credential(
            anthropic, vault_id=vault_id, token=working_token, in_place=True, scope=scope
        )
        if on_mutation is not None:
            on_mutation()
        if on_delivered is not None:
            on_delivered(working_token)


async def finish_app_delivery(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    access: AppSessionAccess,
    provisional_session_id: str,
    session_id: str,
    grant_guard: tuple[uuid.UUID, uuid.UUID] | None = None,
) -> None:
    async with sessionmaker.begin() as session:
        if grant_guard is not None:
            await lock_agent_grants(session, tenant_id=grant_guard[0], agent_id=grant_guard[1])
            if await _access_is_stale(session, access):
                raise ValueError("GitHub access changed during session delivery")
        await set_session_id(
            session, provisional_session_id=provisional_session_id, session_id=session_id
        )
        for issued in access.tokens:
            delivered = await mark_delivered(session, token_id=issued.token_id)
            await append_github_token_event(
                session,
                tenant_id=delivered.tenant_id,
                agent_id=delivered.agent_id,
                account_id=delivered.requester_account_id,
                kind="github_token_deliver",
                outcome="allowed",
                reason="session resource and vault",
                token_id=issued.token_id,
                session_id=session_id,
                installation_id=issued.installation_id,
                repo_ids=list(issued.repo_ids),
                permissions=dict(PERMISSION_PROFILES[issued.profile]),
                expires_at=delivered.expires_at,
                grant_versions=delivered.grant_versions,
            )
    async with sessionmaker() as session:
        stale = await select_stale_tokens(session, include_live_version_bumps=True)
    stale_ids = {row.token_id for row in stale}
    if any(issued.token_id in stale_ids for issued in access.tokens):
        for issued in access.tokens:
            await revoke_token(client, issued.token)
            async with sessionmaker.begin() as session:
                await mark_revoked(session, token_id=issued.token_id)
        raise ValueError("GitHub access changed during session delivery")


async def _access_is_stale(session: AsyncSession, access: AppSessionAccess) -> bool:
    """Limit a grant-locked recheck to this session's replacement tokens."""
    for token in access.tokens:
        if await select_stale_tokens(
            session, include_live_version_bumps=True, token_id=token.token_id
        ):
            return True
    return False


async def revoke_session_tokens(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    session_id: str,
    fernet: MultiFernet,
    except_ids: frozenset[uuid.UUID] = frozenset(),
) -> None:
    async with sessionmaker() as session:
        rows = await list_session_tokens(session, session_id=session_id)
    for row in rows:
        if row.token_id in except_ids:
            continue
        token = decrypt_issued_token(row, fernet=fernet)
        if token is None:
            continue
        await revoke_token(client, token)
        async with sessionmaker.begin() as session:
            await mark_revoked(session, token_id=row.token_id)
            await append_github_token_event(
                session,
                tenant_id=row.tenant_id,
                agent_id=row.agent_id,
                account_id=row.requester_account_id,
                kind="github_token_revoke",
                outcome="allowed",
                reason="session token replaced or closed",
                token_id=row.token_id,
                session_id=session_id,
                installation_id=row.installation_id,
                repo_ids=row.repo_ids,
                permissions=row.permissions,
                expires_at=row.expires_at,
                grant_versions=row.grant_versions,
            )


async def close_headless_app_session(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    fernet: MultiFernet | None,
) -> None:
    """Finish a routine's App session; an interrupted cleanup is retried by the sweeper."""
    async with sessionmaker.begin() as session:
        vault_id = await finish_headless_app_session(session, session_id=session_id)
    if vault_id is None:
        return
    async with httpx.AsyncClient() as client:
        if fernet is None:
            async with sessionmaker() as session:
                if await list_session_tokens(session, session_id=session_id):
                    raise ValueError("App session revocation requires encryption")
        else:
            await revoke_session_tokens(sessionmaker, client, session_id=session_id, fernet=fernet)
    await archive_app_vault(anthropic, vault_id=vault_id)
    async with sessionmaker.begin() as session:
        await mark_headless_app_session_closed(session, session_id=session_id)


async def archive_app_vault(
    anthropic: AsyncAnthropic,
    *,
    vault_id: str,
    scope: Scope | None = None,
) -> None:
    """A repeated cleanup may find a vault archived by an earlier attempt."""
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.github_app_session:archive_app_vault"
    )
    try:
        await archive_vault(anthropic, vault_id, scope=scope)
    except APIStatusError as exc:
        if exc.status_code not in (404, 409) and not (
            exc.status_code == 400 and "already archived" in str(exc).lower()
        ):
            raise


async def _current_app_access(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    fernet: MultiFernet,
    grant_rows: EffectiveAppRows | None = None,
) -> AppSessionAccess:
    """Rebuild the current mounted values from encrypted issued tokens for rollback."""
    async with sessionmaker() as session:
        issued = [
            row
            for row in await list_session_tokens(session, session_id=session_id)
            if row.status == "delivered" and row.superseded_at is None
        ]
        # A revoked grant or removed installation repository is still needed to
        # restore a running turn if this refresh fails after touching MA.
        repo_rows = await list_authorized_repos(session, tenant_id=tenant_id, agent_id=agent_id)
        current_grants = (
            [grant for grant, _, _ in grant_rows]
            if grant_rows is not None
            else await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
        )
        repos = {repo.repo_id: repo for repo in repo_rows}
        grants = {grant.repo_id: grant for grant in current_grants}
        working_ids = {grant.repo_id for grant in current_grants if grant.is_working_repo}
        effective_config = await live_effective_config_for_ma_session(
            session, ma_session_id=session_id
        )
        recorded_scopes = (
            SessionSnapshot.model_validate(effective_config).token_scopes
            if effective_config is not None
            else ()
        )
    # Reconstruct the delivered mode. New grants may already describe the
    # target mode while these rows still belong to the previous one.
    multi_installation = (
        bool(recorded_scopes)
        or any(row.chunk is not None for row in issued)
        or len({row.installation_id for row in issued}) > 1
    )
    working_repo_id = next(iter(sorted(working_ids)), None)
    if multi_installation and any(row.chunk is None for row in issued):
        raise ValueError("multi-installation token inventory has no persisted chunk label")
    recorded_by_token_id = {
        uuid.UUID(token_id): (installation_id, chunk, tuple(sorted(repo_ids)), issued_at)
        for scope in recorded_scopes
        for installation_id, chunk, repo_ids, issued_at, token_id in (token_scope_parts(scope),)
        if token_id is not None
    }
    ordered = sorted(
        issued,
        key=lambda row: (
            row.installation_id,
            (row.chunk if row.chunk is not None else 0)
            if multi_installation
            else ("write" if row.permissions.get("contents") == "write" else "read"),
            row.repo_ids,
        ),
    )
    tokens: list[AppToken] = []
    resources: list[Resource] = []
    working_token: str | None = None
    mounted = _mounted_repo_ids(
        [repo_id for row in ordered for repo_id in row.repo_ids], working_ids=working_ids
    )
    for row in ordered:
        if recorded_by_token_id and recorded_by_token_id.get(row.token_id) != (
            row.installation_id,
            row.chunk,
            tuple(sorted(row.repo_ids)),
            int(row.created_at.timestamp()),
        ):
            raise ValueError("current App token disagrees with its saved snapshot record")
        token = decrypt_issued_token(row, fernet=fernet)
        if (
            token is None
            or not row.repo_ids
            or any(repo_id not in repos for repo_id in row.repo_ids)
        ):
            raise ValueError("current App token cannot be restored")
        profile: PermissionProfile = (
            "write" if row.permissions.get("contents") == "write" else "read"
        )
        chunk = row.chunk if row.chunk is not None else 0
        credential_name = installation_credential_name(row.installation_id, chunk)
        tokens.append(
            AppToken(
                row.token_id,
                token,
                row.installation_id,
                tuple(row.repo_ids),
                profile,
                credential_name,
                int(row.created_at.timestamp()),
                chunk,
            )
        )
        for repo_id in row.repo_ids:
            if repo_id not in mounted:
                continue
            repo = repos[repo_id]
            owner, name = repo.repo_full_name.split("/", 1)
            resources.append(
                {
                    "type": "github_repository",
                    "url": f"https://github.com/{repo.repo_full_name}",
                    "authorization_token": token,
                    "mount_path": (grants[repo_id].mount_path if repo_id in grants else None)
                    or f"/workspace/{owner}/{name}",
                }
            )
        if working_token is None and any(repo_id in working_ids for repo_id in row.repo_ids):
            working_token = token
    return AppSessionAccess(
        tuple(resources),
        tuple(tokens),
        _working_alias(tuple(tokens), working_token, multi_installation=multi_installation),
        token_repo_urls=tuple(
            sorted(
                f"https://github.com/{repos[repo_id].repo_full_name}"
                for item in tokens
                for repo_id in item.repo_ids
            )
        ),
        token_scopes=token_scopes_for_tokens(
            tuple(tokens), include=multi_installation or bool(recorded_scopes)
        ),
        repo_urls_by_id=tuple(
            sorted(
                (repo_id, f"https://github.com/{repos[repo_id].repo_full_name}")
                for item in tokens
                for repo_id in item.repo_ids
            )
        ),
        multi_installation=multi_installation,
        working_repo_id=working_repo_id,
        repo_credential_map=tuple(
            sorted(
                (
                    repos[repo_id].repo_full_name,
                    token.credential_name,
                )
                for token in tokens
                for repo_id in token.repo_ids
            )
        )
        if multi_installation
        else (),
    )


def _combine_app_access(
    replacement: AppSessionAccess,
    previous: AppSessionAccess,
    installation_ids: frozenset[int],
) -> AppSessionAccess:
    """Keep untouched installation tokens and their exact repo scope."""
    previous_mode = previous.multi_installation or bool(previous.token_scopes)
    if replacement.multi_installation != previous_mode:
        raise ValueError("App token mode changed during partial rotation")
    retained = tuple(
        token for token in previous.tokens if token.installation_id not in installation_ids
    )
    tokens = tuple(
        sorted(
            (*retained, *replacement.tokens),
            key=lambda token: (token.installation_id, token.credential_name),
        )
    )
    if any((token.chunk is not None) != replacement.multi_installation for token in tokens):
        raise ValueError("App token labels disagree with the delivered vault mode")
    if not replacement.multi_installation and (previous.token_scopes or replacement.token_scopes):
        raise ValueError("single-installation vault cannot retain multi-installation scopes")
    retained_values = {token.token for token in retained}
    resources = (
        tuple(
            resource
            for resource in previous.resources
            if resource["type"] == "github_repository"
            and resource["authorization_token"] in retained_values
        )
        + replacement.resources
    )
    retained_repo_ids = {repo_id for token in retained for repo_id in token.repo_ids}
    retained_repo_urls = {
        url for repo_id, url in previous.repo_urls_by_id if repo_id in retained_repo_ids
    }
    token_repo_urls = tuple(sorted(retained_repo_urls | set(replacement.token_repo_urls)))
    working = next(
        (
            token.token
            for token in tokens
            if replacement.working_repo_id is not None
            and replacement.working_repo_id in token.repo_ids
        ),
        None,
    )
    retained_map = (
        (repo, variable)
        for repo, variable in previous.repo_credential_map
        if f"https://github.com/{repo}" in retained_repo_urls
    )
    combined_map = tuple(
        sorted({**dict(retained_map), **dict(replacement.repo_credential_map)}.items())
    )
    return AppSessionAccess(
        resources,
        tokens,
        _working_alias(tokens, working, multi_installation=replacement.multi_installation),
        replacement.excluded_repo_urls,
        token_repo_urls,
        token_scopes_for_tokens(
            tokens,
            include=replacement.multi_installation,
        ),
        tuple(
            sorted(
                (
                    (repo_id, url)
                    for repo_id, url in previous.repo_urls_by_id
                    if repo_id in retained_repo_ids
                ),
            )
        )
        + replacement.repo_urls_by_id,
        replacement.multi_installation,
        replacement.working_repo_id,
        combined_map,
    )


async def _rotate_live_app_tokens_unlocked(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    vault_id: str,
    resource_ids: dict[str, str],
    config: GithubAppSettings,
    fernet: MultiFernet,
    active_turn: bool = False,
    unavailable_repos: tuple[tuple[str, datetime], ...] = (),
    installation_ids: tuple[int, ...] = (),
) -> AppSessionAccess:
    """Refresh resources and vault in place; leave equal-access old tokens to expire."""
    scope = resource_scope(
        tenant_id=str(tenant_id),
        account_id=str(account_id) if account_id is not None else "service",
    )
    provisional = f"pending:{uuid.uuid4()}"
    swapped = False

    # Token values MA already holds, in a resource or the vault.
    delivered: set[str] = set()

    def mark_swapped() -> None:
        nonlocal swapped
        swapped = True

    async with httpx.AsyncClient() as client:
        grant_view = await effective_app_grant_view(
            sessionmaker,
            tenant_id=tenant_id,
            agent_id=agent_id,
            fernet=fernet,
        )
        old_access: AppSessionAccess | None = None
        if active_turn or installation_ids:
            try:
                old_access = await _current_app_access(
                    sessionmaker,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    fernet=fernet,
                    grant_rows=grant_view[0],
                )
            except (ValueError, InvalidToken):
                # No rollback snapshot: refresh anyway. A failure still leaves the
                # running turn alone; the next turn boundary replaces the session.
                _log.warning("No App rollback snapshot for active session %s", session_id)
        refresh_installation_ids = installation_ids
        if old_access is not None and installation_ids:
            target_multi = len({repo.installation_id for _, repo, _ in grant_view[0]}) > 1
            if bool(old_access.token_scopes) != target_multi:
                refresh_installation_ids = ()
        # Rotation never retries a refused repo, even after its 60 minutes: only
        # the turn-boundary replacement does, so retries stay one per turn.
        refused = tuple(url for url, _ in unavailable_repos)
        minted_access = await prepare_app_access(
            sessionmaker,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            provisional_session_id=provisional,
            config=config,
            fernet=fernet,
            cache=REQUESTER_CACHE,
            excluded_repo_urls=refused,
            installation_ids=(
                frozenset(refresh_installation_ids) if refresh_installation_ids else None
            ),
            grant_view=grant_view,
            anthropic=anthropic,
            vault_id=vault_id,
        )
        if installation_ids and old_access is None:
            await revoke_app_access(sessionmaker, client, minted_access)
            raise ValueError("current App tokens are required for installation rotation")
        try:
            access = (
                _combine_app_access(minted_access, old_access, frozenset(refresh_installation_ids))
                if refresh_installation_ids and old_access is not None
                else minted_access
            )
        except Exception:
            await revoke_app_access(sessionmaker, client, minted_access)
            raise
        try:
            for resource in minted_access.resources:
                if resource["type"] != "github_repository":
                    continue
                resource_id = resource_ids.get(resource["url"])
                if resource_id is None:
                    # MA cannot add a repository to an existing session. The
                    # vault token works for API calls; the next turn replaces
                    # the session to mount the new checkout.
                    continue
                try:
                    await rotate_session_repo_token(
                        anthropic,
                        session_id,
                        resource_id,
                        resource["authorization_token"],
                        scope=scope,
                    )
                    swapped = True
                    delivered.add(resource["authorization_token"])
                except APIStatusError:
                    if not active_turn:
                        raise
                    # MA can refuse resource changes while a turn is running.
                    # Keep the in-session environment and Copilot token fresh.
                    _log.warning(
                        "MA refused App resource refresh during active turn %s: %s",
                        session_id,
                        resource_id,
                    )
            await add_app_credentials(
                anthropic,
                vault_id=vault_id,
                access=access,
                on_mutation=mark_swapped,
                on_delivered=delivered.add,
                unchanged_credential_names=(
                    frozenset(
                        {
                            token.credential_name
                            for token in old_access.tokens
                            if token.installation_id not in refresh_installation_ids
                        }
                        | (
                            {"GH_TOKEN"}
                            if old_access.working_token is not None
                            and access.working_token == old_access.working_token
                            else set()
                        )
                    )
                    if refresh_installation_ids and old_access is not None
                    else frozenset()
                ),
                scope=scope,
            )
            await finish_app_delivery(
                sessionmaker,
                client,
                access=minted_access,
                provisional_session_id=provisional,
                session_id=session_id,
                grant_guard=(tenant_id, agent_id),
            )
            async with sessionmaker.begin() as session:
                await lock_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
                if await _access_is_stale(session, access):
                    raise ValueError("GitHub access changed before token retirement")
                finished_at = datetime.now(UTC)
                await mark_session_tokens_superseded(
                    session,
                    session_id=session_id,
                    except_ids=frozenset(token.token_id for token in access.tokens),
                    now=finished_at,
                )
                await record_app_token_refresh(
                    session,
                    ma_session_id=session_id,
                    issued_at=int(finished_at.timestamp()),
                    access=access,
                    now=finished_at,
                )
            return access
        except Exception:
            kept: frozenset[uuid.UUID] = frozenset()
            try:
                if old_access is not None:
                    async with sessionmaker() as session:
                        usable_ids = {
                            row.token_id
                            for row in await list_session_tokens(session, session_id=session_id)
                        }
                        hard_stale_ids = {
                            row.token_id for row in await select_stale_tokens(session)
                        }
                    if any(
                        token.token_id not in usable_ids or token.token_id in hard_stale_ids
                        for token in old_access.tokens
                    ):
                        old_access = None
                if old_access is not None:
                    try:
                        async with sessionmaker.begin() as session:
                            await restore_session_tokens(
                                session,
                                token_ids=frozenset(token.token_id for token in old_access.tokens),
                            )
                    except Exception:
                        _log.exception(
                            "Failed to preserve old App tokens for active session %s", session_id
                        )
                    try:
                        await add_app_credentials(
                            anthropic, vault_id=vault_id, access=old_access, scope=scope
                        )
                    except Exception:
                        _log.exception(
                            "Failed to restore App vault for active session %s", session_id
                        )
                    for resource in old_access.resources:
                        if resource["type"] != "github_repository":
                            continue
                        resource_id = resource_ids.get(resource["url"])
                        if resource_id is not None:
                            try:
                                await rotate_session_repo_token(
                                    anthropic,
                                    session_id,
                                    resource_id,
                                    resource["authorization_token"],
                                    scope=scope,
                                )
                            except Exception:
                                _log.exception(
                                    "Failed to restore App resource %s for active session %s",
                                    resource_id,
                                    session_id,
                                )
                elif swapped and not active_turn:
                    # Only an idle session is archived; a running turn is never
                    # interrupted, and the next turn boundary replaces it.
                    await archive_session(anthropic, session_id, scope=scope)
                elif swapped:
                    _log.warning(
                        "App refresh failed mid-swap during active turn %s; left running",
                        session_id,
                    )
                if active_turn:
                    # A running tool may already hold a new token. Let those
                    # expire unless they are wider than what the session keeps.
                    try:
                        async with sessionmaker.begin() as session:
                            kept = await keep_failed_rotation_tokens(
                                session,
                                session_id=session_id,
                                token_ids=frozenset(
                                    token.token_id
                                    for token in minted_access.tokens
                                    if token.token in delivered
                                ),
                                now=datetime.now(UTC),
                            )
                    except Exception:
                        _log.exception(
                            "Failed to keep delivered App tokens for active session %s", session_id
                        )
            finally:
                await revoke_app_access(
                    sessionmaker,
                    client,
                    replace(
                        minted_access,
                        tokens=tuple(
                            token for token in minted_access.tokens if token.token_id not in kept
                        ),
                    ),
                )
            raise


async def rotate_live_app_tokens(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    vault_id: str,
    resource_ids: dict[str, str],
    config: GithubAppSettings,
    fernet: MultiFernet,
    active_turn: bool = False,
    unavailable_repos: tuple[tuple[str, datetime], ...] = (),
    installation_ids: tuple[int, ...] = (),
) -> AppSessionAccess:
    """Rotate without holding a database connection during network calls."""
    return await _rotate_live_app_tokens_unlocked(
        anthropic,
        sessionmaker,
        session_id=session_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        vault_id=vault_id,
        resource_ids=resource_ids,
        config=config,
        fernet=fernet,
        active_turn=active_turn,
        unavailable_repos=unavailable_repos,
        installation_ids=installation_ids,
    )

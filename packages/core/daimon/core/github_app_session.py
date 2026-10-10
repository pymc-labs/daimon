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
    PermissionProfile,
    build_app_jwt,
    group_repository_access,
    mint_installation_token,
)
from daimon.core.github_requester_access import (
    Access,
    PermissionCache,
    effective_access,
    linked_permissions,
)
from daimon.core.mcp_auth import mint_jwt
from daimon.core.mcp_vault import GITHUB_COPILOT_MCP_URL, add_github_copilot_credential
from daimon.core.stores.github_access import (
    AgentGrant,
    AuthorizedRepo,
    list_agent_grants,
    list_authorized_repos,
    list_live_grant_repositories,
)
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
from daimon.core.stores.github_links import get_account_link, get_user
from daimon.core.stores.security_audit import append_github_token_event
from daimon.core.turn_origin import current_origin_id
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppToken:
    token_id: uuid.UUID
    token: str
    installation_id: int
    repo_ids: tuple[int, ...]
    profile: PermissionProfile
    credential_name: str


@dataclass(frozen=True)
class AppSessionAccess:
    resources: tuple[Resource, ...]
    tokens: tuple[AppToken, ...]
    working_token: str | None


REQUESTER_CACHE = PermissionCache()


async def effective_repo_urls(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    is_external: bool,
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
        is_external=is_external,
        config=config,
        fernet=fernet,
        mounted_only=mounted_only,
    )
    return urls


async def effective_repo_state(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    is_external: bool,
    config: GithubAppSettings,
    fernet: MultiFernet | None,
    mounted_only: bool = False,
) -> tuple[tuple[str, ...], dict[int, dict[str, str]]]:
    if is_external:
        return (), {}
    async with sessionmaker() as session:
        if not await list_live_grant_repositories(session, tenant_id=tenant_id, agent_id=agent_id):
            return (), {}
    if fernet is None:
        raise ValueError("GitHub App mode requires encryption")
    async with httpx.AsyncClient() as client:
        rows, _, _ = await _effective_rows(
            sessionmaker,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            config=config,
            fernet=fernet,
            cache=REQUESTER_CACHE,
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
) -> str:
    vault = await anthropic.beta.vaults.create(display_name=f"github-session:{uuid.uuid4()}")
    try:
        if public_url is not None and jwt_secret is not None and account_id is not None:
            await anthropic.beta.vaults.credentials.create(
                vault_id=vault.id,
                auth={
                    "type": "static_bearer",
                    "mcp_server_url": public_url,
                    "token": mint_jwt(
                        account_id=account_id,
                        chat_agent_id=agent_id,
                        secret=jwt_secret,
                        now=datetime.now(UTC),
                    ),
                },
            )
    except BaseException:
        await anthropic.beta.vaults.archive(vault.id)
        raise
    return vault.id


async def _effective_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    config: GithubAppSettings,
    fernet: MultiFernet,
    cache: PermissionCache,
) -> tuple[list[tuple[AgentGrant, AuthorizedRepo, PermissionProfile]], int | None, int | None]:
    async with sessionmaker() as session:
        live = await list_live_grant_repositories(session, tenant_id=tenant_id, agent_id=agent_id)
        grants = [grant for grant, _ in live]
        repos = {repo.repo_id: repo for _, repo in live}
        link = await get_account_link(session, account_id=account_id) if account_id else None
        user = await get_user(session, github_user_id=link.github_user_id) if link else None
    if user is None or user.status != "active":
        user = None
    asker: dict[int, Access] = {}
    if user is not None:
        if config.client_id is None or config.client_secret is None:
            raise ValueError("GitHub App user authorization is not configured")
        for installation_id in {repo.installation_id for repo in repos.values()}:
            asker.update(
                await linked_permissions(
                    sessionmaker,
                    client,
                    user_id=user.github_user_id,
                    installation_id=installation_id,
                    fernet=fernet,
                    client_id=config.client_id,
                    client_secret=config.client_secret.get_secret_value(),
                    cache=cache,
                )
            )
        async with sessionmaker() as session:
            user = await get_user(session, github_user_id=user.github_user_id)
        if user is None or user.status != "active":
            raise ValueError("GitHub requester link changed during access check")
    abilities: dict[int, PermissionProfile] = {
        grant.repo_id: grant.ceiling_access for grant in grants if grant.repo_id in repos
    }
    baselines: dict[int, Access] = {grant.repo_id: grant.baseline_access for grant in grants}
    access = effective_access(abilities, asker if account_id is not None else {}, baselines)
    return (
        [
            (grant, repos[grant.repo_id], access[grant.repo_id])
            for grant in grants
            if grant.repo_id in access
        ],
        user.github_user_id if user is not None else None,
        user.link_generation if user is not None else None,
    )


async def revoke_token(client: httpx.AsyncClient, token: str) -> None:
    response = await client.delete(
        "https://api.github.com/installation/token",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if response.status_code not in (204, 401, 404):
        response.raise_for_status()


async def prepare_app_access(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    is_external: bool,
    provisional_session_id: str,
    config: GithubAppSettings,
    fernet: MultiFernet | None,
    cache: PermissionCache,
) -> AppSessionAccess:
    """Mint only this session's effective repository groups, recording every token."""
    if is_external:
        return AppSessionAccess((), (), None)
    async with sessionmaker() as session:
        if not await list_live_grant_repositories(session, tenant_id=tenant_id, agent_id=agent_id):
            return AppSessionAccess((), (), None)
    if fernet is None or config.app_id is None or config.private_key is None:
        raise ValueError("GitHub App mode requires App credentials and encryption")
    rows, user_id, link_generation = await _effective_rows(
        sessionmaker,
        client,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        config=config,
        fernet=fernet,
        cache=cache,
    )
    if not rows:
        return AppSessionAccess((), (), None)
    groups = group_repository_access(
        [(repo.installation_id, repo.repo_id, access) for _, repo, access in rows]
    )
    groups.sort(key=lambda group: (group[0], group[1], group[2]))
    if len(groups) > 15:
        raise ValueError("GitHub App access exceeds the session vault credential limit")
    jwt = build_app_jwt(
        config.private_key.get_secret_value(), config.app_id, now=int(datetime.now(UTC).timestamp())
    )
    tokens: list[AppToken] = []
    try:
        for group_index, (installation_id, profile, repo_ids) in enumerate(groups):
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
                )
            token = await mint_installation_token(
                client,
                jwt=jwt,
                installation_id=installation_id,
                repository_ids=list(repo_ids),
                profile=profile,
            )
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
            first_repo = next(repo for _, repo, _ in rows if repo.repo_id == repo_ids[0])
            owner = first_repo.repo_full_name.split("/", 1)[0]
            owner_key = "".join(char if char.isalnum() else "_" for char in owner.upper())
            credential_name = f"GH_TOKEN_{owner_key}_{profile.upper()}"
            if any(item.credential_name == credential_name for item in tokens):
                credential_name += f"_{group_index}"
            tokens.append(
                AppToken(
                    pending.token_id,
                    token,
                    installation_id,
                    repo_ids,
                    profile,
                    credential_name,
                )
            )
            async with sessionmaker() as session:
                stale = await select_stale_tokens(session)
            if any(row.token_id == pending.token_id for row in stale):
                raise ValueError("GitHub access changed during token mint")
    except Exception:
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
    mounted = _mounted_repo_ids(
        [repo.repo_id for _, repo, _ in rows],
        working_ids={grant.repo_id for grant, _, _ in rows if grant.is_working_repo},
    )
    for grant, repo, _ in rows:
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
    working = next((by_repo[grant.repo_id] for grant, _, _ in rows if grant.is_working_repo), None)
    return AppSessionAccess(tuple(resources), tuple(tokens), working or tokens[0].token)


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
) -> None:
    desired = {issued.credential_name: issued.token for issued in access.tokens}
    if access.working_token is not None:
        desired["GH_TOKEN"] = access.working_token
    existing = {
        cred.auth.secret_name: cred.id
        async for cred in anthropic.beta.vaults.credentials.list(vault_id=vault_id)
        if isinstance(cred.auth, BetaManagedAgentsEnvironmentVariableAuthResponse)
        and cred.auth.secret_name.startswith("GH_TOKEN")
    }
    for name, token in desired.items():
        if name in existing:
            await anthropic.beta.vaults.credentials.update(
                existing[name],
                vault_id=vault_id,
                auth={"type": "environment_variable", "secret_value": token},
            )
        else:
            await anthropic.beta.vaults.credentials.create(
                vault_id=vault_id,
                auth={
                    "type": "environment_variable",
                    "secret_name": name,
                    "secret_value": token,
                    "networking": {
                        "type": "limited",
                        "allowed_hosts": ["api.github.com", "github.com", "uploads.github.com"],
                    },
                    "injection_location": {"header": True, "body": False},
                },
            )
        if on_mutation is not None:
            on_mutation()
        if on_delivered is not None:
            on_delivered(token)
    for name, credential_id in existing.items():
        if name not in desired:
            await anthropic.beta.vaults.credentials.delete(credential_id, vault_id=vault_id)
            if on_mutation is not None:
                on_mutation()
    if access.working_token is not None:
        await add_github_copilot_credential(
            anthropic, vault_id=vault_id, token=access.working_token, in_place=True
        )
        if on_mutation is not None:
            on_mutation()
        if on_delivered is not None:
            on_delivered(access.working_token)
    else:
        async for cred in anthropic.beta.vaults.credentials.list(vault_id=vault_id):
            if (
                isinstance(cred.auth, BetaManagedAgentsStaticBearerAuthResponse)
                and cred.auth.mcp_server_url == GITHUB_COPILOT_MCP_URL
            ):
                await anthropic.beta.vaults.credentials.delete(cred.id, vault_id=vault_id)
                if on_mutation is not None:
                    on_mutation()


async def finish_app_delivery(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    *,
    access: AppSessionAccess,
    provisional_session_id: str,
    session_id: str,
) -> None:
    async with sessionmaker.begin() as session:
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
        stale = await select_stale_tokens(session)
    stale_ids = {row.token_id for row in stale}
    if any(issued.token_id in stale_ids for issued in access.tokens):
        for issued in access.tokens:
            await revoke_token(client, issued.token)
            async with sessionmaker.begin() as session:
                await mark_revoked(session, token_id=issued.token_id)
        raise ValueError("GitHub access changed during session delivery")


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


async def archive_app_vault(anthropic: AsyncAnthropic, *, vault_id: str) -> None:
    """A repeated cleanup may find a vault archived by an earlier attempt."""
    try:
        await anthropic.beta.vaults.archive(vault_id)
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
        grant_rows = await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
        repos = {repo.repo_id: repo for repo in repo_rows}
        grants = {grant.repo_id: grant for grant in grant_rows}
        working_ids = {grant.repo_id for grant in grant_rows if grant.is_working_repo}
    ordered = sorted(
        issued,
        key=lambda row: (
            row.installation_id,
            "write" if row.permissions.get("contents") == "write" else "read",
            row.repo_ids,
        ),
    )
    tokens: list[AppToken] = []
    resources: list[Resource] = []
    working_token: str | None = None
    mounted = _mounted_repo_ids(
        [repo_id for row in ordered for repo_id in row.repo_ids], working_ids=working_ids
    )
    for group_index, row in enumerate(ordered):
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
        owner = repos[row.repo_ids[0]].repo_full_name.split("/", 1)[0]
        owner_key = "".join(char if char.isalnum() else "_" for char in owner.upper())
        credential_name = f"GH_TOKEN_{owner_key}_{profile.upper()}"
        if any(item.credential_name == credential_name for item in tokens):
            credential_name += f"_{group_index}"
        tokens.append(
            AppToken(
                row.token_id,
                token,
                row.installation_id,
                tuple(row.repo_ids),
                profile,
                credential_name,
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
        tuple(resources), tuple(tokens), working_token or (tokens[0].token if tokens else None)
    )


async def rotate_live_app_tokens(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    is_external: bool,
    vault_id: str,
    resource_ids: dict[str, str],
    config: GithubAppSettings,
    fernet: MultiFernet,
    active_turn: bool = False,
) -> None:
    """Refresh resources and vault in place; leave equal-access old tokens to expire."""
    provisional = f"pending:{uuid.uuid4()}"
    swapped = False

    # Token values MA already holds, in a resource or the vault.
    delivered: set[str] = set()

    def mark_swapped() -> None:
        nonlocal swapped
        swapped = True

    old_access: AppSessionAccess | None = None
    if active_turn:
        try:
            old_access = await _current_app_access(
                sessionmaker,
                session_id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                fernet=fernet,
            )
        except (ValueError, InvalidToken):
            # No rollback snapshot: refresh anyway. A failure still leaves the
            # running turn alone; the next turn boundary replaces the session.
            _log.warning("No App rollback snapshot for active session %s", session_id)
    async with httpx.AsyncClient() as client:
        access = await prepare_app_access(
            sessionmaker,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            is_external=is_external,
            provisional_session_id=provisional,
            config=config,
            fernet=fernet,
            cache=REQUESTER_CACHE,
        )
        try:
            for resource in access.resources:
                if resource["type"] != "github_repository":
                    continue
                resource_id = resource_ids.get(resource["url"])
                if resource_id is None:
                    # MA cannot add a repository to an existing session. The
                    # vault token works for API calls; the next turn replaces
                    # the session to mount the new checkout.
                    continue
                try:
                    await anthropic.beta.sessions.resources.update(
                        resource_id,
                        session_id=session_id,
                        authorization_token=resource["authorization_token"],
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
            )
            await finish_app_delivery(
                sessionmaker,
                client,
                access=access,
                provisional_session_id=provisional,
                session_id=session_id,
            )
            async with sessionmaker.begin() as session:
                await mark_session_tokens_superseded(
                    session,
                    session_id=session_id,
                    except_ids=frozenset(token.token_id for token in access.tokens),
                    now=datetime.now(UTC),
                )
        except Exception:
            kept: frozenset[uuid.UUID] = frozenset()
            try:
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
                        await add_app_credentials(anthropic, vault_id=vault_id, access=old_access)
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
                                await anthropic.beta.sessions.resources.update(
                                    resource_id,
                                    session_id=session_id,
                                    authorization_token=resource["authorization_token"],
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
                    await anthropic.beta.sessions.archive(session_id)
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
                                    for token in access.tokens
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
                        access,
                        tokens=tuple(
                            token for token in access.tokens if token.token_id not in kept
                        ),
                    ),
                )
            raise

"""Session lifecycle — ephemeral per-turn MA sessions.

Sessions are ephemeral artifacts of the Managed Agents API with no local
persistence. Each turn creates a fresh session via the SDK; the returned
BetaManagedAgentsSession is consumed by the turn driver and discarded.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid

import anthropic as anthropic_pkg
import httpx
import structlog
from anthropic import AsyncAnthropic, omit
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.session_create_params import Resource
from cryptography.fernet import MultiFernet
from daimon.core.config import McpSettings
from daimon.core.credential_env import upload_env_and_mount
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_TENANT
from daimon.core.errors import StoreError
from daimon.core.github_credentials import get_pat
from daimon.core.github_repo_auth import resolve_clone_token
from daimon.core.mcp_vault import add_github_copilot_credential, ensure_agent_mcp_vault
from daimon.core.memory_resource import ensure_memory_store_and_mount
from daimon.core.repo_resource import build_repo_resource
from daimon.core.session_context import SessionContext
from daimon.core.stores.agent_repo_binding import get_binding
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

__all__ = ["SessionContext", "create_session"]


async def create_session(
    anthropic: AsyncAnthropic,
    *,
    agent: BetaManagedAgentsAgent,
    environment: BetaEnvironment,
    mcp_settings: McpSettings | None = None,
    account_id: uuid.UUID | None = None,
    session_context: SessionContext | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_uuid: uuid.UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    fernet: MultiFernet | None = None,
    github_fallback_pat: str | None = None,
    github_app_id: str | None = None,
    github_app_private_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> BetaManagedAgentsSession:
    """Create an MA session. Returns the SDK session object directly.

    When ``mcp_settings`` has both ``public_url`` and ``jwt_secret``,
    ``ensure_agent_mcp_vault()`` runs first (idempotent — warm path is a single
    ``vaults.list()`` call) and the per-agent vault id is attached to the session.
    Both ``account_id`` and ``agent_uuid`` are required in that case — no fallback
    to an account-scoped vault.

    ``session_context`` is accepted but no longer threaded into
    ``ensure_agent_mcp_vault`` (deprecated — the vault
    credential is identity-stable and requires no per-turn re-stamp).
    Call-site removal in ``bot.py`` is deferred.

    When ``tenant_id``, ``agent_uuid``, and ``session_factory`` are all
    provided, the agent's tenant-scoped secrets are assembled into a ``.env``
    and mounted as a session ``resources`` entry (``upload_env_and_mount``).
    The ``resources`` kwarg is passed only when the agent actually has
    secrets; otherwise it is omitted entirely. ``resources`` composes
    alongside ``vault_ids`` — it never replaces the vault branch.

    The same gate (``tenant_id`` + ``agent_uuid`` + ``session_factory``) also
    attaches the agent's per-agent memory store via
    ``ensure_memory_store_and_mount`` (lazily provisioned on first use). This
    mount is degrade-not-block: a memory-store provisioning failure
    (``anthropic.APIError`` or ``daimon.core.errors.StoreError``) is logged
    and swallowed rather than propagated — the session is created without
    persistent memory that turn instead of failing outright.

    When the agent has a repo binding, the clone credential is resolved via
    ``daimon.core.github_repo_auth.resolve_clone_token``:
    per-agent PAT wins, else the GitHub App installation token (``github_app_id``
    + ``github_app_private_key``), else the operator ``github_fallback_pat`` for
    a verified-public binding, else the resolver raises ``DaimonError`` — a
    bound repo with no resolvable credential is a loud failure, never a
    silently-omitted clone resource (never an empty ``authorization_token``).
    ``http_client`` is test-injectable; when omitted, a short-lived
    ``httpx.AsyncClient`` is constructed for the resolution.

    On MA failure: ``anthropic.APIError`` propagates uncaught.
    """
    vault_id: str | None = None
    if (
        mcp_settings is not None
        and mcp_settings.public_url is not None
        and mcp_settings.jwt_secret is not None
    ):
        if account_id is None:
            raise ValueError(
                "account_id is required when mcp_settings has public_url and jwt_secret"
            )
        if agent_uuid is None:
            raise ValueError(
                "agent_uuid is required when mcp_settings has public_url and jwt_secret"
            )
        vault_id = await ensure_agent_mcp_vault(
            anthropic,
            account_id=account_id,
            agent_id=agent_uuid,
            jwt_secret=mcp_settings.jwt_secret.get_secret_value().encode(),
            public_url=str(mcp_settings.public_url),
            now=dt.datetime.now(dt.UTC),
        )

    # Dev-agent port: resolve the per-agent GitHub PAT once. It feeds BOTH the
    # github_repository clone resource (below) and the Copilot MCP credential
    # (above the session create). Requires fernet to decrypt it; None when no
    # fernet, no overlay binding, or no stored PAT — all mean "no GitHub".
    per_agent_pat: str | None = None
    if agent_uuid is not None and session_factory is not None and fernet is not None:
        per_agent_pat = await get_pat(
            principal_id=agent_uuid,
            agent_id=agent_uuid,
            sessionmaker=session_factory,
            fernet=fernet,
        )

    # Copilot: mirror the resolved PAT into a static_bearer credential at the
    # GitHub Copilot MCP URL on the agent's vault, so the agent can author PRs
    # via the github MCP toolset. Rides the same
    # vault already attached to the session via vault_ids. Bound to the REAL
    # per-agent identity only — the operator fallback PAT is never mirrored here.
    if vault_id is not None and per_agent_pat is not None:
        await add_github_copilot_credential(anthropic, vault_id=vault_id, token=per_agent_pat)

    resources: list[Resource] = []
    if tenant_id is not None and agent_uuid is not None and session_factory is not None:
        mount = await upload_env_and_mount(
            anthropic, session_factory, tenant_id=tenant_id, agent_id=agent_uuid
        )
        if mount is not None:
            resources.append(mount)

        # Fetch the binding unconditionally — the resolver needs it even when
        # there is no per-agent PAT (App/fallback branches).
        async with session_factory() as session:
            binding = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)
        if binding is not None:
            app_private_key_secret = (
                SecretStr(github_app_private_key) if github_app_private_key is not None else None
            )
            now = int(time.time())
            if http_client is not None:
                clone_token = await resolve_clone_token(
                    http_client,
                    binding=binding,
                    per_agent_pat=per_agent_pat,
                    fallback_pat=github_fallback_pat,
                    app_id=github_app_id,
                    app_private_key=app_private_key_secret,
                    now=now,
                )
            else:
                async with httpx.AsyncClient() as client:
                    clone_token = await resolve_clone_token(
                        client,
                        binding=binding,
                        per_agent_pat=per_agent_pat,
                        fallback_pat=github_fallback_pat,
                        app_id=github_app_id,
                        app_private_key=app_private_key_secret,
                        now=now,
                    )
            repo_resource = build_repo_resource(binding, clone_token)
            if repo_resource is not None:
                resources.append(repo_resource)

        # Memory store (agent memory feature): degrade-not-block. A memory
        # outage must never take down chat — the session just runs without
        # persistent memory this turn.
        try:
            memory_mount = await ensure_memory_store_and_mount(
                anthropic,
                session_factory,
                tenant_id=tenant_id,
                agent_id=agent_uuid,
                agent_name=agent.name,
            )
            resources.append(memory_mount)
        except (anthropic_pkg.APIError, StoreError) as exc:
            _log.warning("memory_store.mount_failed", error=str(exc))

    metadata: dict[str, str] = {}
    if account_id is not None:
        metadata[MA_METADATA_KEY_ACCOUNT] = str(account_id)
    if tenant_id is not None:
        metadata[MA_METADATA_KEY_TENANT] = str(tenant_id)

    return await anthropic.beta.sessions.create(
        agent=agent.id,
        environment_id=environment.id,
        metadata=metadata if metadata else omit,
        vault_ids=[vault_id] if vault_id is not None else omit,
        resources=resources if resources else omit,
    )

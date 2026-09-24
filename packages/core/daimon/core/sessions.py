"""Session lifecycle — ephemeral per-turn MA sessions.

Sessions are ephemeral artifacts of the Managed Agents API with no local
persistence. Each turn creates a fresh session via the SDK; the returned
BetaManagedAgentsSession is consumed by the turn driver and discarded.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

import anthropic as anthropic_pkg
import httpx
import structlog
from anthropic import AsyncAnthropic, omit
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_agent_with_overrides_params import (
    BetaManagedAgentsAgentWithOverridesParams,
)
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from anthropic.types.beta.session_create_params import Agent, Resource
from cryptography.fernet import MultiFernet
from daimon.core.agent_mcp_credentials import (
    mirror_credentials_into_vault,
    resolve_agent_mcp_credentials,
    resolve_hidden_mcp_server_names,
)
from daimon.core.config import McpSettings
from daimon.core.credential_env import upload_env_and_mount
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.errors import StoreError
from daimon.core.github_credentials import get_pat
from daimon.core.github_repo_auth import resolve_clone_token
from daimon.core.mcp_personal_servers import visible_mcp_servers, visible_tools
from daimon.core.mcp_vault import (
    add_github_copilot_credential,
    ensure_agent_mcp_vault,
    hold_agent_vault_lock,
)
from daimon.core.memory_resource import ensure_memory_store_and_mount
from daimon.core.repo_resource import build_repo_resource
from daimon.core.stores.agent_repo_binding import get_binding
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    # Type-only: importing `daimon.core.turn` at runtime would cycle back here
    # through `daimon.core.turn.prepare`.
    from daimon.core.turn.posture import ExemptReason

_log = structlog.get_logger(__name__)

__all__ = ["create_session", "create_isolated_session"]


def _session_metadata(
    *,
    account_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    billing_exempt: ExemptReason | None,
) -> dict[str, str]:
    """The metadata stamp every Daimon-created session carries.

    `daimon_tenant` names the tenant the usage sweep debits; `daimon_account`
    the owning account. `daimon_billing_exempt=<reason>` marks a session
    created for a `BillingExempt` caller, which the sweep skips (the operator
    absorbs its usage). It is decided once, here, from the creator's posture.
    """
    metadata: dict[str, str] = {}
    if account_id is not None:
        metadata[MA_METADATA_KEY_ACCOUNT] = str(account_id)
    if tenant_id is not None:
        metadata[MA_METADATA_KEY_TENANT] = str(tenant_id)
    if billing_exempt is not None:
        metadata[MA_METADATA_KEY_BILLING_EXEMPT] = billing_exempt
    return metadata


async def create_session(
    anthropic: AsyncAnthropic,
    *,
    agent: BetaManagedAgentsAgent,
    environment: BetaEnvironment,
    mcp_settings: McpSettings | None = None,
    account_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_uuid: uuid.UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    fernet: MultiFernet | None = None,
    github_fallback_pat: str | None = None,
    github_app_id: str | None = None,
    github_app_private_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    extra_resources: Sequence[Resource] = (),
    billing_exempt: ExemptReason | None = None,
) -> BetaManagedAgentsSession:
    """Create an MA session. Returns the SDK session object directly.

    When ``mcp_settings`` has both ``public_url`` and ``jwt_secret``,
    ``ensure_agent_mcp_vault()`` runs first (idempotent — warm path is a single
    ``vaults.list()`` call) and the per-agent vault id is attached to the session.
    Both ``account_id``, ``agent_uuid``, and ``session_factory`` are required in
    that case — no fallback to an account-scoped vault. The vault get-or-create
    is serialized per (account_id, agent_id) via a blocking Postgres advisory
    lock (SYNC-01) so concurrent session creations can never orphan a
    duplicate credentialed vault.

    When a per-agent GitHub PAT is resolvable and the vault was ensured, a
    GitHub Copilot MCP credential is mirrored into the vault
    (``add_github_copilot_credential``). This is degrade-not-block: a transient
    ``anthropic.APIError`` on that call is logged and swallowed rather than
    propagated (SYNC-04) — the session still gets created, just without the
    Copilot credential mounted that turn.

    The agent's stored external-MCP credentials are mirrored into the same
    vault on every session create (``resolve_agent_mcp_credentials`` +
    ``mirror_credentials_into_vault``). The servers themselves live on the agent
    spec, so a credential held only in the vault of whoever attached one leaves
    every other caller's turn failing at MCP init; mirroring per session is what
    makes an agent-level server work for an agent-level audience. Unlike the
    Copilot and memory mounts this is NOT degrade-not-block — a missing
    credential hard-fails the turn at MA, so ``anthropic.APIError`` propagates.

    The servers themselves are filtered for this caller before the session
    freezes them: one whose only credential is another person's OAuth grant
    is left out via an ``agent_with_overrides`` ``mcp_servers``/``tools``
    pair (``mcp_personal_servers``). The agent spec is untouched — the people
    who did connect that server still get it. Without the filter MA opened
    the server on every caller's turn, failed it for want of a credential,
    and hung the degraded-turn notice under every reply.

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

    ``extra_resources`` are mounted alongside everything this function
    assembles, for a caller that has a file the new session must start with —
    today, the bundle carrying a replaced session's work. They are passed
    through untouched; this function never inspects or filters them.

    ``billing_exempt`` is the creating caller's ``ExemptReason`` when it runs
    ``BillingExempt``; it is stamped as ``daimon_billing_exempt`` so the usage
    sweep never debits the session to the tenant. ``None`` (every billed
    caller) leaves the session sweepable.

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
        if session_factory is None:
            raise ValueError(
                "session_factory is required when mcp_settings has public_url and jwt_secret"
            )
        vault_id = await ensure_agent_mcp_vault(
            anthropic,
            account_id=account_id,
            agent_id=agent_uuid,
            jwt_secret=mcp_settings.jwt_secret.get_secret_value().encode(),
            public_url=str(mcp_settings.public_url),
            now=dt.datetime.now(dt.UTC),
            session_factory=session_factory,
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
    if (
        vault_id is not None
        and per_agent_pat is not None
        and account_id is not None
        and agent_uuid is not None
        and session_factory is not None
    ):
        # Degrade-not-block: a transient MA failure on this optional credential
        # must not kill the turn. Mirrors the memory-store mount pattern below.
        try:
            async with hold_agent_vault_lock(
                session_factory, account_id=account_id, agent_id=agent_uuid
            ):
                await add_github_copilot_credential(
                    anthropic, vault_id=vault_id, token=per_agent_pat
                )
        except anthropic_pkg.APIError as exc:
            _log.warning(
                "copilot_credential.mount_failed",
                vault_id=vault_id,
                agent_uuid=str(agent_uuid),
                error=str(exc),
            )

    # External MCP servers are attached to the AGENT, so every caller who
    # mentions it gets the toolset — but MA resolves each server's credential
    # from the vault mounted here, which is the CALLER's. Mirror the agent's
    # stored credentials in on every session create (same shape as the Copilot
    # mirror above) or callers who did not personally attach a server fail the
    # whole turn at MCP init. Not degrade-not-block: see
    # mirror_credentials_into_vault.
    if (
        vault_id is not None
        and account_id is not None
        and tenant_id is not None
        and agent_uuid is not None
        and session_factory is not None
        and fernet is not None
    ):
        credentials = await resolve_agent_mcp_credentials(
            sessionmaker=session_factory,
            fernet=fernet,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
        )
        # Locked so a person's OAuth grant replacing a shared token at the same
        # URL never sees this recreate it between its delete and its create.
        async with hold_agent_vault_lock(
            session_factory, account_id=account_id, agent_id=agent_uuid
        ):
            await mirror_credentials_into_vault(
                anthropic, vault_id=vault_id, credentials=credentials
            )

    resources: list[Resource] = list(extra_resources)
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
            _log.warning(
                "memory_store.mount_failed",
                tenant_id=str(tenant_id),
                agent_uuid=str(agent_uuid),
                agent_name=agent.name,
                error=str(exc),
            )

    # A server somebody connected through OAuth authenticates from the
    # connecting person's vault alone, so mounting it on anyone else's session
    # only buys them a failed MCP init and a degraded-turn notice every turn.
    # Overrides keep it off THIS session without touching the agent spec the
    # people who did connect it still answer from.
    agent_argument: Agent = agent.id
    if (
        account_id is not None
        and tenant_id is not None
        and agent_uuid is not None
        and session_factory is not None
    ):
        hidden = await resolve_hidden_mcp_server_names(
            session_factory,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            account_id=account_id,
            server_urls={server.name: server.url for server in agent.mcp_servers},
        )
        if hidden:
            overrides: BetaManagedAgentsAgentWithOverridesParams = {
                "type": "agent_with_overrides",
                "id": agent.id,
                # No `version`: a bare id pins the latest, which is what every
                # other session gets, and both arrays below are full
                # replacements — there is nothing left for a version to pin.
                "mcp_servers": [
                    BetaManagedAgentsURLMCPServerParams(
                        name=server.name, type="url", url=server.url
                    )
                    for server in visible_mcp_servers(agent, hidden)
                ],
                "tools": [
                    cast(Tool, tool.model_dump(mode="json", exclude_none=True))
                    for tool in visible_tools(agent, hidden)
                ],
            }
            agent_argument = overrides
            _log.info(
                "session.personal_mcp_servers_hidden",
                agent_uuid=str(agent_uuid),
                account_id=str(account_id),
                server_names=sorted(hidden),
            )

    metadata = _session_metadata(
        account_id=account_id, tenant_id=tenant_id, billing_exempt=billing_exempt
    )

    return await anthropic.beta.sessions.create(
        agent=agent_argument,
        environment_id=environment.id,
        metadata=metadata if metadata else omit,
        vault_ids=[vault_id] if vault_id is not None else omit,
        resources=resources if resources else omit,
    )


async def create_isolated_session(
    anthropic: AsyncAnthropic,
    *,
    agent: BetaManagedAgentsAgent,
    environment: BetaEnvironment,
    account_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    resources: list[Resource],
    billing_exempt: ExemptReason | None = None,
) -> BetaManagedAgentsSession:
    """Create an MA session for an isolated agent — `create_session` with every
    optional mount removed.

    This is the DEGENERATE sibling of `create_session`, not a smaller version
    of it: it has no `mcp_settings`, no `agent_uuid`, no `session_factory`, no
    `fernet`, and no `github_*` parameter, and it must never grow one "for
    symmetry" — the absent parameters are the security property. It attaches
    no credential vault (so no session-create kwarg for one is passed at
    all — not even the SDK's omit sentinel), mounts no tenant secrets file,
    and provisions no per-agent long-lived store. There is simply no branch
    in this function that could reach for any of those.

    Contrast with `create_session`'s two optional-mount branches: its memory
    mount is degrade-not-block (a provisioning failure is logged and
    swallowed) and its MCP credential mirror hard-fails the turn on error.
    This function has neither branch, and therefore no degrade path at all —
    there is nothing optional left to degrade. A caller gets exactly the
    `resources` it passed and nothing else: no vault, no env file, no memory
    store, no repo. `resources` is passed unconditionally (never omitted);
    an isolated session with an empty resource list is a caller bug, not a
    degraded mode.

    `billing_exempt` stamps the session the same way `create_session` does.

    On MA failure: `anthropic.APIError` propagates uncaught.
    """
    metadata = _session_metadata(
        account_id=account_id, tenant_id=tenant_id, billing_exempt=billing_exempt
    )

    return await anthropic.beta.sessions.create(
        agent=agent.id,
        environment_id=environment.id,
        metadata=metadata if metadata else omit,
        resources=resources,
    )

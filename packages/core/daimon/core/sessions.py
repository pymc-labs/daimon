"""Session lifecycle — ephemeral per-turn MA sessions.

Sessions are ephemeral artifacts of the Managed Agents API with no local
persistence. Each turn creates a fresh session via the SDK; the returned
BetaManagedAgentsSession is consumed by the turn driver and discarded.
"""

from __future__ import annotations

import datetime as dt
import functools
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, cast

import anthropic as anthropic_pkg
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsCustomSkill,
    BetaManagedAgentsSession,
    BetaManagedAgentsSkillParams,
)
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_agent_with_overrides_params import (
    BetaManagedAgentsAgentWithOverridesParams,
)
from anthropic.types.beta.beta_managed_agents_memory_store_resource_param import (
    BetaManagedAgentsMemoryStoreResourceParam,
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
from daimon.core.config import GithubAppSettings, McpSettings
from daimon.core.credential_env import (
    session_env_key,
    upload_env_and_mount,
    upload_git_identity_and_mount,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_PRIVATE_DM,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.errors import StoreError
from daimon.core.github_app_session import (
    REQUESTER_CACHE,
    AppSessionAccess,
    add_app_credentials,
    archive_app_vault,
    create_session_vault,
    declared_app_repositories,
    finish_app_delivery,
    prepare_app_access,
    resolve_bot_identity,
    revoke_app_access,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_personal_servers import visible_mcp_servers, visible_tools
from daimon.core.mcp_vault import (
    GITHUB_COPILOT_MCP_URL,
    ensure_agent_mcp_vault,
    hold_agent_vault_lock,
)
from daimon.core.memory_resource import (
    ensure_memory_store_and_mount,
    ensure_personal_memory_store_and_mount,
    memory_instructions,
    memory_mount,
)
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.core.mux_compat import (
    agent_spec,
    retrieve_environment,
    sdk_agent,
    source_environment_record,
)
from daimon.core.session_ports_compat import (
    archive_session_record,
    create_session_record,
    session_scope,
)
from daimon.core.session_seal import origin_stamp
from daimon.core.session_snapshot import (
    hash_mcp_servers,
    hash_skills,
    session_mcp_servers,
    session_skills,
    session_tools,
    snapshot_from_created_session,
)
from daimon.core.skills.unresolved import (
    report_skill_unavailable,
    skill_unavailable,
    unresolved_skill_id,
)
from daimon.core.stores import (
    agent_files,
    agent_memory_stores,
)
from daimon.core.stores import agent_mcp_credentials as credential_store
from daimon.core.stores.channel_skills import list_skill_channel_rules
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.github_issued_tokens import register_app_session_vault
from daimon.core.tool_safety import OPEN_TOOL_SAFETY, ToolSafetyPolicy
from mux.contracts.ids import Scope
from mux.contracts.resources import ResourceBinding, WorkspaceSource
from mux.drivers.anthropic.resources.agents import UNCLASSIFIED_SOURCE_NAMESPACE, agent_record
from mux.drivers.anthropic.sessions_lifecycle import (
    SessionCreateRequest,
    SessionReads,
)
from mux.errors import ScopeViolation, UnsupportedCapability
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    # Type-only: importing `daimon.core.turn` at runtime would cycle back here
    # through `daimon.core.turn.prepare`.
    from daimon.core.neutral_resources import ResolvedWorkerSpec
    from daimon.core.turn.posture import ExemptReason

_log = structlog.get_logger(__name__)

__all__ = ["create_session", "create_isolated_session"]


async def _session_app_mode(
    session_factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> bool:
    return True


def _caller_mcp_vault_required(
    *,
    app_mode: bool,
    mcp_settings: McpSettings | None,
    account_id: uuid.UUID | None,
    agent_uuid: uuid.UUID | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> bool:
    configured = (
        mcp_settings is not None
        and mcp_settings.public_url is not None
        and mcp_settings.jwt_secret is not None
    )
    return configured and (
        not app_mode
        or (account_id is not None and agent_uuid is not None and session_factory is not None)
    )


def _agent_mcp_mirror_required(
    *,
    vault_available: bool,
    account_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    agent_uuid: uuid.UUID | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    fernet: MultiFernet | None,
) -> bool:
    return (
        vault_available
        and account_id is not None
        and tenant_id is not None
        and agent_uuid is not None
        and session_factory is not None
        and fernet is not None
    )


def _session_mcp_credential(url: str, *, app_mode: bool) -> bool:
    return not app_mode or url != GITHUB_COPILOT_MCP_URL


def _check_resource_source(
    source: BackendAgent,
    agent: BetaManagedAgentsAgent,
    environment: BetaEnvironment,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_scope: Scope,
) -> None:
    actual = source.source
    if (
        actual is None
        or source.tenant_id != tenant_id
        or source.agent_id != agent_uuid
        or agent_uuid != derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)
        or actual.ref.id != agent.id
        or actual.ref.tenant_id != str(tenant_id)
        or actual.revision.native != str(agent.version)
        or actual.native != agent.model_dump(mode="json", exclude_unset=True)
        or resource_scope.tenant_id != str(tenant_id)
        or resource_scope.is_platform
        or resource_scope.is_legacy_host_authorized
        or (account_id is not None and resource_scope.account_id != str(account_id))
        or environment.archived_at is not None
        or (environment.metadata or {}).get(MA_METADATA_KEY_TENANT, str(tenant_id))
        != str(tenant_id)
        or (
            source.environment is not None
            and (
                source.environment.ref.id != environment.id
                or source.environment.ref.kind != "environment"
                or source.environment.ref.provider != actual.ref.provider
                or source.environment.ref.account_scope_id != actual.ref.account_scope_id
                or source.environment.ref.tenant_id != resource_scope.tenant_id
                or source.environment.ref.account_id != resource_scope.account_id
                or source.environment.native
                != environment.model_dump(mode="json", exclude_unset=True)
            )
        )
    ):
        raise ScopeViolation(
            "source-preparation", "source context differs from authorized identity"
        )


async def project_session_resources(
    source: BackendAgent,
    *,
    agent: BetaManagedAgentsAgent,
    environment: BetaEnvironment,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_scope: Scope,
    memory_read_only: bool,
    fernet: MultiFernet | None = None,
    channel_skills: Sequence[BetaManagedAgentsCustomSkill] = (),
    tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY,
    asks_before_publishing: bool = False,
    mcp_settings: McpSettings | None = None,
    agent_github_app: GithubAppSettings | None = None,
    hide_personal_for: uuid.UUID | None = None,
    is_external: bool = False,
    requester_is_headless: bool = False,
    app_session_unmapped: bool = False,
    slack_turn_context_id: uuid.UUID | None = None,
    private_dm_id: str | None = None,
    origin_seal_ids: Collection[str] = (),
    extra_resources: Sequence[Resource] = (),
) -> BackendAgent:
    """Read the existing composer's obligations without resolving or materializing secrets.

    The result describes source intent, never preservation or execution authority.
    Every target mapping remains unproven until the existing preparation seam proves it.
    """
    _check_resource_source(
        source, agent, environment, tenant_id, agent_uuid, account_id, resource_scope
    )
    scope = resource_scope
    app_mode = await _session_app_mode(session_factory, tenant_id=tenant_id, agent_id=agent_uuid)
    caller_vault = _caller_mcp_vault_required(
        app_mode=app_mode,
        mcp_settings=mcp_settings,
        account_id=account_id,
        agent_uuid=agent_uuid,
        session_factory=session_factory,
    )
    if caller_vault and account_id is None:
        raise ScopeViolation("source-preparation", "caller vault has no authorized account")
    async with session_factory() as session:
        keys = (
            ()
            if is_external
            else await agent_files.list_agent_file_revisions(
                session, tenant_id=tenant_id, agent_id=agent_uuid
            )
        )
        credentials = (
            await credential_store.list_credential_revisions(
                session, tenant_id=tenant_id, agent_id=agent_uuid
            )
            if not is_external
            and _agent_mcp_mirror_required(
                vault_available=caller_vault,
                account_id=account_id,
                tenant_id=tenant_id,
                agent_uuid=agent_uuid,
                session_factory=session_factory,
                fernet=fernet,
            )
            else ()
        )
        store_id = await agent_memory_stores.get_memory_store_id(
            session, tenant_id=tenant_id, agent_id=agent_uuid
        )
    hidden = frozenset[str]()
    hiding_for = account_id if account_id is not None else hide_personal_for
    if hiding_for is not None:
        hidden = await resolve_hidden_mcp_server_names(
            session_factory,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            account_id=hiding_for,
            server_urls={server.name: server.url for server in agent.mcp_servers},
            is_external=is_external,
        )
    public_url = (
        str(mcp_settings.public_url)
        if mcp_settings is not None and mcp_settings.public_url is not None
        else None
    )
    tools = session_tools(
        agent,
        hidden,
        tool_safety=tool_safety,
        public_url=public_url,
        asks_before_publishing=asks_before_publishing,
    )
    servers = session_mcp_servers(agent, hidden, tool_safety=tool_safety, public_url=public_url)
    payload = agent.model_dump(
        mode="json", include={"name", "description", "model", "system", "metadata", "multiagent"}
    )
    payload.update(
        tools=[tool.model_dump(mode="json", exclude_none=True) for tool in tools],
        mcp_servers=[server.model_dump(mode="json") for server in servers],
        skills=[skill.model_dump(mode="json") for skill in session_skills(agent, channel_skills)],
    )
    requirements: list[ResourceBinding] = []
    generations: list[tuple[str, dt.datetime]] = []
    for key, updated in keys:
        if session_env_key(key, exclude_github=app_mode):
            identity = f"agent-env:{tenant_id}:{agent_uuid}:{key}"
            requirements.append(
                ResourceBinding(
                    id=identity, kind="credential", target_path=".env", credential_ref=identity
                )
            )
            generations.append((identity, updated))
    for identity, url, updated in credentials:
        if not _session_mcp_credential(url, app_mode=app_mode):
            continue
        reference = f"agent-mcp:{identity}"
        requirements.append(
            ResourceBinding(id=reference, kind="credential", credential_ref=reference)
        )
        generations.append((reference, updated))
    if caller_vault:
        reference = f"caller-mcp:{tenant_id}:{account_id}:{agent_uuid}"
        if slack_turn_context_id is not None:
            reference += f":slack-turn:{slack_turn_context_id}"
        requirements.append(
            ResourceBinding(id=reference, kind="credential", credential_ref=reference)
        )
    repos: list[WorkspaceSource] = []
    if app_mode and not is_external:
        # This owner's requester-access calculation may use network and tokens.
        # Capture its locally declared intent; remote effective access remains unproven.
        repos.extend(
            await declared_app_repositories(
                session_factory, tenant_id=tenant_id, agent_id=agent_uuid
            )
        )
    if app_mode and repos:
        # Without a repository grant, the App vault carries no GitHub source
        # obligation. Caller MCP credentials are captured separately above.
        requirements.append(
            ResourceBinding(id=f"app-session-vault:{tenant_id}:{agent_uuid}", kind="native")
        )
    if (
        extra_resources
        or slack_turn_context_id is not None
        or private_dm_id is not None
        or app_session_unmapped
        or (requester_is_headless and app_mode)
        or origin_seal_ids
        or (app_mode and agent_github_app is not None and repos)
    ):
        # Opaque carry resources and private credentials are not absent obligations.
        requirements.append(ResourceBinding(id="caller-resources", kind="native"))
    effective = agent_spec(payload)
    if source.source is not None:
        # Classify the complete session skill list before neutral projection loses
        # native fields. The driver owns unknown-source detection for both paths.
        session_source = agent_record(
            agent.model_copy(update={"skills": session_skills(agent, channel_skills)}),
            source.source.ref.account_scope_id,
            scope,
        )
        effective = effective.model_copy(
            update={
                "extensions": (
                    *(effective.extensions or ()),
                    *(
                        extension
                        for extension in session_source.spec.extensions or ()
                        if extension.namespace == UNCLASSIFIED_SOURCE_NAMESPACE
                    ),
                )
            }
        )
    return source.model_copy(
        update={
            "effective_spec": effective,
            "environment": source_environment_record(environment, scope),
            "resource_scope": scope,
            "resource_requirements": tuple(requirements),
            "repository_sources": tuple(repos),
            "resource_generations": tuple(generations),
            "asks_before_publishing": asks_before_publishing,
            "memory_required": True,
            "memory_store_id": store_id,
            "memory_read_only": memory_read_only,
            # The projection names only the shared store, so it never promises a
            # personal one beside it.
            "memory_instructions": memory_instructions(
                read_only=memory_read_only, personal_mounted=False
            ),
        }
    )


def _session_metadata(
    *,
    account_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    billing_exempt: ExemptReason | None,
    budget_channel_id: str | None = None,
) -> dict[str, str]:
    """The metadata stamp every Daimon-created session carries.

    `daimon_tenant` names the tenant the usage sweep debits; `daimon_account`
    the owning account. `daimon_billing_exempt=<reason>` marks a session
    created for a `BillingExempt` caller, which the sweep skips (the operator
    absorbs its usage). It is decided once, here, from the creator's posture.
    `daimon_budget_channel` is the channel whose budget the sweep charges.
    """
    metadata: dict[str, str] = {}
    if account_id is not None:
        metadata[MA_METADATA_KEY_ACCOUNT] = str(account_id)
    if tenant_id is not None:
        metadata[MA_METADATA_KEY_TENANT] = str(tenant_id)
    if billing_exempt is not None:
        metadata[MA_METADATA_KEY_BILLING_EXEMPT] = billing_exempt
    if budget_channel_id is not None:
        metadata[MA_METADATA_KEY_BUDGET_CHANNEL] = budget_channel_id
    return metadata


async def _channel_skill_name(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    tenant_id: uuid.UUID | None,
    skill_id: str,
) -> str | None:
    """The name a channel rule recorded for `skill_id`, best-effort (error path only)."""
    if session_factory is None or tenant_id is None:
        return None
    try:
        async with session_factory() as session:
            rows = await list_skill_channel_rules(session, skill_id=skill_id)
    except SQLAlchemyError:
        return None
    return next((row.name for row in rows if row.tenant_id == tenant_id), None)


def native_session_budget_cents(value: object) -> int | None:
    """Compile an explicit positive session budget without decimal rounding."""
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("native session budget requires positive exact USD cents")
    with localcontext() as context:
        context.prec = max(80, len(value.as_tuple().digits) + 2)
        cents = value * Decimal(100)
        if cents != cents.to_integral_value():
            raise ValueError("native session budget requires positive exact USD cents")
        return int(cents)


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
    agent_github_app: GithubAppSettings | None = None,
    is_external: bool = False,
    requester_is_headless: bool = False,
    hide_personal_for: uuid.UUID | None = None,
    app_session_unmapped: bool = False,
    http_client: httpx.AsyncClient | None = None,
    extra_resources: Sequence[Resource] = (),
    billing_exempt: ExemptReason | None = None,
    memory_read_only: bool = False,
    personal_memory: bool = False,
    tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY,
    asks_before_publishing: bool = False,
    confirmed_tools: frozenset[str] = frozenset(),
    slack_turn_context_id: uuid.UUID | None = None,
    private_dm_id: str | None = None,
    budget_channel_id: str | None = None,
    origin_channel_id: str | None = None,
    origin_thread_id: str | None = None,
    origin_seal_ids: Collection[str] = (),
    before_create: Callable[[], Awaitable[None]] | None = None,
    on_app_access: Callable[[AppSessionAccess], None] | None = None,
    allocation: Callable[
        [Callable[[], Awaitable[BetaManagedAgentsSession]], SessionCreateRequest],
        Awaitable[BetaManagedAgentsSession],
    ]
    | None = None,
    channel_skills: Sequence[BetaManagedAgentsCustomSkill] = (),
    model_override: str | None = None,
    resource_source: BackendAgent | None = None,
    worker: ResolvedWorkerSpec | None = None,
    native_budget_usd: Decimal | None = None,
) -> BetaManagedAgentsSession:
    """Create an MA session. Returns the SDK session object directly.

    When ``mcp_settings`` has both ``public_url`` and ``jwt_secret``,
    ``ensure_agent_mcp_vault()`` runs first (refreshing its registered credential
    when needed) and the per-agent vault id is attached to the session.
    Both ``account_id``, ``agent_uuid``, and ``session_factory`` are required in
    that case — no fallback to an account-scoped vault. The vault get-or-create
    is serialized per (account_id, agent_id) via a blocking Postgres advisory
    lock (SYNC-01) so concurrent session creations can never orphan a
    duplicate credentialed vault.

    ``slack_turn_context_id`` selects an isolated execution credential for private
    Slack turns. It is absent for channel, headless and MCP-created sessions.

    ``origin_channel_id``/``origin_thread_id``/``origin_seal_ids`` name the channel
    turn a session is opened for; they are stamped on it so the transcript tools
    can refuse a sealed conversation's transcript outside its channel.

    ``channel_skills`` are the turn's channel's extra skills, added after the
    agent's own (`session_skills`, the shape the drift check hashes too).

    ``model_override`` replaces the model for this session through
    ``agent_with_overrides``. It never changes the stored agent.

    ``budget_channel_id`` is the channel whose budget the session's spend counts
    toward (for a DM, the channel it was moved from). It is stamped apart from
    the origin, so budgeting a DM never puts it under that channel's seal.

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

    ``is_external`` (a person from another organisation) gets neither: no
    ``.env`` and no agent-wide MCP token in their vault, and the servers only
    such a token authenticates are left off their session like another
    person's OAuth server. Their own sign-ins still reach them.

    The same gate (``tenant_id`` + ``agent_uuid`` + ``session_factory``) also
    attaches the agent's per-agent memory store via
    ``ensure_memory_store_and_mount`` (lazily provisioned on first use). This
    mount is degrade-not-block: a memory-store provisioning failure
    (``anthropic.APIError`` or ``daimon.core.errors.StoreError``) is logged
    and swallowed rather than propagated — the session is created without
    persistent memory that turn instead of failing outright.

    ``personal_memory`` also mounts the caller's own personal store for this
    agent (``ensure_personal_memory_store_and_mount``, keyed by ``account_id``),
    with the same access as the shared one and the same degrade-not-block
    handling. Only a caller whose session belongs to one verified person
    passes it: an interactive chat turn (keyed on the caller's account), a hub
    `ask`/`start_turn` (platform sign-in, owner-only handles) and a routine
    whose result reaches only its owner (`rule_views.routine_answers_only_owner`,
    read-only). Every other caller (agent keys, operator and CLI tokens, routines
    with a destination) leaves it off and mounts the shared store alone, with
    instructions that forbid recording personal facts.

    GitHub repository resources and credentials come from the agent's usable
    GitHub App grants. ``prepare_app_access`` mints installation tokens and
    mounts the granted repositories when an App vault is available.

    ``extra_resources`` are mounted alongside everything this function
    assembles, for a caller that has a file the new session must start with —
    today, the bundle carrying a replaced session's work. They are passed
    through untouched; this function never inspects or filters them.

    ``tool_safety`` is the deployment's attached-tool policy. When enabled,
    every gated third-party toolset is sent as ``always_ask`` through the same
    ``agent_with_overrides`` the personal-server filter uses, whatever the
    agent itself stores. Only the built-in ``daimon-mcp`` is exempt, and only
    as the deployment's own endpoint: an entry carrying that name with any
    other URL is re-pointed at ``mcp_settings.public_url`` for this session
    (or gated like a third party when no public URL is configured).
    Disabled (the default) sends the agent unchanged.

    ``billing_exempt`` is the creating caller's ``ExemptReason`` when it runs
    ``BillingExempt``; it is stamped as ``daimon_billing_exempt`` so the usage
    sweep never debits the session to the tenant. ``None`` (every billed
    caller) leaves the session sweepable.

    On MA failure: ``anthropic.APIError`` propagates uncaught.
    """
    if worker is not None and native_budget_usd is not None:
        raise ScopeViolation("session-budget", "root and worker controls cannot be combined")
    budget_cents = native_session_budget_cents(
        worker.limits.native_budget_usd if worker is not None else native_budget_usd
    )
    if native_budget_usd is not None and (
        before_create is None or allocation is None or tenant_id is None or account_id is None
    ):
        raise ScopeViolation("session-budget", "owned funding and allocation required")
    scope = session_scope(
        tenant_id=tenant_id, account_id=account_id, call_site="sessions:create_session"
    )
    resource_agent = agent
    if resource_source is not None or worker is not None:
        from daimon.core.turn.runtime_manifest import worker_source_ceiling

        if (
            resource_source is None
            or worker is None
            or before_create is None
            or tenant_id is None
            or account_id is None
            or agent_uuid is None
            or session_factory is None
            or resource_source.resource_scope is None
            or resource_source.source is None
            or resource_source.source.native is None
            or resource_source.environment is None
            or resource_source.memory_required is not True
            or resource_source.memory_read_only is None
            or resource_source.resource_scope != worker.permissions.fleet.scope
            or (resource_source.memory_read_only and not memory_read_only)
            or (worker.permissions.memory_read_only and not memory_read_only)
        ):
            raise ScopeViolation(
                "worker-resources", "captured source and owned preparation required"
            )
        if worker.declared_drops:
            raise UnsupportedCapability(("native_worker_resource_drop",), worker.backend.profile)
        resource_agent = sdk_agent(resource_source.source)
        scope = resource_source.resource_scope
        _check_resource_source(
            resource_source, resource_agent, environment, tenant_id, agent_uuid, account_id, scope
        )
        # Source authority is refreshed before resource effects and at the existing final fence.
        await before_create()
        if (
            resource_source.memory_store_id is None
            or worker_source_ceiling(resource_source).resource_requirements
            or resource_source.repository_sources
            or extra_resources
        ):
            raise UnsupportedCapability(
                ("native_worker_resource_projection",), worker.backend.profile
            )
        expected = memory_mount(resource_source.memory_store_id, read_only=memory_read_only)
        if len(worker.resources) != 1:
            raise ScopeViolation("worker-resources", "one granted warm memory mount required")
        binding = worker.resources[0]
        native, ref = binding.native, binding.resource
        if (
            native is None
            or native.namespace != "anthropic.session_resource_create"
            or native.version != 1
            or binding.kind != "native"
            or ref is None
            or binding.target_path is not None
            or binding.credential_ref is not None
            or ref.kind != "memory_store"
            or ref.id != resource_source.memory_store_id
            or ref.provider != resource_source.source.ref.provider
            or (ref.tenant_id, ref.account_id) != (scope.tenant_id, scope.account_id)
            or native.value != {"resource": expected}
        ):
            raise ScopeViolation("worker-resources", "grant differs from source memory intent")
    app_mode = False
    if tenant_id is not None and agent_uuid is not None and session_factory is not None:
        app_mode = await _session_app_mode(
            session_factory, tenant_id=tenant_id, agent_id=agent_uuid
        )
    caller_vault = worker is None and _caller_mcp_vault_required(
        app_mode=app_mode,
        mcp_settings=mcp_settings,
        account_id=account_id,
        agent_uuid=agent_uuid,
        session_factory=session_factory,
    )
    vault_id: str | None = None
    caller_vault_id: str | None = None
    try:
        if app_mode:
            assert tenant_id is not None and agent_uuid is not None and session_factory is not None
            if not is_external and await declared_app_repositories(
                session_factory, tenant_id=tenant_id, agent_id=agent_uuid
            ):
                vault_id = await create_session_vault(
                    anthropic,
                    tenant_id=tenant_id,
                    sessionmaker=session_factory,
                    agent_id=agent_uuid,
                    account_id=account_id,
                    public_url=(
                        str(mcp_settings.public_url)
                        if mcp_settings is not None and mcp_settings.public_url is not None
                        else None
                    ),
                    jwt_secret=(
                        mcp_settings.jwt_secret.get_secret_value().encode()
                        if mcp_settings is not None and mcp_settings.jwt_secret is not None
                        else None
                    ),
                )
            if caller_vault:
                assert account_id is not None and session_factory is not None
                assert (
                    mcp_settings is not None
                    and mcp_settings.public_url is not None
                    and mcp_settings.jwt_secret is not None
                )
                caller_vault_id = await ensure_agent_mcp_vault(
                    anthropic,
                    account_id=account_id,
                    agent_id=agent_uuid,
                    jwt_secret=mcp_settings.jwt_secret.get_secret_value().encode(),
                    public_url=str(mcp_settings.public_url),
                    now=dt.datetime.now(dt.UTC),
                    session_factory=session_factory,
                    slack_turn_context_id=slack_turn_context_id,
                    scope=scope,
                )
        elif caller_vault:
            assert (
                mcp_settings is not None
                and mcp_settings.public_url is not None
                and mcp_settings.jwt_secret is not None
            )
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
                slack_turn_context_id=slack_turn_context_id,
                scope=scope,
            )

        # External MCP servers are attached to the AGENT, so every caller who
        # mentions it gets the toolset — but MA resolves each server's credential
        # from the vault mounted here, which is the CALLER's. Mirror the agent's
        # stored credentials in on every session create (same shape as the Copilot
        # mirror above) or callers who did not personally attach a server fail the
        # whole turn at MCP init. Not degrade-not-block: see
        # mirror_credentials_into_vault. Never for a person from another
        # organisation: those servers come off their session instead.
        credential_vault_id = caller_vault_id if app_mode else vault_id
        if not is_external and _agent_mcp_mirror_required(
            vault_available=credential_vault_id is not None,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_uuid=agent_uuid,
            session_factory=session_factory,
            fernet=fernet,
        ):
            assert (
                credential_vault_id is not None and account_id is not None and tenant_id is not None
            )
            assert agent_uuid is not None and session_factory is not None and fernet is not None
            credentials = await resolve_agent_mcp_credentials(
                sessionmaker=session_factory,
                fernet=fernet,
                tenant_id=tenant_id,
                agent_id=agent_uuid,
            )
            credentials = tuple(
                credential
                for credential in credentials
                if _session_mcp_credential(credential.mcp_server_url, app_mode=app_mode)
            )
            # Locked so a person's OAuth grant replacing a shared token at the same
            # URL never sees this recreate it between its delete and its create.
            async with hold_agent_vault_lock(
                session_factory, account_id=account_id, agent_id=agent_uuid
            ):
                await mirror_credentials_into_vault(
                    anthropic,
                    vault_id=credential_vault_id,
                    credentials=credentials,
                    scope=scope,
                )

        resources: list[Resource] = list(extra_resources)
        app_access: AppSessionAccess | None = None
        provisional_session_id = f"pending:{uuid.uuid4()}"
        if tenant_id is not None and agent_uuid is not None and session_factory is not None:
            mount = (
                None
                if is_external
                else await upload_env_and_mount(
                    anthropic,
                    session_factory,
                    tenant_id=tenant_id,
                    agent_id=agent_uuid,
                    exclude_github=app_mode,
                )
            )
            if mount is not None:
                resources.append(mount)

            # Memory stores (agent memory feature): degrade-not-block. A memory
            # outage must never take down chat — the session just runs without
            # persistent memory this turn. The personal store is resolved
            # first because the shared store's instructions depend on it. A fleet
            # worker mounts only its source's captured warm store, never a person's.
            personal_mount: BetaManagedAgentsMemoryStoreResourceParam | None = None
            if personal_memory and account_id is not None and resource_source is None:
                try:
                    personal_mount = await ensure_personal_memory_store_and_mount(
                        anthropic,
                        session_factory,
                        tenant_id=tenant_id,
                        agent_id=agent_uuid,
                        account_id=account_id,
                        agent_name=agent.name,
                        read_only=memory_read_only,
                    )
                except (anthropic_pkg.APIError, StoreError) as exc:
                    _log.warning(
                        "memory_store.personal_mount_failed",
                        tenant_id=str(tenant_id),
                        agent_uuid=str(agent_uuid),
                        agent_name=agent.name,
                        error=str(exc),
                    )
            try:
                memory_resource = await ensure_memory_store_and_mount(
                    anthropic,
                    session_factory,
                    tenant_id=tenant_id,
                    agent_id=agent_uuid,
                    agent_name=resource_agent.name,
                    read_only=memory_read_only,
                    personal_mounted=personal_mount is not None,
                    expected_memory_store_id=resource_source.memory_store_id
                    if resource_source is not None
                    else None,
                )
                resources.append(memory_resource)
            except (anthropic_pkg.APIError, StoreError) as exc:
                if resource_source is not None:
                    raise
                _log.warning(
                    "memory_store.mount_failed",
                    tenant_id=str(tenant_id),
                    agent_uuid=str(agent_uuid),
                    agent_name=resource_agent.name,
                    error=str(exc),
                )
            if personal_mount is not None:
                resources.append(personal_mount)

        # A server somebody connected through OAuth authenticates from the
        # connecting person's vault alone, so mounting it on anyone else's session
        # only buys them a failed MCP init and a degraded-turn notice every turn.
        # Overrides keep it off THIS session without touching the agent spec the
        # people who did connect it still answer from.
        #
        # The same override carries the deployment's tool-safety policy: gated
        # third-party toolsets are sent as `always_ask` whatever the agent itself
        # stores, so every new session is gated however the agent was written.
        agent_argument: Agent = agent.id
        hidden: frozenset[str] = frozenset()
        # A shared thread's session has no account; its synthetic owner, who
        # connected nothing, hides every server someone connected personally.
        hiding_for = account_id if account_id is not None else hide_personal_for
        if (
            hiding_for is not None
            and tenant_id is not None
            and agent_uuid is not None
            and session_factory is not None
        ):
            hidden = await resolve_hidden_mcp_server_names(
                session_factory,
                tenant_id=tenant_id,
                agent_id=agent_uuid,
                account_id=hiding_for,
                server_urls={server.name: server.url for server in agent.mcp_servers},
                is_external=is_external,
            )
            if hidden:
                _log.info(
                    "session.personal_mcp_servers_hidden",
                    agent_uuid=str(agent_uuid),
                    account_id=str(hiding_for),
                    server_names=sorted(hidden),
                )
        public_url = (
            str(mcp_settings.public_url)
            if mcp_settings is not None and mcp_settings.public_url is not None
            else None
        )
        tools = session_tools(
            agent,
            hidden,
            tool_safety=tool_safety,
            public_url=public_url,
            asks_before_publishing=asks_before_publishing,
            confirmed_tools=confirmed_tools,
        )
        servers = session_mcp_servers(
            agent,
            hidden,
            tool_safety=tool_safety,
            public_url=public_url,
            confirmed_tools=confirmed_tools,
        )
        gated = list(tools) != list(visible_tools(agent, hidden))
        healed = list(servers) != list(visible_mcp_servers(agent, hidden))
        if hidden or gated or healed or channel_skills or model_override is not None:
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
                    for server in servers
                ],
                "tools": [
                    cast(Tool, tool.model_dump(mode="json", exclude_none=True)) for tool in tools
                ],
            }
            if model_override is not None:
                overrides["model"] = model_override
            if channel_skills:
                # A full replacement too: the agent's own at the versions it pins.
                overrides["skills"] = [
                    cast(BetaManagedAgentsSkillParams, skill.model_dump(mode="json"))
                    for skill in session_skills(agent, channel_skills)
                ]
                _log.info("session.channel_skills_applied", agent_id=agent.id)
            agent_argument = overrides
            if healed:
                # The reserved name on a server daimon does not run: the tool-safety
                # exemption is for the real endpoint only, so this session gets it.
                _log.warning("session.reserved_mcp_server_healed", agent_id=agent.id)
            if gated:
                _log.info("session.tool_safety_applied", agent_id=agent.id)

        metadata = _session_metadata(
            account_id=account_id,
            tenant_id=tenant_id,
            billing_exempt=billing_exempt,
            budget_channel_id=budget_channel_id,
        )
        if slack_turn_context_id is not None:
            metadata[MA_METADATA_KEY_PRIVATE_DM] = str(slack_turn_context_id)
        elif private_dm_id is not None:
            metadata[MA_METADATA_KEY_PRIVATE_DM] = private_dm_id
        if origin_channel_id is not None:
            metadata.update(
                origin_stamp(
                    channel_id=origin_channel_id,
                    thread_id=origin_thread_id,
                    seal=origin_seal_ids,
                )
            )

        created: BetaManagedAgentsSession | None = None
        owned_created = False
        observed = None
        try:
            if app_mode and vault_id is not None:
                assert (
                    tenant_id is not None and agent_uuid is not None and session_factory is not None
                )
                async with httpx.AsyncClient() as app_client:
                    app_access = await prepare_app_access(
                        session_factory,
                        app_client,
                        tenant_id=tenant_id,
                        agent_id=agent_uuid,
                        account_id=None if requester_is_headless else account_id,
                        is_external=is_external,
                        provisional_session_id=provisional_session_id,
                        config=agent_github_app or GithubAppSettings(),
                        fernet=fernet,
                        cache=REQUESTER_CACHE,
                    )
                if on_app_access is not None:
                    on_app_access(app_access)
                resources.extend(app_access.resources)
                if app_access.tokens:
                    # Degrade-not-block: without it, commits just carry no preset author.
                    try:
                        async with httpx.AsyncClient() as identity_client:
                            bot_identity = await resolve_bot_identity(
                                identity_client,
                                slug=(agent_github_app or GithubAppSettings()).app_slug,
                            )
                        if bot_identity is not None:
                            resources.append(
                                await upload_git_identity_and_mount(
                                    anthropic, session_factory, identity=bot_identity, scope=scope
                                )
                            )
                    except anthropic_pkg.APIError as exc:
                        _log.warning("git_identity.mount_failed", error=str(exc))
                await add_app_credentials(
                    anthropic, vault_id=vault_id, access=app_access, scope=scope
                )
            if before_create is not None:
                # The caller's last access decision, after every await above.
                await before_create()
            create = functools.partial(
                create_session_record,
                anthropic,
                scope=scope,
                agent=agent_argument,
                environment_id=environment.id,
                metadata=metadata if metadata else None,
                vault_ids=(
                    [caller_vault_id, vault_id]
                    if caller_vault_id is not None and vault_id is not None
                    else [vault_id]
                    if vault_id is not None
                    else [caller_vault_id]
                    if caller_vault_id is not None
                    else None
                ),
                resources=resources if resources else None,
            )
            if budget_cents is not None:
                create.keywords["budget"] = {
                    "type": "limit",
                    "max_list_cost": {"amount": str(budget_cents), "currency": "USD"},
                }
            try:
                created = (
                    await allocation(
                        create,
                        SessionCreateRequest.model_validate(
                            {key: value for key, value in create.keywords.items() if key != "scope"}
                        ),
                    )
                    if allocation is not None
                    else await create()
                )
            except anthropic_pkg.BadRequestError as error:
                unresolved = unresolved_skill_id(error)
                if unresolved is None:
                    raise
                # Every turn fails this way until the skill is detached: say which.
                _log.warning(
                    "session.skill_unresolved",
                    agent_id=agent.id,
                    skill_id=unresolved[0],
                    version=unresolved[1],
                )
                raise skill_unavailable(
                    skill_id=unresolved[0],
                    agent=agent,
                    channel_skills=channel_skills,
                    channel_skill_name=await _channel_skill_name(
                        session_factory, tenant_id=tenant_id, skill_id=unresolved[0]
                    ),
                ) from error
            if resource_source is not None or native_budget_usd is not None:
                assert tenant_id is not None and account_id is not None
                backend = managed_agents(
                    anthropic, scope=scope, resources=frozenset({("session", created.id)})
                )
                reader = backend.extension(
                    SessionReads, namespace="anthropic.session_reads", version=1
                )
                observed = await reader.read_native(
                    scope, resource_ref(backend, "session", created.id, scope=scope)
                )
                native_agent, native_metadata, native_resources = (
                    observed.get(name) for name in ("agent", "metadata", "resources")
                )
                owned_created = (
                    observed.get("id") == created.id
                    and isinstance(native_agent, dict)
                    and native_agent.get("id") == agent.id
                    and observed.get("environment_id") == environment.id
                    and isinstance(native_metadata, dict)
                    and native_metadata.get(MA_METADATA_KEY_TENANT) == str(tenant_id)
                    and native_metadata.get(MA_METADATA_KEY_ACCOUNT) == str(account_id)
                )
                if "budget" in create.keywords and (
                    created.model_dump(mode="json").get("budget") != create.keywords["budget"]
                    or observed.get("budget") != create.keywords["budget"]
                ):
                    raise ScopeViolation("session-budget", "native session budget differs")
                if not owned_created or observed.get("archived_at") is not None:
                    raise ScopeViolation("session-budget", "native session ownership differs")
                if native_budget_usd is not None:
                    assert before_create is not None
                    await before_create()
            if resource_source is not None:
                from mux.drivers.anthropic.resources.agents import native_tools_match

                assert observed is not None
                native_agent, native_resources = (
                    observed.get(name) for name in ("agent", "resources")
                )
                assert worker is not None and tenant_id is not None and account_id is not None
                assert resource_source.memory_store_id is not None
                assert resource_source.memory_read_only is not None
                assert (
                    agent_uuid is not None
                    and session_factory is not None
                    and before_create is not None
                )
                expected = memory_mount(resource_source.memory_store_id, read_only=memory_read_only)
                scalars = ("id", "model", "system", "version", "multiagent")
                fields = {*scalars, "tools", "mcp_servers", "skills"}
                intent = agent.model_dump(mode="json")
                if isinstance(agent_argument, dict):
                    intent.update(
                        {key: value for key, value in agent_argument.items() if key in fields}
                    )
                    if isinstance(intent["model"], str):
                        intent["model"] = {"id": intent["model"]}
                try:
                    wanted = type(agent).model_validate(intent)
                    for frozen in (
                        created.model_dump(mode="json", exclude_unset=True).get("agent"),
                        native_agent,
                    ):
                        if not isinstance(frozen, dict):
                            raise ValueError("missing frozen agent")
                        actual = type(agent).model_validate(
                            {**intent, **{key: frozen[key] for key in fields}}
                        )
                        if (
                            any(getattr(actual, key) != getattr(wanted, key) for key in scalars)
                            or not native_tools_match(
                                [
                                    tool.model_dump(mode="json", exclude_none=True)
                                    for tool in wanted.tools
                                ],
                                [
                                    tool.model_dump(mode="json", exclude_none=True)
                                    for tool in actual.tools
                                ],
                            )
                            or hash_mcp_servers(actual.mcp_servers)
                            != hash_mcp_servers(wanted.mcp_servers)
                            or hash_skills(actual.skills) != hash_skills(wanted.skills)
                        ):
                            raise ValueError("frozen execution differs")
                except (ValueError, KeyError, TypeError):
                    raise ScopeViolation(
                        "worker-resources", "frozen execution differs from compiled intent"
                    ) from None
                fresh_environment = await retrieve_environment(
                    anthropic, environment.id, scope=scope
                )
                recaptured = await project_session_resources(
                    resource_source,
                    agent=resource_agent,
                    environment=fresh_environment,
                    session_factory=session_factory,
                    tenant_id=tenant_id,
                    agent_uuid=agent_uuid,
                    account_id=account_id,
                    resource_scope=scope,
                    memory_read_only=resource_source.memory_read_only,
                    fernet=fernet,
                    channel_skills=channel_skills,
                    tool_safety=tool_safety,
                    asks_before_publishing=asks_before_publishing,
                    mcp_settings=mcp_settings,
                    agent_github_app=agent_github_app,
                    hide_personal_for=hide_personal_for,
                    is_external=is_external,
                    requester_is_headless=requester_is_headless,
                    app_session_unmapped=app_session_unmapped,
                    slack_turn_context_id=slack_turn_context_id,
                    private_dm_id=private_dm_id,
                    origin_seal_ids=origin_seal_ids,
                    extra_resources=extra_resources,
                )
                if (
                    not owned_created
                    or observed.get("archived_at") is not None
                    or not isinstance(native_resources, list)
                    or len(native_resources) != 1
                    or not isinstance(native_resources[0], dict)
                    or any(native_resources[0].get(key) != value for key, value in expected.items())
                    or recaptured != resource_source
                ):
                    raise ScopeViolation(
                        "worker-resources", "native source resource readback differs"
                    )
                await before_create()
            if app_access is not None:
                assert (
                    session_factory is not None and tenant_id is not None and vault_id is not None
                )
                snapshot = (
                    snapshot_from_created_session(
                        created,
                        env_sha256=None,
                        env_file_id=None,
                        repo_token_issued_at=None,
                        vault_id=vault_id,
                        sent_skills=session_skills(agent, channel_skills),
                        github_mode="app",
                        is_external=is_external,
                    )
                    if app_session_unmapped
                    else None
                )
                async with session_factory.begin() as session:
                    await register_app_session_vault(
                        session,
                        session_id=created.id,
                        tenant_id=tenant_id,
                        vault_id=vault_id,
                        is_unmapped=requester_is_headless or app_session_unmapped,
                        is_mcp=app_session_unmapped,
                        is_external=is_external,
                        agent_id=agent_uuid if app_session_unmapped else None,
                        account_id=account_id if app_session_unmapped else None,
                        repo_urls=snapshot.repo_urls if snapshot is not None else (),
                        repo_resource_ids=(
                            snapshot.repo_resource_ids if snapshot is not None else None
                        ),
                    )
                async with httpx.AsyncClient() as app_client:
                    await finish_app_delivery(
                        session_factory,
                        app_client,
                        access=app_access,
                        provisional_session_id=provisional_session_id,
                        session_id=created.id,
                    )
            return created
        except BaseException:
            if created is not None and owned_created:
                from daimon.core.turn.run import (
                    _archive_orphaned_session,  # pyright: ignore[reportPrivateUsage]
                )
                from mux.drivers.anthropic.transport import single_attempt_mutation

                with single_attempt_mutation():
                    await _archive_orphaned_session(anthropic, session_id=created.id, scope=scope)
            elif (
                created is not None
                and app_mode
                and resource_source is None
                and native_budget_usd is None
            ):
                await archive_session_record(
                    anthropic,
                    created.id,
                    scope=scope,
                )
            if app_access is not None:
                assert session_factory is not None
                async with httpx.AsyncClient() as app_client:
                    await revoke_app_access(session_factory, app_client, app_access)
            raise
    except BaseException:
        if app_mode and vault_id is not None:
            await archive_app_vault(anthropic, vault_id=vault_id, scope=scope)
        raise


async def create_isolated_session(
    anthropic: AsyncAnthropic,
    *,
    agent: BetaManagedAgentsAgent,
    environment: BetaEnvironment,
    account_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    resources: list[Resource],
    billing_exempt: ExemptReason | None = None,
    memory_read_only: bool = False,
    budget_channel_id: str | None = None,
    origin_channel_id: str | None = None,
    origin_seal_ids: Collection[str] = (),
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

    `billing_exempt`, `budget_channel_id` and the origin stamp the session the
    same way `create_session` does: metadata only, never a mount.

    On MA failure: `anthropic.APIError` propagates, except a skill the agent
    pins that no longer exists, which raises `SkillUnavailable` telling the
    reader to have the report re-published.
    """
    metadata = _session_metadata(
        account_id=account_id,
        tenant_id=tenant_id,
        billing_exempt=billing_exempt,
        budget_channel_id=budget_channel_id,
    )
    if origin_channel_id is not None:
        metadata.update(
            origin_stamp(channel_id=origin_channel_id, thread_id=None, seal=origin_seal_ids)
        )

    try:
        return await create_session_record(
            anthropic,
            scope=session_scope(
                tenant_id=tenant_id,
                account_id=account_id,
                call_site="sessions:create_isolated_session",
            ),
            agent=agent.id,
            environment_id=environment.id,
            metadata=metadata if metadata else None,
            resources=resources,
        )
    except anthropic_pkg.BadRequestError as error:
        unresolved = unresolved_skill_id(error)
        if unresolved is None:
            raise
        _log.warning("session.skill_unresolved", agent_id=agent.id, skill_id=unresolved[0])
        raise report_skill_unavailable(unresolved[0]) from error

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
from typing import TYPE_CHECKING, cast

import anthropic as anthropic_pkg
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
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
from daimon.core.caller_resources import (
    APP_SESSION_VAULT_PREFIX,
    CALLER_RESOURCES_ID,
    CALLER_VAULT_PREFIX,
)
from daimon.core.config import GithubAppSettings, McpSettings
from daimon.core.credential_env import (
    github_repo_map_sha256,
    github_repo_script_sha256,
    session_env_key,
    upload_env_and_mount,
    upload_git_identity_and_mount,
    upload_github_repo_helpers_and_mount,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_BILLING_EXEMPT,
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_CONTEXT_ISOLATED,
    MA_METADATA_KEY_PRIVATE_DM,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.errors import StoreError
from daimon.core.github_app_session import (
    REQUESTER_CACHE,
    AppSessionAccess,
    EffectiveAppGrantView,
    add_app_credentials,
    archive_app_vault,
    create_session_vault,
    declared_app_repositories,
    effective_app_grant_view,
    finish_app_delivery,
    prepare_app_access,
    repo_credential_entries,
    resolve_bot_identity,
    revoke_app_access,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_hold import hold_key
from daimon.core.mcp_identity import RelayBinding, identity_relay_urls, relay_url
from daimon.core.mcp_personal_servers import visible_mcp_servers, visible_tools
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.mcp_vault import (
    GITHUB_COPILOT_MCP_URL,
    create_relay_vault,
    ensure_agent_mcp_vault,
    hold_agent_vault_lock,
)
from daimon.core.memory_resource import (
    ensure_channel_memory_store_and_mount,
    ensure_memory_store_and_mount,
    ensure_personal_memory_store_and_mount,
    memory_instructions,
    memory_scope,
)
from daimon.core.mux_compat import agent_spec, archive_vault, source_environment_record
from daimon.core.session_ports_compat import (
    archive_session_record,
    create_session_record,
    session_scope,
)
from daimon.core.session_seal import origin_stamp
from daimon.core.session_snapshot import (
    MaSkill,
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
from daimon.core.stores import agent_files, agent_memory_stores
from daimon.core.stores import agent_mcp_credentials as credential_store
from daimon.core.stores.channel_skills import list_skill_channel_rules
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.github_issued_tokens import register_app_session_vault
from daimon.core.tool_safety import OPEN_TOOL_SAFETY, ToolSafetyPolicy
from daimon.core.turn_hold import TurnHold
from mux.contracts.ids import Scope
from mux.contracts.resources import ResourceBinding, WorkspaceSource
from mux.drivers.anthropic import (
    UNCLASSIFIED_SOURCE_NAMESPACE,
    agent_record,
)
from mux.errors import ScopeViolation
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
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
    channel_skills: Sequence[MaSkill] = (),
    tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY,
    asks_before_publishing: bool = False,
    mcp_settings: McpSettings | None = None,
    agent_github_app: GithubAppSettings | None = None,
    hide_personal_for: uuid.UUID | None = None,
    requester_is_headless: bool = False,
    app_session_unmapped: bool = False,
    slack_turn_context_id: uuid.UUID | None = None,
    hold: TurnHold | None = None,
    private_dm_id: str | None = None,
    origin_seal_ids: Collection[str] = (),
    extra_resources: Sequence[Resource] = (),
) -> BackendAgent:
    """Read the existing composer's obligations without resolving or materializing secrets.

    The result describes source intent, never preservation or execution authority.
    Every target mapping remains unproven until the existing preparation seam proves it.
    """
    if hold is not None:
        raise ScopeViolation("source-preparation", "a held turn cannot capture an unheld source")
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
        keys = await agent_files.list_agent_file_revisions(
            session, tenant_id=tenant_id, agent_id=agent_uuid
        )
        credentials = (
            await credential_store.list_credential_revisions(
                session, tenant_id=tenant_id, agent_id=agent_uuid
            )
            if _agent_mcp_mirror_required(
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
        reference = f"{CALLER_VAULT_PREFIX}{tenant_id}:{account_id}:{agent_uuid}"
        if slack_turn_context_id is not None:
            reference += f":slack-turn:{slack_turn_context_id}"
        if hold is not None:
            reference += f":hold:{hold_key(hold)}"
        requirements.append(
            ResourceBinding(id=reference, kind="credential", credential_ref=reference)
        )
    repos: list[WorkspaceSource] = []
    if app_mode:
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
            ResourceBinding(id=f"{APP_SESSION_VAULT_PREFIX}{tenant_id}:{agent_uuid}", kind="native")
        )
    if (
        extra_resources
        or slack_turn_context_id is not None
        or (hold is not None and caller_vault)
        or private_dm_id is not None
        or app_session_unmapped
        or (requester_is_headless and app_mode)
        or origin_seal_ids
        or (app_mode and agent_github_app is not None and repos)
    ):
        # Opaque carry resources and private credentials are not absent obligations.
        requirements.append(ResourceBinding(id=CALLER_RESOURCES_ID, kind="native"))
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
    daimon_rule_tools: frozenset[str] = frozenset(),
    slack_turn_context_id: uuid.UUID | None = None,
    hold: TurnHold | None = None,
    memory_hold: TurnHold | None = None,
    private_dm_id: str | None = None,
    budget_channel_id: str | None = None,
    origin_channel_id: str | None = None,
    origin_thread_id: str | None = None,
    origin_seal_ids: Collection[str] = (),
    before_create: Callable[[], Awaitable[None]] | None = None,
    on_app_access: Callable[[AppSessionAccess], None] | None = None,
    channel_skills: Sequence[MaSkill] = (),
    model_override: str | None = None,
    system_override: str | None = None,
    relay_session_key: uuid.UUID | None = None,
    isolated_relay_names: frozenset[str] = frozenset(),
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

    ``hold`` makes the session a held turn's: it mounts that hold's own Daimon
    credential vault (`ensure_agent_mcp_vault`), and the GitHub-token vault, which
    carries a Daimon credential too, gets the same hold. Without a caller
    account (a shared thread) there is no Daimon credential and so no hold to carry.

    ``memory_hold`` is the held place whose memory the session mounts: the
    channel's own store (``ensure_channel_memory_store_and_mount``), read-write
    unless ``memory_read_only``, and neither the agent-wide store nor a
    personal one. It is the turn's hold whether or not a credential carries it,
    so a shared thread (no caller account, hence no ``hold``) still mounts the
    channel's memory.

    ``origin_channel_id``/``origin_thread_id``/``origin_seal_ids`` name the channel
    turn a session is opened for; they are stamped on it so the transcript tools
    can refuse a sealed conversation's transcript outside its channel.

    ``channel_skills`` are the turn's extra skills (the platform's, then the
    channel's), added after the agent's own (`session_skills`, the shape the
    drift check hashes too).

    ``model_override`` replaces the model for this session through
    ``agent_with_overrides``. It never changes the stored agent.

    ``system_override`` replaces the system prompt the same way, for a platform
    whose variant differs from the agent's own (`daimon.core.platform_system`).

    An agent attached to a signed-identity relay (`daimon.core.mcp_identity`) gets a
    vault of this session's own for it (`create_relay_vault`), whatever caller.
    ``relay_session_key`` is the `thread_sessions` row a person's chat session will
    be recorded under, which the relay reads to find the turn the session runs;
    without it (a routine, any headless caller) the relay names no person.

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
    scope = session_scope(
        tenant_id=tenant_id, account_id=account_id, call_site="sessions:create_session"
    )
    isolation_public_url = (
        str(mcp_settings.public_url)
        if mcp_settings is not None and mcp_settings.public_url is not None
        else None
    )
    if isolated_relay_names and (
        relay_session_key is None
        or account_id is None
        or extra_resources
        or isolation_public_url is None
        or frozenset(server.name for server in agent.mcp_servers)
        != isolated_relay_names | {"daimon-mcp"}
        or len(agent.mcp_servers) != len(isolated_relay_names) + 1
        or any(
            canonical_mcp_url(server.url)
            != canonical_mcp_url(
                isolation_public_url
                if server.name == "daimon-mcp"
                else relay_url(isolation_public_url, server.name)
            )
            for server in agent.mcp_servers
        )
        or any(
            (
                tool.type == "mcp_toolset"
                and tool.mcp_server_name not in isolated_relay_names | {"daimon-mcp"}
            )
            or tool.type not in {"mcp_toolset", "agent_toolset_20260401"}
            for tool in agent.tools
        )
        or sum(tool.type == "agent_toolset_20260401" for tool in agent.tools) != 1
        or any(
            tool.type == "agent_toolset_20260401"
            and any(
                config.name not in {"bash", "read", "edit", "grep", "glob", "write"}
                for config in tool.configs or []
            )
            for tool in agent.tools
        )
        or frozenset(
            tool.mcp_server_name for tool in agent.tools if tool.type == "mcp_toolset"
        )
        != isolated_relay_names | {"daimon-mcp"}
        or sum(tool.type == "mcp_toolset" for tool in agent.tools)
        != len(isolated_relay_names) + 1
    ):
        raise ValueError("isolated relay requires a dedicated relay-only agent session")
    if isolated_relay_names:
        assert isolation_public_url is not None
    resource_agent = agent
    app_mode = False
    if (
        not isolated_relay_names
        and tenant_id is not None
        and agent_uuid is not None
        and session_factory is not None
    ):
        app_mode = await _session_app_mode(
            session_factory, tenant_id=tenant_id, agent_id=agent_uuid
        )
    app_grant_view: EffectiveAppGrantView | None = None
    if app_mode:
        assert tenant_id is not None and agent_uuid is not None and session_factory is not None
        app_grant_view = await effective_app_grant_view(
            session_factory,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            fernet=fernet,
        )
    caller_vault = _caller_mcp_vault_required(
        app_mode=app_mode,
        mcp_settings=mcp_settings,
        account_id=account_id,
        agent_uuid=agent_uuid,
        session_factory=session_factory,
    )
    vault_id: str | None = None
    caller_vault_id: str | None = None
    relay_vault_id: str | None = None
    try:
        if app_mode:
            assert tenant_id is not None and agent_uuid is not None and session_factory is not None
            if app_grant_view is not None and app_grant_view[0]:
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
                    hold=hold,
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
                    hold=hold,
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
                hold=hold,
                scope=scope,
            )

        relay_urls = (
            ()
            if mcp_settings is None or mcp_settings.public_url is None
            else identity_relay_urls(
                (server.url for server in agent.mcp_servers),
                public_url=str(mcp_settings.public_url),
            )
        )
        if relay_urls and (caller_vault_id or vault_id) is not None and account_id is not None:
            assert agent_uuid is not None and session_factory is not None
            assert mcp_settings is not None and mcp_settings.jwt_secret is not None
            relay_vault_id = await create_relay_vault(
                anthropic,
                session_factory=session_factory,
                account_id=account_id,
                agent_id=agent_uuid,
                jwt_secret=mcp_settings.jwt_secret.get_secret_value().encode(),
                relay_urls=relay_urls,
                relay=RelayBinding(
                    agent_name=agent.name,
                    session_key=relay_session_key,
                    isolated=bool(isolated_relay_names),
                ),
                now=dt.datetime.now(dt.UTC),
                scope=scope,
                slack_turn_context_id=slack_turn_context_id,
                hold=hold,
            )
        if isolated_relay_names and relay_vault_id is None:
            raise ValueError("isolated relay credential is unavailable")

        # External MCP servers are attached to the AGENT, so every caller who
        # mentions it gets the toolset — but MA resolves each server's credential
        # from the vault mounted here, which is the CALLER's. Mirror the agent's
        # stored credentials in on every session create (same shape as the Copilot
        # mirror above) or callers who did not personally attach a server fail the
        # whole turn at MCP init. Not degrade-not-block: see
        # mirror_credentials_into_vault.
        credential_vault_id = caller_vault_id if app_mode else vault_id
        if not isolated_relay_names and _agent_mcp_mirror_required(
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
        if (
            not isolated_relay_names
            and tenant_id is not None
            and agent_uuid is not None
            and session_factory is not None
        ):
            mount = await upload_env_and_mount(
                anthropic,
                session_factory,
                tenant_id=tenant_id,
                agent_id=agent_uuid,
                exclude_github=app_mode,
            )
            if mount is not None:
                resources.append(mount)
            if memory_hold is not None:
                # A held turn mounts only its channel's store, shared with
                # everyone there, so nothing agent-wide or personal can leak in.
                try:
                    resources.append(
                        await ensure_channel_memory_store_and_mount(
                            anthropic,
                            session_factory,
                            tenant_id=tenant_id,
                            agent_id=agent_uuid,
                            platform=memory_hold.platform,
                            channel_id=memory_hold.channel_id,
                            agent_name=resource_agent.name,
                            read_only=memory_read_only,
                        )
                    )
                except (anthropic_pkg.APIError, StoreError) as exc:
                    _log.warning(
                        "memory_store.channel_mount_failed",
                        tenant_id=str(tenant_id),
                        agent_uuid=str(agent_uuid),
                        agent_name=resource_agent.name,
                        error=str(exc),
                    )
            else:
                personal_mount: BetaManagedAgentsMemoryStoreResourceParam | None = None
                if personal_memory and account_id is not None:
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
                        expected_memory_store_id=None,
                    )
                    resources.append(memory_resource)
                except (anthropic_pkg.APIError, StoreError) as exc:
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
            daimon_rule_tools=daimon_rule_tools,
        )
        servers = session_mcp_servers(
            agent,
            hidden,
            tool_safety=tool_safety,
            public_url=public_url,
            confirmed_tools=confirmed_tools,
        )
        if isolated_relay_names:
            tools = [
                tool
                for tool in tools
                if tool.type == "mcp_toolset" and tool.mcp_server_name in isolated_relay_names
            ]
            servers = [server for server in servers if server.name in isolated_relay_names]
            if frozenset(server.name for server in servers) != isolated_relay_names:
                raise ValueError("isolated relay server is unavailable")
        gated = list(tools) != list(visible_tools(agent, hidden))
        healed = list(servers) != list(visible_mcp_servers(agent, hidden))
        if (
            hidden
            or gated
            or healed
            or channel_skills
            or model_override is not None
            or system_override is not None
            or isolated_relay_names
        ):
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
            if system_override is not None:
                overrides["system"] = system_override
            if isolated_relay_names:
                overrides["skills"] = []
            elif channel_skills:
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
        if isolated_relay_names:
            metadata[MA_METADATA_KEY_CONTEXT_ISOLATED] = "true"
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
                        provisional_session_id=provisional_session_id,
                        config=agent_github_app or GithubAppSettings(),
                        fernet=fernet,
                        cache=REQUESTER_CACHE,
                        grant_view=app_grant_view,
                        anthropic=anthropic,
                        vault_id=vault_id,
                    )
                if on_app_access is not None:
                    on_app_access(app_access)
                resources.extend(app_access.resources)
                if app_access.multi_installation and any(
                    token.credential_name.startswith("GH_TOKEN_I") for token in app_access.tokens
                ):
                    entries = repo_credential_entries(app_access)
                    resources.extend(
                        await upload_github_repo_helpers_and_mount(
                            anthropic,
                            session_factory,
                            entries=entries,
                            tenant_id=tenant_id,
                        )
                    )
                    metadata["daimon_github_repo_map_sha256"] = github_repo_map_sha256(entries)
                    metadata["daimon_github_repo_script_sha256"] = github_repo_script_sha256()
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
                # The relay vault goes first: `turn.prepare` records the last
                # mounted vault as the session's Daimon one.
                vault_ids=(
                    [relay_vault_id]
                    if isolated_relay_names and relay_vault_id is not None
                    else [
                        mounted
                        for mounted in (relay_vault_id, caller_vault_id, vault_id)
                        if mounted is not None
                    ]
                )
                or None,
                resources=resources if resources else None,
            )
            try:
                created = await create()
                if isolated_relay_names and (
                    frozenset(server.name for server in created.agent.mcp_servers)
                    != isolated_relay_names
                    or len(created.agent.mcp_servers) != len(isolated_relay_names)
                    or any(
                        canonical_mcp_url(server.url)
                        != canonical_mcp_url(
                            relay_url(cast(str, isolation_public_url), server.name)
                        )
                        for server in created.agent.mcp_servers
                    )
                    or any(
                        tool.type != "mcp_toolset"
                        or tool.mcp_server_name not in isolated_relay_names
                        for tool in created.agent.tools
                    )
                    or created.agent.skills
                    or (created.metadata or {}).get(MA_METADATA_KEY_CONTEXT_ISOLATED) != "true"
                    or created.resources
                    or created.vault_ids != [relay_vault_id]
                ):
                    raise ValueError("provider did not honor isolated session configuration")
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
                        mcp_hold=hold_key(hold) if account_id is not None else None,
                        memory_scope=memory_scope(memory_hold),
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
            if created is not None and (app_mode or isolated_relay_names):
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
        if relay_vault_id is not None:
            await archive_vault(anthropic, relay_vault_id, scope=scope)
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

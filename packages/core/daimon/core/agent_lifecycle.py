"""Cross-adapter primitives for blank-agent creation, forking and best-effort delete archival.

Extracted from Discord's `agent_setup.write` module (the original,
audited implementation) so Slack and MCP get a behavior-identical copy of
these security-sensitive paths rather than a per-adapter reimplementation.

Signatures take explicit primitives (`anthropic`, `sessionmaker`, `fernet`,
`oauth_scopes`, tenant/agent UUIDs) — NEVER a Runtime/Protocol object —
because `daimon.core` must not import any `daimon.adapters` module
(import-linter contract), and the three adapter runtimes (Discord, Slack,
MCP) use different field names for the same collaborators.
"""

from __future__ import annotations

import contextlib
import uuid

import anthropic as anthropic_errors
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.agent_ownership import agent_name_lock, record_agent_creator
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.defaults.reconcile_agents import AgentNameTaken, reconcile_agent
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.errors import AgentNameCollision, DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.permissions import without_agent_rules
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_mcp_credentials as mcp_credentials_store
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    PolicyBusyError,
    load_access_policy,
    lock_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.scoped_config_write import clear_agent_references
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger()


async def create_user_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    *,
    tenant_id: uuid.UUID,
    spec: AgentSpec,
    account_id: uuid.UUID,
    public_url: str | None,
    creator_account_id: uuid.UUID | None,
    taken_message: str,
) -> ResourceOutcome:
    """Create a user-owned agent; refuse a name in use; the person who created it owns it.

    Names are tenant-wide identity (reconcile dedup and the resolver key on
    tenant and name only), so any existing agent with the name refuses it,
    with `AgentNameCollision(taken_message)`. With a creator, the check and the
    create run under the name lock, and the create never updates an agent that
    took the name meanwhile, so nobody owns or rewrites someone else's agent.
    `account_id` is the metadata stamp (the workspace account), not the owner.
    `managed=False`: a seeded-looking agent would be swept by the next defaults
    apply because it is not in the seeded spec list.
    """
    async with contextlib.AsyncExitStack() as stack:
        if sessionmaker is not None and creator_account_id is not None:
            await stack.enter_async_context(
                agent_name_lock(sessionmaker, tenant_id=tenant_id, name=spec.name)
            )
        if await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=spec.name):
            raise AgentNameCollision(taken_message)
        try:
            outcome = await reconcile_agent(
                anthropic,
                spec,
                tenant_id=tenant_id,
                dry_run=False,
                account_id=account_id,
                public_url=public_url,
                managed=False,
                create_only=True,
            )
        except AgentNameTaken as exc:
            raise AgentNameCollision(taken_message) from exc
        if (
            sessionmaker is not None
            and creator_account_id is not None
            and outcome.action == Action.CREATED
            and outcome.anthropic_id is not None
        ):
            await record_agent_creator(
                sessionmaker,
                tenant_id=tenant_id,
                agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=outcome.anthropic_id),
                account_id=creator_account_id,
            )
    return outcome


async def create_blank_agent(
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    name: str,
    system: str | None,
    model: str,
    account_id: uuid.UUID,
    public_url: str | None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    creator_account_id: uuid.UUID | None = None,
) -> ResourceOutcome:
    """Create an unrouted, user-owned agent from a panel's New agent form.

    The person who pressed New agent (`creator_account_id`) owns it.
    """
    try:
        spec = AgentSpec.model_validate({"name": name, "model": model, "system": system})
    except ValidationError as err:
        raise DaimonError(f"Spec validation failed: {err}") from err
    return await create_user_agent(
        anthropic,
        sessionmaker,
        tenant_id=tenant_id,
        spec=spec,
        account_id=account_id,
        public_url=public_url,
        creator_account_id=creator_account_id,
        taken_message=f"There is already an agent named {name} here. Pick a different name.",
    )


async def strip_credentialed_mcp_servers(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    source_agent_uuid: uuid.UUID,
    mcp_servers: list[dict[str, object]] | None,
    tools: list[dict[str, object]] | None,
) -> tuple[list[dict[str, object]] | None, list[dict[str, object]] | None]:
    """The source's MCP servers and tools minus every server backed by a stored token.

    A fork starts with no credentials: no GitHub credential, no repo binding
    or recorded proof of repo access, and no agent-wide MCP token. Copying
    them made every fork an independent holder of the source's access, with
    no pin and no routing of its own, so one fork was enough to take another
    project's repo and connectors out of the channels they serve, and to keep
    them after the source was revoked. Whoever needs the copy to reach a repo
    or a connector attaches its own credential.

    A server whose token lives in ``agent_mcp_credentials`` would fail every
    turn's MCP init on a fork without that token, so it is dropped together
    with its ``mcp_toolset`` entries. Servers with no stored token (anonymous
    ones, and sign-in servers whose grants live in per-person vaults keyed by
    the source agent) carry no secret and are kept.
    """
    async with sessionmaker() as session:
        credentials = await mcp_credentials_store.list_credentials(
            session, tenant_id=tenant_id, agent_id=source_agent_uuid
        )
    credentialed_urls = {canonical_mcp_url(row.mcp_server_url) for row in credentials}
    if not credentialed_urls or mcp_servers is None:
        return mcp_servers, tools
    dropped = {
        str(server.get("name"))
        for server in mcp_servers
        if canonical_mcp_url(str(server.get("url") or "")) in credentialed_urls
    }
    if not dropped:
        return mcp_servers, tools
    _log.info(
        "agent_lifecycle.fork_dropped_credentialed_mcp_servers",
        source_agent_id=str(source_agent_uuid),
        count=len(dropped),
    )
    kept_servers = [server for server in mcp_servers if server.get("name") not in dropped]
    kept_tools = (
        None
        if tools is None
        else [
            tool
            for tool in tools
            if not (tool.get("type") == "mcp_toolset" and tool.get("mcp_server_name") in dropped)
        ]
    )
    return kept_servers, kept_tools


async def archive_memory_store_best_effort(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    log_context: dict[str, object],
) -> None:
    """Archive the agent's memory store, degrading best-effort on failure.

    Callers invoke this after the MA agent itself has already been archived
    and the retry path is dead (archived agents are filtered from lookup),
    so a transient memory-store archive failure must not strand the delete
    flow in a failed state — mirrors the mount-side degrade policy in
    `memory_resource.py`. Catches ONLY `anthropic.APIError`; any other
    exception propagates.
    """
    try:
        await archive_memory_store_for_agent(
            anthropic,
            sessionmaker,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
    except anthropic_errors.APIError:
        # Best-effort degrade: the caller's own delete flow has already
        # committed (e.g. the MA agent is archived and the retry path is
        # dead), so a transient memory-store archive failure must not
        # strand it in a failed state — mirrors the mount-side policy in
        # memory_resource.py.
        _log.warning("agent_lifecycle.memory_store_archive_failed", **log_context)


async def forget_archived_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    name: str,
    agent: BetaManagedAgentsAgent,
) -> tuple[str, ...]:
    """Drop an archived agent's rules and clear every scope row naming it, by its own names.

    Called after the MA archive by the CLI, MCP and Slack archive actions.
    The rule goes on each of its own names (the one asked for, its MA name
    and its config name), never the source names a report reader also
    answers to, which are another agent's. A rule left behind outlives the
    agent, and a later agent taking the name (a channel copy,
    `channel_rules.copy_name`) inherits it. One transaction under the policy
    lock. When the policy can't be read or its lock can't be
    had, the scope rows are still cleared and the rule is left for
    `daimon agents rule set <name> --anywhere`; any other failure raises.
    Returns the names whose rule was dropped.
    """
    own = (name, agent.name, agent.metadata.get(MA_METADATA_KEY_NAME))
    names = tuple(dict.fromkeys(n for n in own if n))
    log_fields = {"tenant_id": str(tenant_id), "agent_name": name, "ma_agent_id": agent.id}
    try:
        async with policy_write_transaction(sessionmaker, tenant_id=tenant_id) as session:
            await lock_access_policy(session, tenant_id=tenant_id)
            policy = await load_access_policy(session, tenant_id=tenant_id)
            updated = without_agent_rules(policy, names)
            if updated != policy:
                await set_access_policy(session, tenant_id=tenant_id, policy=updated)
            await _clear_references(session, tenant_id=tenant_id, names=names)
        return tuple(n for n in names if n in policy.agent_rules)
    except (AccessPolicyUnreadable, PolicyBusyError):
        _log.warning("agent_lifecycle.agent_rule_kept", exc_info=True, **log_fields)
    async with sessionmaker.begin() as session:
        await _clear_references(session, tenant_id=tenant_id, names=names)
    return ()


async def _clear_references(
    session: AsyncSession, *, tenant_id: uuid.UUID, names: tuple[str, ...]
) -> None:
    for each in names:
        await clear_agent_references(session, tenant_id=tenant_id, agent_name=each)

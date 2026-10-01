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

import uuid

import anthropic as anthropic_errors
import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import DaimonError
from daimon.core.memory_resource import archive_memory_store_for_agent
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_mcp_credentials as mcp_credentials_store
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger()


async def create_blank_agent(
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    name: str,
    system: str | None,
    model: str,
    account_id: uuid.UUID,
    public_url: str | None,
) -> ResourceOutcome:
    """Create an unrouted, user-owned agent from a panel's New agent form.

    Names are tenant-wide identity (reconcile dedup and the resolver key on
    tenant and name only), so any existing agent with `name` refuses it.
    `managed=False`: a seeded-looking agent would be swept by the next
    defaults apply because it is not in the seeded spec list.
    """
    if await find_agents_by_daimon_tag(anthropic, tenant_id=tenant_id, name=name):
        raise DaimonError(
            f"This workspace already has an agent named {name}. Pick a different name."
        )
    try:
        spec = AgentSpec.model_validate({"name": name, "model": model, "system": system})
    except ValidationError as err:
        raise DaimonError(f"Spec validation failed: {err}") from err
    return await reconcile_agent(
        anthropic,
        spec,
        tenant_id=tenant_id,
        dry_run=False,
        account_id=account_id,
        public_url=public_url,
        managed=False,
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
    credentialed_urls = {row.mcp_server_url.rstrip("/") for row in credentials}
    if not credentialed_urls or mcp_servers is None:
        return mcp_servers, tools
    dropped = {
        str(server.get("name"))
        for server in mcp_servers
        if str(server.get("url") or "").rstrip("/") in credentialed_urls
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

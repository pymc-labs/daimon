"""Apply a compatibility decision's ops to a live MA session.

The shell half of `session_compat`: that module decides *what* has to change,
this one performs it against the API and reports the configuration the session
runs afterwards. Nothing here decides anything — the op list is the decision.

Two failure shapes are deliberately not exceptions:

- `SessionBusy` — MA refuses `sessions.update` while the session is running
  ("Cannot update agent while session is running"). That is a wait, not a
  fault: the caller defers and retries at the next bind.
- `EnvMountLost` — the old `.env` resource was deleted and the replacement
  could not be mounted. It IS an exception, because the turn must not run
  against a session whose secrets just vanished, but it carries the snapshot
  the caller must persist: with `env_sha256` cleared, the next bind's decision
  is another `ReplaceEnvFile` that only has to add.

Every other API failure propagates; the caller turns it into a preparation
failure that leaves the old session live and untouched.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import structlog
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.beta_managed_agents_session_agent_update_param import (
    BetaManagedAgentsSessionAgentUpdateParam,
    Tool,
)
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from cryptography.fernet import MultiFernet
from daimon.core.agent_mcp_credentials import (
    McpVaultIds,
    resolve_hidden_mcp_server_names,
    resync_mounted_shared_vault,
    sync_agent_mcp_credentials,
)
from daimon.core.config import GithubAppSettings, McpSettings
from daimon.core.credential_env import assemble_env_bytes, upload_env_file
from daimon.core.errors import DaimonError
from daimon.core.github_app_session import rotate_live_app_tokens
from daimon.core.mcp_vault import refresh_mounted_mcp_vaults
from daimon.core.session_compat import (
    ChangeReason,
    RemirrorVaultCredentials,
    ReplaceEnvFile,
    ReplaceToolsAndMcpServers,
    RotateAppTokens,
    UpdateOp,
)
from daimon.core.session_ports_compat import (
    add_session_file_record,
    remove_session_resource_record,
    retrieve_session_record,
    session_scope,
    update_session_record,
    walk_session_resource_records,
)
from daimon.core.session_snapshot import (
    MaTool,
    SessionSnapshot,
    hash_env_bytes,
    hash_mcp_servers,
    hash_tools,
    refreshed_unavailable_state,
    session_mcp_servers,
    session_tools,
)
from daimon.core.stores.agent_daimon_tools import record_enabled_daimon_tools
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.tool_safety import ToolSafetyPolicy
from mux.contracts.ids import Revision, Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

__all__ = ["AppliedOps", "EnvMountLost", "SessionBusy", "apply_update_ops"]

_ENV_MOUNT_PATH = ".env"
_RUNNING_MARKER = "while session is running"


@dataclass(frozen=True, slots=True)
class AppliedOps:
    """Every op ran. `snapshot` is what the session runs from the next turn."""

    snapshot: SessionSnapshot
    applied: tuple[ChangeReason, ...]


@dataclass(frozen=True, slots=True)
class SessionBusy:
    """MA refused a mid-turn update. `snapshot` is what DID apply before that."""

    snapshot: SessionSnapshot
    applied: tuple[ChangeReason, ...]


class EnvMountLost(DaimonError):
    """The old `.env` is gone and the new one could not be mounted."""

    def __init__(self, *, snapshot: SessionSnapshot, session_id: str) -> None:
        super().__init__(f"the .env resource for session {session_id} could not be remounted")
        self.snapshot = snapshot
        self.session_id = session_id


def _is_session_running(error: APIStatusError) -> bool:
    """MA's refusal to touch a session's agent mid-turn (capability matrix P2.d)."""
    return error.status_code == 400 and _RUNNING_MARKER in str(error).lower()


def _agent_update(
    tools: Sequence[MaTool], mcp_servers: Sequence[BetaManagedAgentsMCPServerURLDefinition]
) -> BetaManagedAgentsSessionAgentUpdateParam:
    """Both arrays, in full, from the agent as it stands now.

    `sessions.update` is a full replacement, so a partial array would silently
    drop whatever it omitted. `vault_ids` is never sent — MA rejects it
    outright ("Updating vault_ids is not yet supported"), and the vault a
    session mounts is fixed at create time anyway.

    "In full" means the caller's full list, not the agent's: a server only
    somebody else's OAuth grant can authenticate is left out here exactly as
    `create_session` leaves it out, or the first configuration change of the
    session's life would push it back onto a caller who cannot open it. And
    both are gated as `create_session` gated them (`session_tools`), or the
    update would write the agent's `always_allow` over the session's
    `always_ask`.
    """
    return {
        "tools": [cast(Tool, tool.model_dump(mode="json")) for tool in tools],
        "mcp_servers": [
            cast(BetaManagedAgentsURLMCPServerParams, server.model_dump(mode="json"))
            for server in mcp_servers
        ],
    }


async def _find_env_resource_id(
    anthropic: AsyncAnthropic, session_id: str, *, scope: Scope
) -> str | None:
    """The mounted `.env`'s resource id, for a snapshot that never recorded one."""
    async for resource in walk_session_resource_records(anthropic, session_id, scope=scope):
        if resource.kind == "artifact" and cast(str, resource.target_path).endswith(
            _ENV_MOUNT_PATH
        ):
            return resource.id
    return None


async def _replace_env_file(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    op: ReplaceEnvFile,
    session_id: str,
    snapshot: SessionSnapshot,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    scope: Scope,
) -> SessionSnapshot:
    """Swap the session's `.env` for the agent's current secrets, in place.

    Delete-then-add, in that order: MA rejects a second resource at an occupied
    mount path (P0.f/P3.a), and the vacated path is visible to the same turn
    (P3.c). An agent whose last secret was removed keeps no `.env` at all,
    which is what a fresh session for it would mount.
    """
    async with sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_uuid)
    if snapshot.github_mode == "app":
        rows = [row for row in rows if row.key not in ("GH_TOKEN", "GITHUB_TOKEN")]

    file_id = (
        await upload_env_file(anthropic, sessionmaker, rows=rows, scope=scope) if rows else None
    )
    cleared = snapshot.model_copy(
        update={"env_sha256": None, "env_file_id": None, "env_resource_id": None}
    )

    resource_id = op.old_resource_id
    if resource_id is None:
        resource_id = await _find_env_resource_id(anthropic, session_id, scope=scope)
    if resource_id is not None:
        try:
            await remove_session_resource_record(anthropic, session_id, resource_id, scope=scope)
        except APIStatusError as error:
            # Already gone is the state we were asking for.
            if error.status_code != 404:
                raise

    if file_id is None:
        return cleared

    try:
        added = await add_session_file_record(
            anthropic, session_id, file_id=file_id, mount_path=op.mount_path, scope=scope
        )
    except APIStatusError as error:
        log.warning("session_update.env_mount_lost", session_id=session_id, error=str(error))
        raise EnvMountLost(snapshot=cleared, session_id=session_id) from error

    return snapshot.model_copy(
        update={
            "env_sha256": hash_env_bytes(assemble_env_bytes(rows)),
            "env_file_id": file_id,
            "env_resource_id": added.id,
        }
    )


async def apply_update_ops(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    ops: Sequence[UpdateOp],
    session_id: str,
    recorded: SessionSnapshot,
    agent: BetaManagedAgentsAgent,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    account_id: uuid.UUID,
    mcp: McpSettings,
    tool_safety: ToolSafetyPolicy,
    fernet: MultiFernet | None,
    github_fallback_pat: str | None,
    github_app_id: str | None,
    github_app_private_key: str | None,
    agent_github_app: GithubAppSettings | None = None,
    now: dt.datetime,
    asks_before_publishing: bool = False,
    confirmed_tools: frozenset[str] = frozenset(),
    daimon_rule_tools: frozenset[str] = frozenset(),
    mcp_vault_ids: McpVaultIds | None = None,
    shared_owner: uuid.UUID | None = None,
    legacy_memory_servers: tuple[str, ...] = (),
) -> AppliedOps | SessionBusy:
    """Run `ops` against the live session and return what it runs afterwards.

    Ops are applied in the order `decide_session_compatibility` produced them
    and each one folds its result into the snapshot, so a `SessionBusy` return
    still reports everything that landed before MA refused. `applied` is
    coarse — the axes an op touches, not the exact fields that differed; the
    caller intersects it with its own decision's reasons.
    """
    scope = session_scope(
        tenant_id=tenant_id, account_id=account_id, call_site="session_update_ops:apply_update_ops"
    )
    snapshot = recorded
    applied: list[ChangeReason] = []

    # Shared workspaces must never acquire a caller's MCP identity. App mode
    # can mount both a caller and a repository vault; refresh every actual slot
    # rather than relying on undocumented provider credential precedence.
    if (
        shared_owner is None
        and snapshot.vault_id is not None
        and mcp.public_url is not None
        and mcp.jwt_secret is not None
    ):
        mounted_vaults = (snapshot.vault_id,)
        if snapshot.github_mode == "app" or snapshot.chat_session_id is not None:
            live = await retrieve_session_record(anthropic, session_id, scope=scope)
            mounted_vaults = tuple(dict.fromkeys(live.vault_ids))
        await refresh_mounted_mcp_vaults(
            anthropic,
            vault_ids=mounted_vaults,
            account_id=account_id,
            agent_id=agent_uuid,
            jwt_secret=mcp.jwt_secret.get_secret_value().encode(),
            public_url=str(mcp.public_url),
            now=now,
            session_factory=sessionmaker,
            scope=scope,
        )

    for op in ops:
        match op:
            case ReplaceEnvFile():
                snapshot = await _replace_env_file(
                    anthropic,
                    sessionmaker,
                    op=op,
                    session_id=session_id,
                    snapshot=snapshot,
                    tenant_id=tenant_id,
                    agent_uuid=agent_uuid,
                    scope=scope,
                )
                applied.append("env_file")
            case ReplaceToolsAndMcpServers():
                hidden = await resolve_hidden_mcp_server_names(
                    sessionmaker,
                    tenant_id=tenant_id,
                    agent_id=agent_uuid,
                    account_id=account_id,
                    server_urls={server.name: server.url for server in agent.mcp_servers},
                )
                public_url = None if mcp.public_url is None else str(mcp.public_url)
                tools = session_tools(
                    agent,
                    hidden,
                    tool_safety=tool_safety,
                    public_url=public_url,
                    asks_before_publishing=asks_before_publishing,
                    confirmed_tools=confirmed_tools,
                    daimon_rule_tools=daimon_rule_tools,
                    legacy_memory_servers=legacy_memory_servers,
                )
                await record_enabled_daimon_tools(
                    sessionmaker,
                    tenant_id=tenant_id,
                    agent_id=agent_uuid,
                    tools=[tool.model_dump(mode="json") for tool in tools],
                )
                servers = session_mcp_servers(
                    agent,
                    hidden,
                    tool_safety=tool_safety,
                    public_url=public_url,
                    confirmed_tools=confirmed_tools,
                    chat_session_id=recorded.chat_session_id,
                )
                try:
                    await update_session_record(
                        anthropic,
                        session_id,
                        scope=scope,
                        agent_id=agent.id,
                        revision=Revision(
                            local=snapshot.agent_version, native=str(snapshot.agent_version)
                        ),
                        agent=_agent_update(tools, servers),
                    )
                except APIStatusError as error:
                    if not _is_session_running(error):
                        raise
                    log.info("session_update.deferred_busy", session_id=session_id)
                    return SessionBusy(snapshot=snapshot, applied=tuple(applied))
                snapshot = snapshot.model_copy(
                    update={
                        "tools_sha256": hash_tools(tools),
                        "mcp_servers_sha256": hash_mcp_servers(servers),
                    }
                )
                applied.extend(("tools", "mcp_servers"))
            case RotateAppTokens():
                if snapshot.vault_id is None or not isinstance(fernet, MultiFernet):
                    raise ValueError("App session has no vault or encryption")
                access = await rotate_live_app_tokens(
                    anthropic,
                    sessionmaker,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    agent_id=agent_uuid,
                    account_id=account_id,
                    vault_id=snapshot.vault_id,
                    resource_ids=op.resource_ids,
                    unavailable_repos=snapshot.unavailable_repos,
                    config=agent_github_app or GithubAppSettings(),
                    fernet=fernet,
                    installation_ids=op.installation_ids,
                )
                issued_at = int(now.timestamp())
                legacy_issued_at = issued_at
                snapshot = snapshot.model_copy(
                    update={
                        "repo_urls": tuple(
                            url
                            for url in snapshot.repo_urls
                            if url not in access.excluded_repo_urls
                        ),
                        "token_repo_urls": access.token_repo_urls,
                        "token_scopes": access.token_scopes,
                        "token_issued_at_by_installation": {
                            installation_id: min(
                                token.issued_at or issued_at
                                for token in access.tokens
                                if token.installation_id == installation_id
                            )
                            for installation_id in {scope[0] for scope in access.token_scopes}
                        },
                        **refreshed_unavailable_state(
                            snapshot, access.excluded_repo_urls, now=dt.datetime.now(dt.UTC)
                        ),
                        "repo_token_issued_at": legacy_issued_at,
                    }
                )
                applied.append("repo_token_age")
            case RemirrorVaultCredentials():
                # A shared thread re-mirrors its own vault, never a caller's.
                if shared_owner is not None:
                    # Only into a vault this session mounts; see the callee.
                    if fernet is not None and snapshot.vault_id is not None:
                        mounted: tuple[str, ...] = (snapshot.vault_id,)
                        if snapshot.github_mode == "app":
                            live = await retrieve_session_record(anthropic, session_id, scope=scope)
                            mounted = tuple(live.vault_ids)
                        await resync_mounted_shared_vault(
                            anthropic,
                            sessionmaker=sessionmaker,
                            fernet=fernet,
                            tenant_id=tenant_id,
                            agent_id=agent_uuid,
                            owner=shared_owner,
                            mounted_vault_ids=mounted,
                            exclude_github_copilot=snapshot.github_mode == "app",
                            vault_ids=mcp_vault_ids,
                        )
                # The organisation's tokens never reach an external caller's vault.
                elif (
                    fernet is not None and mcp.public_url is not None and mcp.jwt_secret is not None
                ):
                    await sync_agent_mcp_credentials(
                        anthropic,
                        sessionmaker=sessionmaker,
                        fernet=fernet,
                        tenant_id=tenant_id,
                        agent_id=agent_uuid,
                        account_id=account_id,
                        jwt_secret=mcp.jwt_secret.get_secret_value().encode(),
                        public_url=str(mcp.public_url),
                        now=now,
                        exclude_github_copilot=snapshot.github_mode == "app",
                        vault_ids=mcp_vault_ids,
                    )

    return AppliedOps(snapshot=snapshot, applied=tuple(applied))

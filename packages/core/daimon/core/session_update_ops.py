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

import httpx
import structlog
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_session_agent_update_param import (
    BetaManagedAgentsSessionAgentUpdateParam,
    Tool,
)
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from anthropic.types.beta.sessions.beta_managed_agents_file_resource import (
    BetaManagedAgentsFileResource,
)
from cryptography.fernet import MultiFernet
from daimon.core.agent_mcp_credentials import (
    resolve_hidden_mcp_server_names,
    sync_agent_mcp_credentials,
)
from daimon.core.config import McpSettings
from daimon.core.credential_env import assemble_env_bytes, upload_env_file
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import get_pat
from daimon.core.github_repo_auth import resolve_clone_token
from daimon.core.mcp_personal_servers import visible_mcp_servers, visible_tools
from daimon.core.session_compat import (
    ChangeReason,
    RemirrorVaultCredentials,
    ReplaceEnvFile,
    ReplaceToolsAndMcpServers,
    RotateRepoToken,
    UpdateOp,
)
from daimon.core.session_snapshot import (
    SessionSnapshot,
    hash_env_bytes,
    hash_mcp_servers,
    hash_tools,
)
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.agent_repo_binding import get_binding
from pydantic import SecretStr
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
    agent: BetaManagedAgentsAgent, hidden_mcp_server_names: frozenset[str]
) -> BetaManagedAgentsSessionAgentUpdateParam:
    """Both arrays, in full, from the agent as it stands now.

    `sessions.update` is a full replacement, so a partial array would silently
    drop whatever it omitted. `vault_ids` is never sent — MA rejects it
    outright ("Updating vault_ids is not yet supported"), and the vault a
    session mounts is fixed at create time anyway.

    "In full" means the caller's full list, not the agent's: a server only
    somebody else's OAuth grant can authenticate is left out here exactly as
    `create_session` leaves it out, or the first configuration change of the
    session's life would push it back onto a caller who cannot open it.
    """
    return {
        "tools": [
            cast(Tool, tool.model_dump(mode="json"))
            for tool in visible_tools(agent, hidden_mcp_server_names)
        ],
        "mcp_servers": [
            cast(BetaManagedAgentsURLMCPServerParams, server.model_dump(mode="json"))
            for server in visible_mcp_servers(agent, hidden_mcp_server_names)
        ],
    }


async def _find_env_resource_id(anthropic: AsyncAnthropic, session_id: str) -> str | None:
    """The mounted `.env`'s resource id, for a snapshot that never recorded one."""
    async for resource in anthropic.beta.sessions.resources.list(session_id):
        if isinstance(resource, BetaManagedAgentsFileResource) and resource.mount_path.endswith(
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
) -> SessionSnapshot:
    """Swap the session's `.env` for the agent's current secrets, in place.

    Delete-then-add, in that order: MA rejects a second resource at an occupied
    mount path (P0.f/P3.a), and the vacated path is visible to the same turn
    (P3.c). An agent whose last secret was removed keeps no `.env` at all,
    which is what a fresh session for it would mount.
    """
    async with sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_uuid)

    file_id = await upload_env_file(anthropic, sessionmaker, rows=rows) if rows else None
    cleared = snapshot.model_copy(
        update={"env_sha256": None, "env_file_id": None, "env_resource_id": None}
    )

    resource_id = op.old_resource_id
    if resource_id is None:
        resource_id = await _find_env_resource_id(anthropic, session_id)
    if resource_id is not None:
        try:
            await anthropic.beta.sessions.resources.delete(resource_id, session_id=session_id)
        except APIStatusError as error:
            # Already gone is the state we were asking for.
            if error.status_code != 404:
                raise

    if file_id is None:
        return cleared

    try:
        added = await anthropic.beta.sessions.resources.add(
            session_id, file_id=file_id, type="file", mount_path=op.mount_path
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


async def _rotate_repo_token(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    op: RotateRepoToken,
    session_id: str,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    fernet: MultiFernet | None,
    github_fallback_pat: str | None,
    github_app_id: str | None,
    github_app_private_key: str | None,
    now: dt.datetime,
) -> bool:
    """Mint a fresh clone credential and hand it to the mounted repo resource."""
    async with sessionmaker() as session:
        binding = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)
    if binding is None:
        return False

    per_agent_pat = (
        None
        if fernet is None
        else await get_pat(
            principal_id=agent_uuid, agent_id=agent_uuid, sessionmaker=sessionmaker, fernet=fernet
        )
    )
    async with httpx.AsyncClient() as client:
        token = await resolve_clone_token(
            client,
            binding=binding,
            per_agent_pat=per_agent_pat,
            fallback_pat=github_fallback_pat,
            app_id=github_app_id,
            app_private_key=(
                None if github_app_private_key is None else SecretStr(github_app_private_key)
            ),
            now=int(now.timestamp()),
        )
    await anthropic.beta.sessions.resources.update(
        op.resource_id, session_id=session_id, authorization_token=token
    )
    return True


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
    fernet: MultiFernet | None,
    github_fallback_pat: str | None,
    github_app_id: str | None,
    github_app_private_key: str | None,
    now: dt.datetime,
) -> AppliedOps | SessionBusy:
    """Run `ops` against the live session and return what it runs afterwards.

    Ops are applied in the order `decide_session_compatibility` produced them
    and each one folds its result into the snapshot, so a `SessionBusy` return
    still reports everything that landed before MA refused. `applied` is
    coarse — the axes an op touches, not the exact fields that differed; the
    caller intersects it with its own decision's reasons.
    """
    snapshot = recorded
    applied: list[ChangeReason] = []

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
                try:
                    await anthropic.beta.sessions.update(
                        session_id, agent=_agent_update(agent, hidden)
                    )
                except APIStatusError as error:
                    if not _is_session_running(error):
                        raise
                    log.info("session_update.deferred_busy", session_id=session_id)
                    return SessionBusy(snapshot=snapshot, applied=tuple(applied))
                snapshot = snapshot.model_copy(
                    update={
                        "tools_sha256": hash_tools(visible_tools(agent, hidden)),
                        "mcp_servers_sha256": hash_mcp_servers(visible_mcp_servers(agent, hidden)),
                    }
                )
                applied.extend(("tools", "mcp_servers"))
            case RotateRepoToken():
                rotated = await _rotate_repo_token(
                    anthropic,
                    sessionmaker,
                    op=op,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    agent_uuid=agent_uuid,
                    fernet=fernet,
                    github_fallback_pat=github_fallback_pat,
                    github_app_id=github_app_id,
                    github_app_private_key=github_app_private_key,
                    now=now,
                )
                if rotated:
                    snapshot = snapshot.model_copy(
                        update={"repo_token_issued_at": int(now.timestamp())}
                    )
                    applied.append("repo_token_age")
            case RemirrorVaultCredentials():
                if fernet is not None and mcp.public_url is not None and mcp.jwt_secret is not None:
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
                    )

    return AppliedOps(snapshot=snapshot, applied=tuple(applied))

"""Assemble + deliver per-agent secrets as a mounted `.env`.

Pure logic lives in `assemble_env_bytes` (rows → KEY=VALUE bytes); the shell
`upload_env_and_mount` wires the tenant-scoped row fetch + SDK Files-API upload
+ TTL-delete enqueue together. Called from `sessions.py` and `headless_runner.py`
at session-create time.

Tenant isolation lives here: assembly reads ONLY the agent's own
(tenant_id, agent_id) rows. Secret values never reach logs — log file_id and
key_count only.
"""

from __future__ import annotations

import datetime as dt
import shlex
import uuid

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from anthropic.types.beta.beta_managed_agents_file_resource_params import (
    BetaManagedAgentsFileResourceParams,
)
from daimon.core.env_file import (
    env_row_skip_reason,
    is_reserved_github_token_name,
    serialize_env_file,
)
from daimon.core.github_app_auth import BotIdentity
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import upload_credential_file
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.domain import AgentFileRow
from daimon.core.stores.pending_file_deletes import enqueue_pending_file_delete
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)
_MOUNT_PATH = ".env"
#: Served at /mnt/session/uploads/git-identity.sh. Not a ``.env`` suffix, so the
#: session snapshot and the `.env` swap never mistake it for the secrets file.
GIT_IDENTITY_MOUNT_PATH = "git-identity.sh"


def session_env_key(key: str, *, exclude_github: bool) -> bool:
    """The session owner's app-mode exclusion, before value decoding/upload."""
    return not exclude_github or not is_reserved_github_token_name(key)


def assemble_env_bytes(rows: list[AgentFileRow]) -> bytes:
    """Pure: build .env byte content from secret rows.

    Returns empty bytes for empty rows (caller decides whether to skip upload).
    Each row becomes a `KEY=VALUE` line; the blob has a trailing newline.

    Quoting is delegated to `serialize_env_file`, which quotes only values that
    could not otherwise be read back. That minimalism is load-bearing: these
    bytes are hashed into the fingerprint session compatibility diffs, so
    quoting values that never needed it would invalidate every live agent's
    mounted `.env` at once.

    A row whose name is hard-denied — stored before that rule existed, or by an
    admin who set `DATABASE_URL` and the like — or whose value holds a NUL is
    left out and logged by name: the file is `source`d in the sandbox, so
    exporting `LD_PRELOAD`, `BASH_ENV` or `TAR_OPTIONS` set by any member would
    run their code in every later turn. This is the hard-deny layer only, so an
    admin-set ordinary name such as `DATABASE_URL` still mounts.
    """
    kept: list[tuple[str, str]] = []
    for row in rows:
        problem = env_row_skip_reason(row.key, row.content)
        if problem is not None:
            _log.warning(
                "credential_env.row_skipped",
                agent_id=str(row.agent_id),
                key=row.key,
                reason=problem,
            )
            continue
        kept.append((row.key, row.content))
    return serialize_env_file(kept)


async def upload_env_file(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    rows: list[AgentFileRow],
    ttl_hours: int = 1,
    scope: Scope | None = None,
) -> str:
    """Upload the `.env` these rows assemble to, enqueue its TTL delete, return its file id.

    The upload half of `upload_env_and_mount`, split out because a live
    session's `.env` is replaced by mounting the new file with
    `sessions.resources.add` rather than through `sessions.create` — same
    bytes, same disposable-object retention, different mount call.
    """
    file_id = await _upload_disposable(
        anthropic,
        session_factory,
        content=assemble_env_bytes(rows),
        mount_path=_MOUNT_PATH,
        ttl_hours=ttl_hours,
        secret_values=[row.content for row in rows],
        scope=scope
        or Scope.legacy_host_authorized(call_site="daimon.core.credential_env:upload_env_file"),
    )
    _log.info("credential_env.uploaded", file_id=file_id, key_count=len(rows))
    return file_id


async def _upload_disposable(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    content: bytes,
    mount_path: str,
    ttl_hours: int,
    secret_values: list[str],
    scope: Scope,
) -> str:
    """Upload `content` through the Files API and enqueue the object's TTL delete."""
    uploaded: FileMetadata = await upload_credential_file(
        anthropic,
        content,
        filename=mount_path,
        media_type="text/plain",
        secret_values=secret_values,
        scope=scope,
    )
    delete_after = dt.datetime.now(dt.UTC) + dt.timedelta(hours=ttl_hours)
    async with session_factory() as session, session.begin():
        await enqueue_pending_file_delete(session, file_id=uploaded.id, delete_after=delete_after)
    return uploaded.id


def render_git_identity_script(identity: BotIdentity) -> bytes:
    """Pure: the two `git config` lines that make commits author as the App's bot."""
    name, email = shlex.quote(identity.name), shlex.quote(identity.email)
    return (
        f"git config --global user.name {name}\ngit config --global user.email {email}\n"
    ).encode()


async def upload_git_identity_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    identity: BotIdentity,
    ttl_hours: int = 1,
    scope: Scope | None = None,
) -> BetaManagedAgentsFileResourceParams:
    """Upload the bot commit-identity script and return its session resource.

    The identity is not a secret, but it cannot ride the GitHub App's vault
    credentials (those are injected into request headers, not exported as
    plain environment variables) and it cannot ride the `.env` (an agent with
    no secrets has no `.env`, and its bytes are hashed into the session
    snapshot). A separate file leaves both alone.
    """
    file_id = await _upload_disposable(
        anthropic,
        session_factory,
        content=render_git_identity_script(identity),
        mount_path=GIT_IDENTITY_MOUNT_PATH,
        ttl_hours=ttl_hours,
        secret_values=[],
        scope=scope
        or Scope.legacy_host_authorized(
            call_site="daimon.core.credential_env:upload_git_identity_and_mount"
        ),
    )
    return {"type": "file", "file_id": file_id, "mount_path": GIT_IDENTITY_MOUNT_PATH}


async def upload_env_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    ttl_hours: int = 1,
    exclude_github: bool = False,
) -> BetaManagedAgentsFileResourceParams | None:
    """Fetch the agent's tenant-scoped secrets, upload them as a `.env`, mount it.

    Reads ONLY the (tenant_id, agent_id) rows (tenant isolation), assembles the
    `.env`, uploads via the Files API, and enqueues the uploaded object for TTL
    deletion (default 1h) — the DB row is the durable copy, the Files object is
    disposable per session. Returns the session-resource dict to pass as
    `resources=[result]`, or None when the agent has no secrets (caller skips
    resources entirely).

    The requested mount_path is ".env"; MA serves it at
    `/mnt/session/uploads/.env` (skills read from there).
    """
    async with session_factory() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_id)
    rows = [row for row in rows if session_env_key(row.key, exclude_github=exclude_github)]
    if not rows:
        return None

    file_id = await upload_env_file(
        anthropic,
        session_factory,
        rows=rows,
        ttl_hours=ttl_hours,
        scope=resource_scope(tenant_id=str(tenant_id)),
    )
    _log.info("credential_env.mounted", file_id=file_id, key_count=len(rows))
    return {"type": "file", "file_id": file_id, "mount_path": _MOUNT_PATH}

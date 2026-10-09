"""The steps `session_preparation` orchestrates, and the types they trade in.

Split out of `session_preparation` to keep that module about the decision it
makes; everything here is one step of it — the caller-scoped lock, the two
snapshots being compared, the backoff arithmetic, and the staged replacement
that survives a crash between its billed parts.

The transfer hook is opaque on purpose: `WorkspaceTransfer` says what the
successor needs mounted and prefixed, not how the old session's work is
carried, so the checkpoint machinery can change without this module moving.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import structlog
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsCustomSkill,
    BetaManagedAgentsSession,
)
from anthropic.types.beta.beta_managed_agents_system_content_block_param import (
    BetaManagedAgentsSystemContentBlockParam,
)
from anthropic.types.beta.session_create_params import Resource
from cryptography.fernet import MultiFernet
from daimon.core.agent_mcp_credentials import resolve_hidden_mcp_server_names
from daimon.core.config import GithubAppSettings
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.github_app_session import effective_repo_urls
from daimon.core.session_fence_retry import try_fence
from daimon.core.session_ports_compat import retrieve_session_record, session_scope
from daimon.core.session_snapshot import (
    SessionSnapshot,
    desired_snapshot,
    fingerprint_identity,
    fingerprint_mutable,
    hash_env_bytes,
    snapshot_from_retrieved_session,
)
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.agent_memory_stores import get_memory_store_id
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.domain import (
    SessionPreparationRow,
    ThreadSessionRow,
    TransferKind,
    UnsavedWorkChoice,
)
from daimon.core.stores.github_access import get_agent_mode
from daimon.core.stores.thread_sessions import record_snapshot
from daimon.core.tool_safety import ToolSafetyPolicy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.turn.admission import Admission
    from daimon.core.turn.deps import TurnDeps
    from daimon.core.turn.prepare import FreshSession

log = structlog.get_logger(__name__)

# Namespace prefix for the pg_advisory_xact_lock key, so this subsystem's keys
# can never collide with another advisory-lock user in the same database.
LOCK_NAMESPACE = "session_preparation"

PreparationStageName = Literal["decide", "update", "checkpoint", "upload", "create"]

# Backoff after a failed replacement: doubling minutes, capped.
MAX_BACKOFF_MINUTES = 60


@dataclass(frozen=True, slots=True)
class PreparedReplacement:
    """What a transfer hook produced for the successor session to start from."""

    extra_resources: tuple[Resource, ...]
    """File resources to mount at `sessions.create` — the carried work."""

    transfer_file_id: str | None
    transfer_kind: TransferKind
    user_prefix: str
    """Prepended to the successor's first user message. Never trusted content."""

    system_blocks: tuple[BetaManagedAgentsSystemContentBlockParam, ...] = ()
    """Empty when the destination model does not accept `system.message`."""


class WorkspaceTransfer(Protocol):
    """Carry one session's work into its successor. Implemented outside this module.

    `transfer_id` is stable across retries of the same preparation, so an
    implementation that already produced a bundle for it can return that one
    instead of paying for a second checkpoint turn.

    `unsaved_work` is the caller's standing answer about uncommitted changes in
    a mounted repository, read off their mapping row. None means they were
    never asked, which carries the same meaning as `"copy"`: capture the work.

    `before_send` is the caller's last access decision, run right before a
    checkpoint message is sent into the old session.
    """

    async def __call__(
        self,
        *,
        old_session_id: str,
        old_snapshot: SessionSnapshot,
        transfer_id: uuid.UUID,
        deadline: dt.datetime,
        destination_model_id: str,
        destination_agent_name: str,
        requested_work: str | None,
        unsaved_work: UnsavedWorkChoice | None = None,
        before_send: Callable[[], Awaitable[None]] | None = None,
    ) -> PreparedReplacement: ...


class FreshSessionFactory(Protocol):
    """`turn.prepare.create_fresh_session`, injected to keep imports one-way.

    `turn.prepare` is the module every adapter test patches `create_session`
    on, so session creation has to stay there; passing the function in is what
    lets this module orchestrate it without importing the module that calls it.
    """

    async def __call__(
        self,
        deps: TurnDeps,
        admission: Admission,
        *,
        tenant_id: uuid.UUID,
        platform: str,
        thread_id: str,
        session_account_id: uuid.UUID,
        extra_resources: tuple[Resource, ...] = (),
        predecessor_id: uuid.UUID | None = None,
        transfer_file_id: str | None = None,
        transfer_kind: TransferKind | None = None,
    ) -> FreshSession: ...


async def lock_preparation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    account_id: uuid.UUID,
) -> None:
    """Serialize preparation for one caller's thread session, for this transaction.

    Nonblocking: a contending caller releases its transaction and permit,
    retries, then re-reads the row. Racing mentions must decide against the
    latest row under this fence so they create one successor. Hashed inside
    Postgres (`hashtextextended`), never with Python's salted `hash()`.
    """
    key = f"{LOCK_NAMESPACE}:{tenant_id}:{platform}:{thread_id}:{account_id}"
    await try_fence(session, key)


def turn_is_active(row: ThreadSessionRow, *, now: dt.datetime) -> bool:
    """Is a turn still plausibly running on this mapping?

    A marker older than the per-turn ceiling cannot belong to a live turn — the
    turn that wrote it either cleared it or died with its process — so an
    abandoned marker must not defer every later change forever.
    """
    if row.active_turn_message_id is None or row.active_turn_started_at is None:
        return False
    # Import lazily: turn.__init__ re-exports run, which imports these stages.
    from daimon.core.turn.ceiling import TURN_CEILING_S

    return (now - row.active_turn_started_at).total_seconds() < TURN_CEILING_S


def retry_after(preparation: SessionPreparationRow) -> dt.datetime:
    """When a failed preparation for the same target may be attempted again."""
    return preparation.updated_at + dt.timedelta(
        minutes=min(2**preparation.attempts, MAX_BACKOFF_MINUTES)
    )


def failure_reason(stage: PreparationStageName, error: BaseException) -> str:
    """Encode which step failed into the stored reason, so a retry can report it."""
    return f"{stage}: {error}"


def failed_stage(reason: str | None) -> PreparationStageName:
    """Recover the failing step from a stored reason; `create` when unreadable."""
    head = (reason or "").partition(":")[0]
    if head in ("decide", "update", "checkpoint", "upload", "create"):
        return head
    return "create"


async def recorded_snapshot(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    existing: ThreadSessionRow,
    observed: BetaManagedAgentsSession | None,
) -> SessionSnapshot | None:
    """What a REUSED session is running, backfilling a pre-continuity row once.

    The recorded snapshot answers this without any MA call. A row written
    before snapshots existed has none, so the session is read once and the
    snapshot written back — `check_session_agent` hands over the response when
    its own legacy `ma_agent_id` branch already fetched it, so a legacy row
    costs at most one `sessions.retrieve` per bind.

    Returns None when the session is gone (404): there is nothing to compare a
    configuration against, and the turn's own dead-session recovery is what
    replaces it.
    """
    if existing.effective_config is not None:
        return existing.effective_config

    if observed is None:
        try:
            observed = await retrieve_session_record(
                anthropic,
                existing.ma_session_id,
                scope=session_scope(
                    tenant_id=existing.tenant_id,
                    account_id=existing.account_id,
                    call_site="session_preparation_stages:recorded_snapshot",
                ),
            )
        except APIStatusError as error:
            if error.status_code == 404:
                return None
            raise

    snapshot = snapshot_from_retrieved_session(observed)
    async with sessionmaker() as session, session.begin():
        await record_snapshot(
            session,
            id=existing.id,
            snapshot=snapshot,
            identity_fingerprint=fingerprint_identity(snapshot),
            mutable_fingerprint=fingerprint_mutable(snapshot),
        )
    return snapshot


async def desired_snapshot_for(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    agent: BetaManagedAgentsAgent,
    environment_id: str,
    tenant_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    account_id: uuid.UUID,
    github_app: GithubAppSettings | None = None,
    fernet: MultiFernet | None = None,
    is_external: bool = False,
    recorded: SessionSnapshot | None,
    tool_safety: ToolSafetyPolicy,
    public_url: str | None,
    memory_read_only: bool = False,
    asks_before_publishing: bool = False,
    channel_skills: Sequence[BetaManagedAgentsCustomSkill] = (),
) -> SessionSnapshot:
    """What a session created right now, for this caller, would freeze.

    Four indexed DB reads and no MA call, so an unchanged configuration costs
    nothing at bind time. The fourth is `account_id`'s: which of the agent's
    MCP servers only somebody else's OAuth grant can authenticate, since
    `create_session` leaves those off this caller's session and the hashes
    have to describe the session that would actually be created.

    Two fields are deliberately carried over from `recorded` rather than
    re-derived:

    - `vault_id` — the per-(account, agent) vault is get-or-created at session
      create and stays that caller's vault; re-reading it would cost a
      `vaults.list()` on every turn to detect a change no daimon path makes.
    - `repo_token_issued_at` — an age, not a configuration; the compatibility
      decision reads it off the recorded snapshot to time rotation.

    `recorded` is None only for a row whose snapshot could not be read at all;
    the carried fields are then unknown rather than unchanged.

    The repo URL is rebuilt in the shape `build_repo_resource` mounts
    (`https://github.com/<owner/repo>`) so a binding and a mounted resource
    compare equal. A bound repo with no resolvable clone token never reaches
    here — `create_session` raises instead of mounting nothing.
    """
    async with sessionmaker() as session:
        rows = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_uuid)
        mode = await get_agent_mode(session, tenant_id=tenant_id, agent_id=agent_uuid)
        if mode == "app":
            rows = [row for row in rows if row.key not in ("GH_TOKEN", "GITHUB_TOKEN")]
        binding = (
            None
            if mode == "app"
            else await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)
        )
        memory_store_id = await get_memory_store_id(
            session, tenant_id=tenant_id, agent_id=agent_uuid
        )

    return desired_snapshot(
        agent,
        hidden_mcp_server_names=await resolve_hidden_mcp_server_names(
            sessionmaker,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            account_id=account_id,
            server_urls={server.name: server.url for server in agent.mcp_servers},
        ),
        environment_id=environment_id,
        github_mode=mode,
        repo_urls=(
            await effective_repo_urls(
                sessionmaker,
                tenant_id=tenant_id,
                agent_id=agent_uuid,
                account_id=account_id,
                is_external=is_external,
                config=github_app or GithubAppSettings(),
                fernet=fernet,
            )
            if mode == "app"
            else ()
        ),
        env_sha256=hash_env_bytes(assemble_env_bytes(rows)) if rows else None,
        repo_url=None if binding is None else f"https://github.com/{binding.repo_url}",
        repo_branch=None if binding is None else binding.default_branch,
        memory_store_id=memory_store_id,
        memory_read_only=memory_read_only,
        asks_before_publishing=asks_before_publishing,
        vault_id=None if recorded is None else recorded.vault_id,
        env_file_id=None if recorded is None else recorded.env_file_id,
        repo_mount_path=None if recorded is None else recorded.repo_mount_path,
        repo_token_issued_at=None if recorded is None else recorded.repo_token_issued_at,
        channel_skills=channel_skills,
        tool_safety=tool_safety,
        public_url=public_url,
    )

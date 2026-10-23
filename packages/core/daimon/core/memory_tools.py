"""Memory for provider turns that cannot mount it: Daimon's own memory tools.

An Anthropic Managed Agents turn mounts its memory stores natively. A Gemini (or
later OpenAI) turn reaches the same stores through these tools instead, on its
per-turn `provider_turn` MCP credential. What a call may touch comes only from
that turn's `TurnMemoryGrant`, looked up by the credential's jti: never from
the token's claims and never from tool input. The model names a memory by a
path under `shared/` (the agent's store) or `personal/` (the caller's own
store, only for a single-caller turn); it never names a store.

Read-only is enforced here, on every write and delete, from the grant and the
tenant policy as it is now (a seal or the DM policy switched on mid-turn makes
the next write refuse), because there is no native mount to enforce it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

import structlog
from anthropic import APIStatusError, AsyncAnthropic
from daimon.core.stores.turn_memory_grants import TurnMemoryGrantRow, get_turn_memory_grant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.turn.admission import Admission

_log = structlog.get_logger(__name__)

#: The tools a provider turn's Daimon MCP connection carries for memory.
MEMORY_TOOLS: tuple[str, ...] = ("memory_list", "memory_read", "memory_write", "memory_delete")

MemoryKind = Literal["shared", "personal"]
_PREFIXES: dict[MemoryKind, str] = {"shared": "shared/", "personal": "personal/"}
_MAX_PATH = 512
#: One memory's content, in UTF-8 bytes.
_MAX_CONTENT_BYTES = 100_000
#: Memories one store may hold through these tools, and one listing returns.
_MAX_MEMORIES = 1_000


class MemoryRefused(Exception):
    """A memory tool call refused with a plain, model-facing reason."""


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    path: str
    content_sha256: str


def turn_memory_grant(
    admission: Admission,
    *,
    jti: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    worker: bool,
    expires_at: datetime,
) -> TurnMemoryGrantRow | None:
    """The grant a provider turn's credential carries, from that turn's own admission.

    The same facts a native mount follows: a shared thread has no personal store,
    and the admitted memory posture (seals, a read-only DM) is the read-only flag.
    None without the admission's recorded grant, for a held turn (natively it
    mounts only its channel's store, which these tools do not serve) and for a
    worker (workers never get memory): no memory is offered then.
    """
    if admission.grant is None or admission.hold is not None or worker:
        return None
    return TurnMemoryGrantRow(
        jti=jti,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        agent_name=admission.config.agent_name or admission.agent.name,
        personal=admission.shared_owner is None,
        read_only=admission.memory_read_only,
        is_dm=admission.grant.is_dm,
        platform=platform,
        channel_id=admission.grant.channel_id,
        thread_id=admission.grant.thread_id,
        expires_at=expires_at,
    )


async def turn_grant(
    sessions: async_sessionmaker[AsyncSession],
    *,
    jti: uuid.UUID | None,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    now: datetime,
) -> TurnMemoryGrantRow:
    """This credential's grant, bound to the verified identity it was minted for."""
    if jti is None or agent_id is None:
        raise MemoryRefused("Memory is not available in this conversation.")
    async with sessions() as session:
        grant = await get_turn_memory_grant(session, jti=jti)
    if (
        grant is None
        or grant.tenant_id != tenant_id
        or grant.account_id != account_id
        or grant.agent_id != agent_id
        or grant.expires_at <= now
    ):
        raise MemoryRefused("Memory is not available in this conversation.")
    return grant


async def writable_now(
    sessions: async_sessionmaker[AsyncSession], grant: TurnMemoryGrantRow
) -> bool:
    """The grant's posture rechecked against the policy as it is now; never relaxes."""
    from daimon.core.permissions import limiting_ids_at
    from daimon.core.stores.access_policy import load_access_policy

    if grant.read_only:
        return False
    async with sessions() as session:
        policy = await load_access_policy(session, tenant_id=grant.tenant_id)
    if limiting_ids_at(policy, channel_id=grant.channel_id, thread_id=grant.thread_id):
        return False
    return not (grant.is_dm and policy.dm_memory_read_only)


def split_path(path: str) -> tuple[MemoryKind, str]:
    """`shared/notes.md` -> ("shared", "/notes.md"). Refuses anything else."""
    if len(path) > _MAX_PATH or "\\" in path or any(ord(c) < 32 for c in path):
        raise MemoryRefused("Name a memory as shared/<path> or personal/<path>.")
    for kind, prefix in _PREFIXES.items():
        if path.startswith(prefix):
            inner = path[len(prefix) :]
            parts = inner.split("/")
            if not inner or any(part in ("", ".", "..") for part in parts):
                raise MemoryRefused("Name a memory as shared/<path> or personal/<path>.")
            return kind, "/" + inner
    raise MemoryRefused("Name a memory as shared/<path> or personal/<path>.")


def _shown(kind: MemoryKind, native_path: str) -> str:
    return _PREFIXES[kind] + native_path.lstrip("/")


def _kinds(grant: TurnMemoryGrantRow) -> tuple[MemoryKind, ...]:
    return ("shared", "personal") if grant.personal else ("shared",)


async def _store(
    anthropic: AsyncAnthropic,
    sessions: async_sessionmaker[AsyncSession],
    grant: TurnMemoryGrantRow,
    kind: MemoryKind,
    *,
    create: bool,
) -> str | None:
    """The store this turn may use for `kind`; created (race-safe) only for a write."""
    from daimon.core.memory_resource import (
        ensure_memory_store_and_mount,
        ensure_personal_memory_store_and_mount,
    )
    from daimon.core.stores.agent_memory_stores import (
        get_memory_store_id,
        get_personal_memory_store_id,
    )

    if kind == "personal" and not grant.personal:
        raise MemoryRefused("Personal memory is not available in a shared conversation.")
    if not create:
        async with sessions() as session:
            if kind == "shared":
                return await get_memory_store_id(
                    session, tenant_id=grant.tenant_id, agent_id=grant.agent_id
                )
            return await get_personal_memory_store_id(
                session,
                tenant_id=grant.tenant_id,
                agent_id=grant.agent_id,
                account_id=grant.account_id,
            )
    if kind == "shared":
        mount = await ensure_memory_store_and_mount(
            anthropic,
            sessions,
            tenant_id=grant.tenant_id,
            agent_id=grant.agent_id,
            agent_name=grant.agent_name,
        )
    else:
        mount = await ensure_personal_memory_store_and_mount(
            anthropic,
            sessions,
            tenant_id=grant.tenant_id,
            agent_id=grant.agent_id,
            account_id=grant.account_id,
            agent_name=grant.agent_name,
        )
    return mount["memory_store_id"]


def _scope(grant: TurnMemoryGrantRow):
    from daimon.core.mux_backend import resource_scope

    return resource_scope(tenant_id=str(grant.tenant_id))


async def _entries(anthropic: AsyncAnthropic, store_id: str, grant: TurnMemoryGrantRow):
    from daimon.core.memory_view import list_memory_records

    return await list_memory_records(anthropic, store_id, scope=_scope(grant))


async def list_memories(
    anthropic: AsyncAnthropic,
    sessions: async_sessionmaker[AsyncSession],
    grant: TurnMemoryGrantRow,
) -> list[MemoryEntry]:
    out: list[MemoryEntry] = []
    for kind in _kinds(grant):
        store_id = await _store(anthropic, sessions, grant, kind, create=False)
        if store_id is None:
            continue
        out.extend(
            MemoryEntry(path=_shown(kind, record.path), content_sha256=record.content_sha256)
            for record in await _entries(anthropic, store_id, grant)
        )
    return out[:_MAX_MEMORIES]


async def read_memory_at(
    anthropic: AsyncAnthropic,
    sessions: async_sessionmaker[AsyncSession],
    grant: TurnMemoryGrantRow,
    path: str,
) -> str | None:
    from daimon.core.memory_view import get_memory_content

    kind, native = split_path(path)
    store_id = await _store(anthropic, sessions, grant, kind, create=False)
    if store_id is None:
        return None
    return await get_memory_content(anthropic, store_id, native, scope=_scope(grant))


async def write_memory_at(
    anthropic: AsyncAnthropic,
    sessions: async_sessionmaker[AsyncSession],
    grant: TurnMemoryGrantRow,
    path: str,
    content: str,
    *,
    expected_content_sha256: str | None,
) -> str:
    """Create the memory, or replace it only while it still has `expected_content_sha256`."""
    from daimon.core.memory_view import delete_memory_records
    from daimon.core.mux_compat import read_memory, write_memory

    kind, native = split_path(path)
    if len(content.encode()) > _MAX_CONTENT_BYTES:
        raise MemoryRefused("That memory is too large.")
    if not await writable_now(sessions, grant):
        raise MemoryRefused("Memory is read-only in this conversation.")
    store_id = await _store(anthropic, sessions, grant, kind, create=True)
    assert store_id is not None
    records = await _entries(anthropic, store_id, grant)
    existing = [r for r in records if r.path == native]
    previous: str | None = None
    if existing:
        if expected_content_sha256 is None:
            raise MemoryRefused(
                "A memory already exists there. Read it, then pass its content_sha256 to "
                "replace it."
            )
        if existing[0].content_sha256 != expected_content_sha256:
            raise MemoryRefused("That memory changed since you read it. Read it again.")
        # Kept so a failed replacement never loses the memory it replaces.
        previous = (
            await read_memory(anthropic, store_id, existing[0].id, scope=_scope(grant))
        ).content
        deleted = await delete_memory_records(anthropic, store_id, existing, scope=_scope(grant))
        if deleted != len(existing):
            raise MemoryRefused("That memory changed since you read it. Read it again.")
    elif expected_content_sha256 is not None:
        raise MemoryRefused("That memory no longer exists. Read it again.")
    elif len(records) >= _MAX_MEMORIES:
        raise MemoryRefused("This memory is full. Replace or delete a memory first.")
    try:
        await write_memory(anthropic, store_id, native, content, scope=_scope(grant))
    except Exception as error:
        if previous is not None:
            try:
                await write_memory(anthropic, store_id, native, previous, scope=_scope(grant))
            except Exception:
                # Never the model-chosen path itself: a digest identifies it.
                _log.warning(
                    "memory_tools.restore_failed",
                    path_sha256=hashlib.sha256(native.encode()).hexdigest()[:16],
                )
        if isinstance(error, APIStatusError) and error.status_code in (409, 412):
            raise MemoryRefused("Another writer saved that memory first. Read it again.") from error
        raise
    return hashlib.sha256(content.encode()).hexdigest()


async def delete_memory_at(
    anthropic: AsyncAnthropic,
    sessions: async_sessionmaker[AsyncSession],
    grant: TurnMemoryGrantRow,
    path: str,
    *,
    expected_content_sha256: str,
) -> bool:
    from daimon.core.memory_view import delete_memory_records

    kind, native = split_path(path)
    if not await writable_now(sessions, grant):
        raise MemoryRefused("Memory is read-only in this conversation.")
    store_id = await _store(anthropic, sessions, grant, kind, create=False)
    if store_id is None:
        return False
    matching = [
        r
        for r in await _entries(anthropic, store_id, grant)
        if r.path == native and r.content_sha256 == expected_content_sha256
    ]
    if not matching:
        raise MemoryRefused("That memory changed or is gone. Read it again.")
    return await delete_memory_records(anthropic, store_id, matching, scope=_scope(grant)) == len(
        matching
    )

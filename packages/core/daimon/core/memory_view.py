"""The memory stores behind a chat's agent, for the `memory` commands.

The shared store is read-only here. The caller's OWN personal store can also be
listed, read and cleared: it is always resolved from the account of the person
who typed the command, and no argument can name another person, so nobody can
see or clear anyone else's.

Command grammar, the same on Discord, Slack and Teams (`parse_memory_args`):

- `memory` / `memory <path>`: the shared store, as before.
- `memory mine` / `memory mine <path>`: the caller's personal store.
- `memory forget-mine`: shows what would be deleted and a confirmation code.
- `memory forget-mine <code>`: deletes exactly the memories that were shown,
  only when the code still matches the store as listed (ids, paths and
  content hashes), so a stale or guessed code deletes nothing, and a memory
  written or changed meanwhile is kept and reported.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Literal

import anthropic as anthropic_errors
from anthropic import AsyncAnthropic
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_compat import delete_memory, list_memory_entries, read_memory
from daimon.core.rule_views import is_memory_hidden
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.agent_memory_stores import (
    get_memory_store_id,
    get_personal_memory_store_id,
)
from daimon.core.stores.domain import Platform
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class ChannelAgent:
    """The agent answering at a place, and who asked."""

    agent_name: str
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    account_id: uuid.UUID


async def resolve_channel_agent(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: Platform,
    user_id: str,
    channel_id: str,
    default: DeploymentDefault,
    thread_id: str | None = None,
) -> ChannelAgent | None:
    """The agent answering in `channel_id` (or `thread_id` under it) for `user_id`.

    None when no agent is configured there or its pin or channel isolation
    hides it from that place. Raises DaimonError when the configured agent is
    missing on the MA side.
    """
    # Never committed: a first-contact principal only scopes this read.
    async with sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=user_id
        )
        scope = ScopeContext(
            account_id=principal.account_id,
            tenant_id=tenant_id,
            channel_id=channel_id,
            platform=platform,
            thread_id=thread_id,
        )
        config = await resolve_config(session, context=scope, default=default)
        policy = await load_access_policy(session, tenant_id=tenant_id)
    if config.agent_name is None:
        return None
    agent = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=config.agent_name)
    if agent is None:
        raise DaimonError(f"Configured agent '{config.agent_name}' not found.")
    names = (config.agent_name, *agent_pin_names(agent.name, agent.metadata))
    if is_memory_hidden(
        policy,
        agent_names=names,
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id if thread_id is not None else None,
    ):
        return None
    return ChannelAgent(
        agent_name=config.agent_name,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        account_id=principal.account_id,
    )


async def get_channel_memory_store(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    platform: Platform,
    user_id: str,
    channel_id: str,
    default: DeploymentDefault,
    thread_id: str | None = None,
) -> tuple[str, str] | None:
    """`(agent_name, memory_store_id)` of the shared store for the agent answering in `channel_id`.

    With `thread_id`, the agent answering in that thread under it, so a thread
    bound to another agent shows that agent's memory.
    None when no agent is configured there, it has no memory store yet, or its
    pin or channel isolation hides it from that place.
    Raises DaimonError when the configured agent is missing on the MA side.
    """
    agent = await resolve_channel_agent(
        sessionmaker,
        anthropic,
        tenant_id=tenant_id,
        platform=platform,
        user_id=user_id,
        channel_id=channel_id,
        default=default,
        thread_id=thread_id,
    )
    if agent is None:
        return None
    async with sessionmaker() as session:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent.agent_id)
    return None if store_id is None else (agent.agent_name, store_id)


async def get_own_personal_memory_store(
    sessionmaker: async_sessionmaker[AsyncSession], agent: ChannelAgent
) -> str | None:
    """The personal store of the person who asked, for this agent, or None.

    Keyed only by `agent.account_id`, the invoking principal's own account.
    """
    async with sessionmaker() as session:
        return await get_personal_memory_store_id(
            session, tenant_id=agent.tenant_id, agent_id=agent.agent_id, account_id=agent.account_id
        )


type MemoryRequestKind = Literal[
    "shared_list", "shared_show", "mine_list", "mine_show", "forget_ask", "forget_confirm"
]


@dataclass(frozen=True, slots=True)
class MemoryRequest:
    kind: MemoryRequestKind
    arg: str | None = None  # a path, or the confirmation code


MINE = "mine"
FORGET_MINE = "forget-mine"


def parse_memory_args(text: str | None) -> MemoryRequest:
    """Read a `memory` command's arguments (see the module docstring)."""
    words = (text or "").strip().split(maxsplit=1)
    if not words:
        return MemoryRequest("shared_list")
    head, rest = words[0], (words[1].strip() if len(words) > 1 else "")
    if head.lower() == MINE:
        return MemoryRequest("mine_show", rest) if rest else MemoryRequest("mine_list")
    if head.lower() == FORGET_MINE:
        return MemoryRequest("forget_confirm", rest) if rest else MemoryRequest("forget_ask")
    return MemoryRequest("shared_show", (text or "").strip())


def is_strict_memory_args(text: str) -> bool:
    """Whether `text` reads unambiguously as `memory` arguments, not prose.

    For a platform where the command is a plain chat word (Teams): a shared
    path, `mine` with no or a `/path`, or `forget-mine` with no or a single
    code-shaped word. Anything else is a message to the agent.
    """
    text = text.strip()
    if not text or text.startswith("/"):
        return True
    request = parse_memory_args(text)
    if request.kind in ("mine_list", "forget_ask"):
        return True
    if request.kind == "mine_show":
        return (request.arg or "").startswith("/")
    if request.kind == "forget_confirm":
        arg = request.arg or ""
        return len(arg) == 6 and all(c in "0123456789abcdefABCDEF" for c in arg)
    return False


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One memory as listed: what a forget confirmation is bound to."""

    id: str
    path: str
    content_sha256: str


async def list_memory_records(
    anthropic: AsyncAnthropic, store_id: str, *, scope: Scope | None = None
) -> list[MemoryRecord]:
    """Every memory in the store, sorted by path, with its id and content hash."""
    backend_scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.memory_view:list_memory_records"
    )
    page = list_memory_entries(anthropic, store_id, path_prefix="/", scope=backend_scope)
    records = [
        MemoryRecord(id=item.id, path=item.path, content_sha256=item.content_sha256)
        async for item in page
        if item.type == "memory"
    ]
    return sorted(records, key=lambda record: record.path)


def forget_code(store_id: str, records: list[MemoryRecord]) -> str:
    """A short code naming exactly these memories of this store, as they are now.

    Bound to each memory's id, path and content hash, so a memory written,
    renamed or changed after the preview gives another code. Stateless: the
    same contents give the same code.
    """
    lines = sorted(f"{r.id}\t{r.path}\t{r.content_sha256}" for r in records)
    digest = hashlib.sha256("\n".join([store_id, *lines]).encode()).hexdigest()
    return digest[:6]


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


@dataclass(frozen=True, slots=True)
class PersonalMemoryReply:
    """What to tell the person; each adapter renders it in its own format."""

    title: str
    paths: tuple[str, ...] = ()
    content: str | None = None
    hint: str | None = None


async def personal_memory_reply(
    anthropic: AsyncAnthropic,
    *,
    agent_name: str,
    store_id: str | None,
    request: MemoryRequest,
    command: str,
    scope: Scope | None = None,
) -> PersonalMemoryReply:
    """Answer a `mine` or `forget-mine` request against the caller's own store.

    `store_id` must be the caller's own (`get_own_personal_memory_store`).
    `command` is how the platform spells the command (`/memory`, `memory`).
    `scope` is the caller's tenant scope for the provider reads and deletes.
    """
    empty = PersonalMemoryReply(f"You have no personal memory with {agent_name}.")
    if store_id is None:
        return empty
    if request.kind == "mine_show":
        path = request.arg or ""
        content = await get_memory_content(anthropic, store_id, path, scope=scope)
        if content is None:
            return PersonalMemoryReply(
                f"No file called {path} in your personal memory.",
                hint=f"{command} {MINE} lists them.",
            )
        return PersonalMemoryReply(path, content=content)
    # Listed once: a forget confirmation is checked against, and deletes only,
    # exactly this snapshot.
    records = await list_memory_records(anthropic, store_id, scope=scope)
    paths = [record.path for record in records]
    if request.kind == "mine_list":
        if not paths:
            return empty
        return PersonalMemoryReply(
            f"Your personal memory with {agent_name} ({_files(len(paths))}). Only you see this.",
            paths=tuple(paths),
            hint=f"{command} {MINE} <path> shows one. {command} {FORGET_MINE} clears it.",
        )
    code = forget_code(store_id, records)
    if not paths:
        return empty
    if request.kind == "forget_ask":
        return PersonalMemoryReply(
            f"This deletes {_files(len(paths))} from your personal memory with {agent_name}. "
            "Nobody else's memory, and the shared memory, stay as they are.",
            paths=tuple(paths),
            hint=f"To confirm, run {command} {FORGET_MINE} {code}",
        )
    if request.kind != "forget_confirm":
        raise ValueError(f"not a personal memory request: {request.kind}")
    if (request.arg or "").strip().lower() != code:
        return PersonalMemoryReply(
            "Nothing was deleted: that code doesn't match your personal memory as it is now.",
            hint=f"Run {command} {FORGET_MINE} to get a new code.",
        )
    deleted = await delete_memory_records(anthropic, store_id, records, scope=scope)
    kept = len(await list_memory_records(anthropic, store_id, scope=scope))
    title = f"Deleted {_files(deleted)} from your personal memory with {agent_name}."
    if kept:
        title += (
            f" Kept {_files(kept)} written or changed since you were shown the list; "
            f"run {command} {FORGET_MINE} again to clear them too."
        )
    return PersonalMemoryReply(title)


async def delete_memory_records(
    anthropic: AsyncAnthropic,
    store_id: str,
    records: list[MemoryRecord],
    *,
    scope: Scope | None = None,
) -> int:
    """Delete exactly `records`, each only while its content is still what was listed.

    Never lists again: a memory written after `records` were listed is not in
    them and stays. One whose content changed since fails the API's
    `expected_content_sha256` precondition and stays too. The store and its
    binding stay. Returns how many were deleted.
    """
    backend_scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.memory_view:delete_memory_records"
    )
    deleted = 0
    for record in records:
        try:
            await delete_memory(
                anthropic,
                store_id,
                record.id,
                expected_content_sha256=record.content_sha256,
                scope=backend_scope,
            )
        except anthropic_errors.APIStatusError as error:
            # 404: already gone. 409/412: changed since the preview, so kept.
            if error.status_code not in (404, 409, 412):
                raise
            continue
        deleted += 1
    return deleted


async def list_memory_paths(
    anthropic: AsyncAnthropic, store_id: str, *, scope: Scope | None = None
) -> list[str]:
    """Every memory path in the store, sorted."""
    return [record.path for record in await list_memory_records(anthropic, store_id, scope=scope)]


async def get_memory_content(
    anthropic: AsyncAnthropic, store_id: str, path: str, *, scope: Scope | None = None
) -> str | None:
    """The memory at exactly `path`, or None when there is none."""
    backend_scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.memory_view:get_memory_content"
    )
    for record in await list_memory_records(anthropic, store_id, scope=backend_scope):
        if record.path == path:
            memory = await read_memory(anthropic, store_id, record.id, scope=backend_scope)
            return memory.content or ""
    return None

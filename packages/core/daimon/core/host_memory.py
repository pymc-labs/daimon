"""Daimon-hosted memory tools over the agent's existing memory stores.

A backend without native memory stores (OpenAI, Gemini) reaches the same
shared, personal and channel stores a Claude session mounts, through the host.
The store follows the turn's credential (`choose_store`), never an argument
that could name another place or person. Self-hosted turns never call
Anthropic, so they get no memory at all (`tests/parity`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

from anthropic import AsyncAnthropic
from daimon.core.memory_resource import (
    ensure_channel_memory_store_and_mount,
    ensure_memory_store_and_mount,
    ensure_personal_memory_store_and_mount,
)
from daimon.core.memory_view import MemoryRecord, list_memory_records
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import create_memory, delete_memory, read_memory, update_memory
from daimon.core.turn_hold import TurnHold
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

HOST_MEMORY_TOOLS: Final = ("memory_view", "memory_create", "memory_str_replace", "memory_delete")
HOST_MEMORY_TAG: Final = "host-memory"
MAX_MEMORY_BYTES: Final = 102_400
_MAX_PATH_BYTES: Final = 1024

StoreKind = Literal["shared", "personal", "channel"]


class HostMemoryError(ValueError):
    """A memory request refused with a reason the agent can act on."""


@dataclass(frozen=True, slots=True)
class HostMemoryPlace:
    """Where a turn's memory lives, read from its credential and turn origin."""

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    account_id: uuid.UUID
    hold: TurnHold | None
    shared_thread: bool
    read_only: bool


def choose_store(
    place: HostMemoryPlace,
    *,
    personal: bool,
    shared_on: bool,
    personal_on: bool,
    channel_on: bool,
) -> StoreKind:
    """The store a Claude session at this place would mount for this request."""
    if place.hold is not None:
        # A held turn sees only its channel's store, as on Claude.
        if personal:
            raise HostMemoryError("Personal memory is not used in this channel.")
        if not channel_on:
            raise HostMemoryError("Memory is off in this channel.")
        return "channel"
    if personal:
        if place.shared_thread:
            raise HostMemoryError("A shared conversation has no personal memory.")
        if not personal_on:
            raise HostMemoryError("Personal memory is off here.")
        return "personal"
    if not shared_on:
        raise HostMemoryError("Memory is off here.")
    return "shared"


def check_path(path: str) -> str:
    """The memory store's own path rules, refused before any call."""
    segments = path.split("/")[1:]
    if (
        not path.startswith("/")
        or len(path.encode()) > _MAX_PATH_BYTES
        or any(segment in ("", ".", "..") for segment in segments[:-1])
        or segments[-1] in (".", "..")
    ):
        raise HostMemoryError(f"Invalid memory path: {path!r}")
    return path


def replace_once(content: str, old: str, new: str) -> str:
    """`content` with the one occurrence of `old` replaced; refuses zero or many."""
    count = content.count(old) if old else 0
    if count != 1:
        raise HostMemoryError(
            f"old_str must appear exactly once in the memory; it appears {count} times."
        )
    return content.replace(old, new, 1)


def check_content(content: str) -> str:
    if len(content.encode()) > MAX_MEMORY_BYTES:
        raise HostMemoryError(f"A memory holds at most {MAX_MEMORY_BYTES} bytes.")
    return content


class HostMemory:
    """One turn's memory store, provisioned on first use like a session mount."""

    def __init__(
        self,
        anthropic: AsyncAnthropic,
        session_factory: async_sessionmaker[AsyncSession],
        place: HostMemoryPlace,
        kind: StoreKind,
    ) -> None:
        self._anthropic = anthropic
        self._session_factory = session_factory
        self._place = place
        self._kind = kind
        self._scope = resource_scope(tenant_id=str(place.tenant_id))

    async def _store_id(self) -> str:
        place, common = self._place, (self._anthropic, self._session_factory)
        if self._kind == "channel":
            assert place.hold is not None  # choose_store
            mount = await ensure_channel_memory_store_and_mount(
                *common,
                tenant_id=place.tenant_id,
                agent_id=place.agent_id,
                platform=place.hold.platform,
                channel_id=place.hold.channel_id,
                agent_name=place.agent_name,
                read_only=place.read_only,
            )
        elif self._kind == "personal":
            mount = await ensure_personal_memory_store_and_mount(
                *common,
                tenant_id=place.tenant_id,
                agent_id=place.agent_id,
                account_id=place.account_id,
                agent_name=place.agent_name,
                read_only=place.read_only,
            )
        else:
            mount = await ensure_memory_store_and_mount(
                *common,
                tenant_id=place.tenant_id,
                agent_id=place.agent_id,
                agent_name=place.agent_name,
                read_only=place.read_only,
            )
        return mount["memory_store_id"]

    def _writable(self) -> None:
        if self._place.read_only:
            raise HostMemoryError("Memory is read-only here.")

    async def _records(self, store_id: str) -> list[MemoryRecord]:
        return await list_memory_records(self._anthropic, store_id, scope=self._scope)

    async def _record(self, store_id: str, path: str) -> MemoryRecord:
        found = next((r for r in await self._records(store_id) if r.path == path), None)
        if found is None:
            raise HostMemoryError(f"No memory at {path}.")
        return found

    async def view(self, path: str) -> str:
        """A file's content, or the paths under a directory."""
        path, store_id = check_path(path), await self._store_id()
        records = await self._records(store_id)
        exact = next((r for r in records if r.path == path), None)
        if exact is not None:
            memory = await read_memory(self._anthropic, store_id, exact.id, scope=self._scope)
            return memory.content or ""
        prefix = path if path.endswith("/") else f"{path}/"
        listed = [r.path for r in records if r.path.startswith(prefix)]
        return "\n".join(listed) if listed else f"No memories under {prefix}"

    async def create(self, path: str, content: str) -> str:
        self._writable()
        path, content, store_id = check_path(path), check_content(content), await self._store_id()
        if any(r.path == path for r in await self._records(store_id)):
            raise HostMemoryError(f"A memory already exists at {path}.")
        await create_memory(self._anthropic, store_id, path, content, scope=self._scope)
        return f"Saved {path}."

    async def str_replace(self, path: str, old: str, new: str) -> str:
        self._writable()
        path, store_id = check_path(path), await self._store_id()
        record = await self._record(store_id, path)
        memory = await read_memory(self._anthropic, store_id, record.id, scope=self._scope)
        content = check_content(replace_once(memory.content or "", old, new))
        await update_memory(
            self._anthropic,
            store_id,
            record.id,
            content,
            expected_content_sha256=record.content_sha256,
            scope=self._scope,
        )
        return f"Updated {path}."

    async def delete(self, path: str) -> str:
        self._writable()
        path, store_id = check_path(path), await self._store_id()
        record = await self._record(store_id, path)
        await delete_memory(
            self._anthropic,
            store_id,
            record.id,
            expected_content_sha256=record.content_sha256,
            scope=self._scope,
        )
        return f"Deleted {path}."

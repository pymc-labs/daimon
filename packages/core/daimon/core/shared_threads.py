"""Opt-in shared threads: one provider session per thread, whoever writes in it.

A thread is shared only when its channel's backend configuration says
`thread_mode="shared"` and `DAIMON_TURN__CHANNEL_BACKENDS` is on (otherwise
admission carries no revision at all), and never for a setup conversation.
Everything else keeps today's per-caller session, decided without a read.

A shared thread's session row belongs to a synthetic owner derived from the
thread itself, never a person's account and never the sentinel the removed
legacy shared mode used, so no caller's private history (nor that legacy
mode's) can ever be selected for it. Its binding lives in the state store's
shared slot (`mux.state.lease.Slot` with no account), pinned to the
configuration revision it was bound under: a later configuration change
applies to new threads, while this one stays on its revision, and is refused
visibly if that revision can no longer run.

A shared workspace must not act as any one caller, so it is created with no
caller credentials (no personal vault, MCP identity, personal servers or
GitHub user grant) and only agent-owned ones: the agent must run in app mode.
Charges stay with each caller, as the turn recorder writes them by the
writer's platform user.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.channel_backend import BackendUnsupported, check_backend
from daimon.core.stores import mux_state
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.state.lease import Slot
from mux.state.store import check_binding_successor
from sqlalchemy.ext.asyncio import AsyncSession

SHARED_OWNER_NAMESPACE = uuid.UUID("6f1c2f1e-7d0a-4b8e-9a51-3c9a2d5e8b41")
"""Namespace of shared-thread owners. Distinct from `uuid.NAMESPACE_URL`, which
the removed legacy shared mode used, so the two can never collide."""


@dataclass(frozen=True)
class SharedThread:
    """A thread whose turns share one session: its slot, owner and revision."""

    slot: Slot
    owner: uuid.UUID
    revision: ConfigRevision
    binding: ProviderBinding | None


def shared_slot(tenant_id: uuid.UUID, platform: str, channel_id: str, thread_id: str) -> Slot:
    channel = ChannelRef(tenant_id=str(tenant_id), platform=platform, channel_id=channel_id)
    return Slot(thread=ThreadRef(channel=channel, thread_id=thread_id))


def shared_owner(slot: Slot) -> uuid.UUID:
    """The session-row owner of a shared thread: a pure function of the thread."""
    channel = slot.thread.channel
    key = f"{channel.tenant_id}:{channel.platform}:{channel.channel_id}:{slot.thread.thread_id}"
    return uuid.uuid5(SHARED_OWNER_NAMESPACE, key)


def may_have_shared_binding(revision: ConfigRevision | None) -> bool:
    """Whether a thread under `revision` can be shared, so its binding must be read.

    No revision (unconfigured, or the flag off) and a channel configured once,
    per caller, read nothing. A channel whose configuration changed may have
    threads bound shared under an earlier revision, which stay shared.
    """
    if revision is None:
        return False
    return revision.thread_mode == "shared" or revision.local > 1


async def resolve_shared_thread(
    session: AsyncSession,
    revision: ConfigRevision | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str,
) -> SharedThread | None:
    """The thread's shared binding, or None when the thread runs per caller.

    A thread already bound shared keeps the revision it was bound under; one
    not yet bound is shared only if the channel's current revision says so.
    Raises `BackendUnsupported` if the revision the thread runs under cannot
    run here.
    """
    if not may_have_shared_binding(revision):
        return None
    assert revision is not None
    slot = shared_slot(tenant_id, platform, channel_id, thread_id)
    binding = await mux_state.get_binding(session, slot)
    if binding is None:
        if revision.thread_mode != "shared":
            return None
        return SharedThread(slot=slot, owner=shared_owner(slot), revision=revision, binding=None)
    pinned = await mux_state.get_config_revision(
        session, slot.thread.channel, binding.config_revision
    )
    if pinned is None:
        raise BackendUnsupported(f"revision {binding.config_revision} of the thread is missing")
    check_backend(pinned)
    return SharedThread(slot=slot, owner=shared_owner(slot), revision=pinned, binding=binding)


async def record_shared_binding(
    session: AsyncSession, shared: SharedThread, *, ma_session_id: str
) -> ProviderBinding:
    """Bind the thread's shared slot to `ma_session_id`; a new generation if it changed."""
    current = await mux_state.get_binding(session, shared.slot)
    if current is not None and current.native_refs.get("session") == ma_session_id:
        return current
    binding = ProviderBinding(
        id=str(shared.owner),
        thread=shared.slot.thread,
        provider="anthropic",
        profile=shared.revision.profile,
        native_refs={"session": ma_session_id},
        generation=(current.generation if current else 0) + 1,
        config_revision=shared.revision.local,
        legacy_account_id=None,
    )
    expected = current.generation if current else 0
    check_binding_successor(current, binding, expected_generation=expected)
    return await mux_state.put_binding(session, binding, expected_generation=expected)

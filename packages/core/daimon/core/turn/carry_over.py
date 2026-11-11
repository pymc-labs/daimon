"""Carry a thread's conversation over when it moves to another provider.

A confirmed switch releases the slot (`daimon.core.thread_release`) and the
released binding keeps the released session's refs (`previous_refs`). The next
provider's first session reads that session through its own provider's Events
port, and starts with the framing a binding replacement uses. Managed Agents
turns write no slot binding, so their conversation is found by thread session
row. A session that cannot be read, or a slot released before refs were kept,
gets the lost-workspace framing. Old sessions are left as they are.

A move to Managed Agents carries files too: they are uploaded before, and
mounted on, the session that was just created (`carry_into_managed_agents`).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

import structlog
from daimon.core.channel_backend import channel_ref
from daimon.core.errors import DaimonError
from daimon.core.mux_backend import TurnRuntime, managed_agents, resource_ref
from daimon.core.stores.thread_sessions import get_thread_session_at
from daimon.core.turn.admission import Admission
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.prepare import ContinuityOutcome, ProviderPreparationRequest
from daimon.core.turn.replace_binding import (
    OldSession,
    carry_files,
    carry_framing,
    hold_slot,
    read_turns,
)
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ChannelRef, Scope
from mux.contracts.ports import Artifacts
from mux.contracts.resources import ProviderBinding, ResourceBinding
from mux.errors import MuxError
from mux.profiles import MANAGED_AGENTS
from mux.state.lease import Slot
from mux.state.store import SESSION_REF, StateStore, is_released, previous_refs

log = structlog.get_logger()

SessionReader = Callable[
    [TurnRuntime, ConfigRevision, Scope, ProviderBinding, StateStore], OldSession
]
"""A provider's view of one of its old sessions: its ref and an Events port."""

_READERS: dict[str, SessionReader] = {}


def register_session_reader(profile: str, reader: SessionReader, *, fallback: bool = True) -> None:
    """Register how `profile`'s old sessions are read; a fallback keeps an override."""
    if fallback and profile in _READERS:
        return
    _READERS[profile] = reader


def carry_over_channel(
    admission: Admission, *, tenant_id: uuid.UUID, platform: str
) -> ChannelRef | None:
    """The channel a Managed Agents turn's slot hangs off; Claude default has no revision."""
    if admission.backend_revision is not None:
        return admission.backend_revision.channel
    if admission.origin_channel_id is None:
        return None
    return channel_ref(tenant_id, platform, admission.origin_channel_id)


@dataclass(frozen=True)
class CarriedOver:
    continuity: ContinuityOutcome
    mounts: tuple[ResourceBinding, ...] = ()


async def thread_predecessor(
    request: ProviderPreparationRequest, store: StateStore, slot: Slot
) -> ProviderBinding | None:
    """The session this slot's next provider carries over, as its binding was.

    No `session` ref means there was one that can no longer be found. None when
    the slot is bound (its host replaces it) or nothing came before.
    """
    released = await store.get_binding(slot, include_released=True)
    if released is not None and not is_released(released):
        return None
    refs = previous_refs(released) if released is not None else {}
    released_at = datetime.fromisoformat(refs["released_at"]) if "released_at" in refs else None
    to_managed_agents = request.admission.backend_revision is None or (
        request.admission.backend_revision.profile == MANAGED_AGENTS.profile_id
    )
    async with request.deps.sessionmaker() as session:
        row = await get_thread_session_at(
            session,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            account_id=request.session_account_id,
            at=request.now(),
        )
    if (
        released is not None
        and SESSION_REF in refs
        and released_at is not None
        and (row is None or row.created_at <= released_at)
    ):
        return released.model_copy(
            update={
                "provider": refs["provider"],
                "profile": refs["profile"],
                "config_revision": int(refs["config_revision"]),
                "native_refs": {k: refs[k] for k in (SESSION_REF, "account_scope") if k in refs},
            }
        )
    legacy = released is not None and released_at is None
    if (
        row is not None
        and not to_managed_agents
        and (released is None or not legacy or released.provider == MANAGED_AGENTS.provider)
    ):
        return ProviderBinding(
            id=released.id if released is not None else f"managed-agents:{row.ma_session_id}",
            thread=slot.thread,
            provider=MANAGED_AGENTS.provider,
            profile=MANAGED_AGENTS.profile_id,
            native_refs={SESSION_REF: row.ma_session_id},
            generation=released.generation if released is not None else 1,
            config_revision=0,
            legacy_account_id=slot.account_id,
        )
    if (
        released is not None
        and legacy
        and released.provider != MANAGED_AGENTS.provider
        and not (to_managed_agents and row is not None)
    ):
        # Released before its session's refs were kept.
        return released.model_copy(update={"native_refs": {}})
    return None


def _runtime(request: ProviderPreparationRequest, revision: ConfigRevision) -> TurnRuntime:
    with contextlib.suppress(KeyError):
        return request.deps.turn_runtimes[revision.profile]
    factory = request.deps.turn_runtime_factory
    if factory is None:
        raise KeyError(revision.profile)
    return factory(
        replace(request, admission=replace(request.admission, backend_revision=revision))
    )


async def replaced_session(
    request: ProviderPreparationRequest,
    store: StateStore,
    binding: ProviderBinding,
    own: OldSession | None,
) -> OldSession:
    """The session a host replacement ends: `own` when the host's driver reads it, else
    read through its provider's reader, so its conversation carries and it is archived.
    Unread, the successor starts with the lost framing and the person is told."""
    return own if own is not None else await _old_session(request, store, binding)


async def _old_session(
    request: ProviderPreparationRequest, store: StateStore, binding: ProviderBinding
) -> OldSession:
    unread = OldSession(binding=binding, ref=None, backend=None)
    session_id = binding.native_refs.get(SESSION_REF)
    if session_id is None:
        return unread
    try:
        if binding.provider == MANAGED_AGENTS.provider:
            backend = managed_agents(
                request.deps.anthropic,
                scope=request.scope,
                resources=frozenset({("session", session_id)}),
            )
            ref = resource_ref(backend, "session", session_id, scope=request.scope)
            return OldSession(binding=binding, ref=ref, backend=backend)
        reader = _READERS.get(binding.profile)
        revision = await store.get_config_revision(binding.thread.channel, binding.config_revision)
        if reader is None or revision is None:
            log.warning("carry_over.reader_missing", profile=binding.profile)
            return unread
        return reader(_runtime(request, revision), revision, request.scope, binding, store)
    except (DaimonError, MuxError, KeyError) as error:
        log.warning("carry_over.reader_unavailable", profile=binding.profile, error=str(error))
        return unread


async def carry_over(
    request: ProviderPreparationRequest,
    *,
    store: StateStore,
    slot: Slot,
    predecessor: ProviderBinding,
    model_id: str,
    agent_name: str,
    target_artifacts: Artifacts | None = None,
) -> CarriedOver:
    """Read `predecessor`'s conversation (and files, given `target_artifacts`) for the
    slot's first session on its new provider, framed as a replacement's handoff."""
    old = await _old_session(request, store, predecessor)
    key = f"carry-over:{predecessor.id}:g{predecessor.generation}"
    async with hold_slot(store, slot, request.now()) as lease:
        # Host journals serve a session's events only inside its turn context.
        reading_scope = old.scope or request.scope
        reading = TurnPersistence(
            store, old.binding, reading_scope, operation_key=key, session_ref=old.ref
        )
        with reading.holding(lease):
            turns = await read_turns(reading_scope, old)
            mounts, files = (
                await carry_files(reading_scope, old, target_artifacts, key)
                if target_artifacts is not None and turns is not None
                else ((), ())
            )
    framing, state = carry_framing(
        turns=turns, files=files, model_id=model_id, agent_name=agent_name
    )
    log.info(
        "thread.carried_over",
        binding_id=predecessor.id,
        from_provider=predecessor.provider,
        state=state,
        turns=len(turns or ()),
        files_carried=len(files),
    )
    return CarriedOver(
        continuity=ContinuityOutcome(
            state=state,
            applied=(),
            transfer_kind="transcript" if state == "replaced" else "history",
            user_prefix=framing.user_prefix,
            system_blocks=framing.system.blocks if framing.system is not None else (),
        ),
        mounts=mounts,
    )


async def carry_on_adoption(
    request: ProviderPreparationRequest,
    *,
    store: StateStore,
    slot: Slot,
    model_id: str,
    agent_name: str,
    target_artifacts: Artifacts | None = None,
) -> CarriedOver | None:
    """What a host's first session on `slot` starts with; None when nothing came before."""
    if not request.reuse_existing:
        return None
    predecessor = await thread_predecessor(request, store, slot)
    if predecessor is None:
        return None
    return await carry_over(
        request,
        store=store,
        slot=slot,
        predecessor=predecessor,
        model_id=model_id,
        agent_name=agent_name,
        target_artifacts=target_artifacts,
    )


async def carry_into_managed_agents(
    request: ProviderPreparationRequest,
    *,
    store: StateStore,
    slot: Slot,
    session_id: str,
    model_id: str,
    agent_name: str,
) -> CarriedOver | None:
    """`carry_on_adoption` for a just-created Managed Agents session, files mounted on it."""
    client = request.deps.anthropic
    target = managed_agents(
        client, scope=request.scope, resources=frozenset({("session", session_id)})
    )
    carried = await carry_on_adoption(
        request,
        store=store,
        slot=slot,
        model_id=model_id,
        agent_name=agent_name,
        target_artifacts=target.artifacts,
    )
    if carried is None or not carried.mounts:
        return carried
    files = frozenset(
        ("file", mount.resource.id) for mount in carried.mounts if mount.resource is not None
    )
    mounting = managed_agents(
        client,
        scope=request.scope,
        resources=frozenset({("session", session_id), *files}),
    )
    session = resource_ref(mounting, "session", session_id, scope=request.scope)
    mounted: list[ResourceBinding] = []
    for mount in carried.mounts:
        try:
            await mounting.session_admin.add_file(
                request.scope, session, mount, key=f"carry-over:{session_id}:{mount.id}"
            )
        except MuxError as error:
            log.warning("carry_over.file_not_mounted", mount_id=mount.id, error=str(error))
            continue
        mounted.append(mount)
    return replace(carried, mounts=tuple(mounted))

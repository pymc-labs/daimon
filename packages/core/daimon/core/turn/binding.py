"""Resolve a prepared mux turn's persisted slot before acquiring its lease."""

from __future__ import annotations

import uuid

from daimon.core.stores import mux_state
from daimon.core.stores.thread_sessions import get_live_thread_session, get_thread_session_by_id
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.outcomes import current_outcome
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.prepare import admitted_session_scope
from mux.contracts.ids import ChannelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import BindingConflict, ScopeViolation
from mux.profiles import get_profile
from mux.state.lease import Slot


async def prepared_persistence(
    deps: TurnDeps,
    admission: Admission,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    session_id: str,
    mapping_id: uuid.UUID | None,
    operation_key: str | None = None,
    session_ref: ResourceRef | None = None,
) -> TurnPersistence:
    """Adopt existing native ids; publish ownership through put_binding only.

    The mapping was authorized by preparation. Validate it again rather than
    accepting a session/thread from a driver parameter. Shared bindings are
    N2's account=None slot, with their pinned configuration and stable id.
    """
    scope = admitted_session_scope(admission, tenant_id=tenant_id, session_id=session_id)
    observation = current_outcome.get()
    key = operation_key or str(observation.id if observation is not None else uuid.uuid4())
    store = deps.state_store or mux_state.PostgresStateStore(deps.sessionmaker)
    revision = admission.backend_revision
    if revision is not None and revision.profile != "anthropic.managed_agents":
        # Provider preparation owns publication of its native binding. Never
        # adopt an Anthropic mapping or manufacture foreign provider ids here.
        provider = get_profile(revision.profile).provider
        if (
            mapping_id is not None
            or session_ref is None
            or (
                session_ref.kind != "session"
                or session_ref.id != session_id
                or session_ref.provider != provider
                or session_ref.tenant_id != scope.tenant_id
                or session_ref.account_id != scope.account_id
                or revision.channel.tenant_id != scope.tenant_id
                or revision.channel.platform != platform
            )
        ):
            raise ScopeViolation(session_id, "provider turn lacks its authorized native binding")
        slot = Slot(
            thread=ThreadRef(channel=revision.channel, thread_id=thread_id),
            account_id=None if admission.shared_owner is not None else scope.account_id,
        )
        binding = await store.get_binding(slot)
        if binding is None or (
            binding.provider != provider
            or binding.profile != revision.profile
            or binding.native_refs.get("session") != session_id
            or binding.config_revision != revision.local
            or (admission.shared_owner is not None and binding.id != str(admission.shared_owner))
        ):
            raise ScopeViolation(
                session_id, "provider preparation has no matching persisted binding"
            )
        return TurnPersistence(store, binding, scope, operation_key=f"turn:{session_id}:{key}")
    channel_id = admission.origin_channel_id or ""
    if mapping_id is not None:
        async with deps.sessionmaker() as db:
            mapped = await get_thread_session_by_id(db, id=mapping_id)
            live = await get_live_thread_session(
                db,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                account_id=admission.shared_owner or admission.account_id,
            )
        if mapped is None or (
            mapped.tenant_id != tenant_id
            or mapped.platform != platform
            or mapped.thread_id != thread_id
            or mapped.account_id != (admission.shared_owner or admission.account_id)
            or mapped.ma_session_id != session_id
            or mapped.status != "live"
            or live is None
            or live.id != mapped.id
        ):
            raise ScopeViolation(session_id, "prepared session mapping differs from this turn")
        # Match the migration's slot key, including its channel-less rows.
        channel_id = mapped.channel_id or ""
    if admission.shared_owner is not None:
        if admission.backend_revision is None:
            raise ScopeViolation(session_id, "shared turn has no admitted channel revision")
        channel = admission.backend_revision.channel
        if channel.tenant_id != scope.tenant_id or channel.platform != platform:
            raise ScopeViolation(session_id, "shared binding differs from the admitted channel")
    else:
        channel = ChannelRef(tenant_id=scope.tenant_id, platform=platform, channel_id=channel_id)
    slot = Slot(
        thread=ThreadRef(channel=channel, thread_id=thread_id),
        account_id=None if admission.shared_owner is not None else scope.account_id,
    )
    current = await store.get_binding(slot)
    if current is not None and (
        current.provider != "anthropic" or current.profile != "anthropic.managed_agents"
    ):
        raise ScopeViolation(session_id, "persisted binding belongs to another provider profile")
    if current is None and admission.shared_owner is not None:
        # bind_session has already committed N2's shared binding. An append
        # or driver call must never create ownership for an unbound shared session.
        raise ScopeViolation(session_id, "shared session has no persisted binding")
    if (
        current is not None
        and admission.shared_owner is not None
        and current.id != str(admission.shared_owner)
    ):
        raise ScopeViolation(session_id, "shared binding belongs to another owner")
    if current is None:
        binding = ProviderBinding(
            id=(
                str(mapping_id)
                if mapping_id is not None
                else str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"daimon.turn.binding:{slot.model_dump_json()}")
                )
            ),
            thread=slot.thread,
            provider="anthropic",
            profile="anthropic.managed_agents",
            native_refs={"session": session_id, "agent": admission.agent.id},
            generation=1,
            config_revision=(admission.backend_revision.local if admission.backend_revision else 0),
            legacy_account_id=scope.account_id,
        )
    elif current.native_refs.get("session") != session_id:
        # A prepared replacement is a new generation of the same slot. The
        # old session retains its existing journal owner in StateStore.
        binding = current.model_copy(
            update={
                "generation": current.generation + 1,
                "native_refs": {**current.native_refs, "session": session_id},
            }
        )
    else:
        binding = current
    if binding != current:
        try:
            binding = await store.put_binding(
                binding, expected_generation=current.generation if current else 0
            )
        except BindingConflict:
            winner = await store.get_binding(slot)
            if winner is None or winner.native_refs.get("session") != session_id:
                raise
            binding = winner
    return TurnPersistence(store, binding, scope, operation_key=f"turn:{session_id}:{key}")

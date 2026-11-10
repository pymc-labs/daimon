"""Replacing a provider thread's native session when its binding no longer fits.

One rule for every provider host. `binding_decision` maps the binding's
recorded fields onto the session classifier (`decide_session_compatibility`),
so a change it would continue or update in place never replaces. Only
`replace_binding` performs a replacement: under the slot lease it reads the old
session's conversation (and, where the target mounts files, its artifacts),
creates the successor through the host, writes the next generation of the same
binding and terminates the old session through the Sessions port.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

import structlog
from daimon.core.handoff_context import (
    HandoffFraming,
    TranscriptTurn,
    render_handoff_framing,
    render_lost_workspace_framing,
    render_previous_session,
    select_recent_turn_texts,
)
from daimon.core.session_compat import (
    ChangeReason,
    CompatibilityDecision,
    MaCapabilities,
    ReplaceSession,
    decide_session_compatibility,
)
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.turn.errors import SessionBusyError
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.prepare import ContinuityOutcome
from mux.contracts.events import AgentMessagePayload, UserMessagePayload
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.ports import Artifacts, ManagedAgents
from mux.contracts.resources import ProviderBinding, ResourceBinding
from mux.errors import MuxError, UnsupportedCapability
from mux.state.lease import Lease, LeaseBusy, Slot, StaleFence
from mux.state.store import ERA_REF, StateStore

log = structlog.get_logger(__name__)

REPLACEMENT_LEASE_TTL = timedelta(minutes=5)
BUSY_RETRY_S = 5
MAX_EVENT_PAGES = 5
MAX_CARRIED_FILES = 20
MAX_CARRIED_FILE_BYTES = 10 * 1024 * 1024
MAX_CARRIED_TOTAL_BYTES = 50 * 1024 * 1024
CARRIED_FILES_DIR = "/workspace/previous"

BindingField = Literal[
    "profile", "provider", "config_revision", "agent", "account_scope", "model", "plan_digest"
]
BINDING_FIELDS: tuple[BindingField, ...] = (
    "profile",
    "provider",
    "config_revision",
    "agent",
    "account_scope",
    "model",
    "plan_digest",
)


@dataclass(frozen=True, slots=True)
class BindingFacts:
    """The fields of a binding that decide whether its session still fits."""

    profile: str
    provider: str
    config_revision: int
    agent: str
    account_scope: str
    model: str
    plan_digest: str
    environment: str = ""
    """Set by a host whose plan names its environment (self-hosted)."""


def recorded_facts(binding: ProviderBinding, *, environment: bool = False) -> BindingFacts:
    """What `binding` was created with; `environment` reads its environment ref too."""
    refs = binding.native_refs
    return BindingFacts(
        environment=refs.get("environment", "") if environment else "",
        profile=binding.profile,
        provider=binding.provider,
        config_revision=binding.config_revision,
        agent=refs.get("agent", ""),
        account_scope=refs.get("account_scope", ""),
        model=refs.get("model", ""),
        plan_digest=refs.get("plan_digest", ""),
    )


def _snapshot(facts: BindingFacts, updatable: frozenset[BindingField]) -> SessionSnapshot:
    def part(*names: BindingField) -> str:
        return "\x1f".join(str(getattr(facts, name)) for name in names if name not in updatable)

    in_place = "\x1f".join(str(getattr(facts, name)) for name in sorted(updatable))
    return SessionSnapshot(
        ma_agent_id=part("agent"),
        model_id=part("model"),
        system_sha256=part("config_revision", "plan_digest"),
        skills_sha256="",
        environment_id=part("profile", "provider", "account_scope") + "\x1f" + facts.environment,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        tools_sha256=in_place,
        mcp_servers_sha256="",
        env_sha256=None,
        agent_version=0,
        agent_name="",
    )


def binding_decision(
    recorded: BindingFacts,
    desired: BindingFacts,
    *,
    reuse_existing: bool = True,
    updatable: frozenset[BindingField] = frozenset(),
) -> CompatibilityDecision:
    """Reuse, update in place (`updatable` fields only) or replace the bound session.

    A caller that may not reuse the session at all gets a replacement with no reason.
    """
    if not reuse_existing:
        return ReplaceSession(reasons=())
    return decide_session_compatibility(
        recorded=_snapshot(recorded, updatable),
        desired=_snapshot(desired, updatable),
        capabilities=MaCapabilities(session_agent_updatable=True),
        now=datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class OldSession:
    """The session being replaced, and a driver that may read and terminate it."""

    binding: ProviderBinding
    ref: ResourceRef | None
    backend: ManagedAgents | None


@dataclass(frozen=True, slots=True)
class Replacement:
    binding: ProviderBinding
    continuity: ContinuityOutcome


# The host creates the successor session (`resources` mounted) under `lease`, with
# `era` suffixing its idempotency keys, and returns the binding to publish.
CreateSuccessor = Callable[[str, tuple[ResourceBinding, ...], Lease], Awaitable[ProviderBinding]]


def _busy(now: datetime, reason: str) -> SessionBusyError:
    return SessionBusyError(
        pending_reasons=(reason,), retry_after=now + timedelta(seconds=BUSY_RETRY_S)
    )


@contextlib.asynccontextmanager
async def hold_slot(
    store: StateStore, slot: Slot, now: datetime, *, ttl: timedelta = REPLACEMENT_LEASE_TTL
) -> AsyncIterator[Lease]:
    """Hold the thread's lease while its binding changes; a running turn means busy."""
    try:
        lease = await store.acquire_lease(
            slot,
            holder="binding-change:" + str(uuid4()),
            turn_id="binding-change:" + str(uuid4()),
            now=now,
            ttl=ttl,
        )
    except LeaseBusy as error:
        raise _busy(now, "session_active") from error
    try:
        yield lease
    finally:
        # A lease lost to expiry already belongs to a newer holder: leave it alone.
        with contextlib.suppress(StaleFence):
            await store.release_lease(lease)


async def read_turns(scope: Scope, old: OldSession) -> list[TranscriptTurn] | None:
    """The old conversation's tail, or None when it cannot be read."""
    if old.backend is None or old.ref is None:
        return None
    texts: list[tuple[Literal["user", "agent"], str]] = []
    page = PageRequest(limit=100, order="desc")
    try:
        for _ in range(MAX_EVENT_PAGES):
            result = await old.backend.events.list(scope, old.ref, page=page)
            for event in result.data:
                payload = event.typed_payload()
                if isinstance(payload, UserMessagePayload):
                    role: Literal["user", "agent"] = "user"
                elif isinstance(payload, AgentMessagePayload) and payload.complete:
                    role = "agent"
                else:
                    continue
                parts = (getattr(part, "text", None) for part in payload.content)
                texts.append((role, "\n".join(text for text in parts if isinstance(text, str))))
            if not result.has_more or result.next_cursor is None:
                break
            page = PageRequest(cursor=result.next_cursor, limit=100, order="desc")
    except MuxError as error:
        log.warning("binding.transcript_unreadable", binding_id=old.binding.id, error=str(error))
        return None
    texts.reverse()
    return select_recent_turn_texts(texts)


def _safe_name(filename: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", filename.rsplit("/", 1)[-1]).lstrip(".") or "file"
    name, suffix = base, 1
    while name in taken:
        suffix += 1
        name = f"{suffix}-{base}"
    taken.add(name)
    return name


async def carry_files(
    scope: Scope, old: OldSession, target: Artifacts, key: str
) -> tuple[tuple[ResourceBinding, ...], tuple[str, ...]]:
    """Copy the old session's artifacts within the caps; mounts and their paths."""
    if old.backend is None or old.ref is None:
        return (), ()
    try:
        listed = await old.backend.artifacts.list(
            scope, old.ref, page=PageRequest(limit=MAX_CARRIED_FILES)
        )
    except MuxError:
        return (), ()
    mounts: list[ResourceBinding] = []
    taken: set[str] = set()
    total = 0
    for artifact in listed.data[:MAX_CARRIED_FILES]:
        size = artifact.size_bytes
        if size is None or size > MAX_CARRIED_FILE_BYTES or total + size > MAX_CARRIED_TOTAL_BYTES:
            continue
        try:
            body = old.backend.artifacts.download(scope, artifact.ref)
            copied = await target.upload(
                scope,
                body,
                filename=artifact.filename,
                media_type=artifact.media_type,
                key=f"{key}:file:{artifact.ref.id}",
            )
        except MuxError as error:
            log.warning("binding.file_not_carried", artifact_id=artifact.ref.id, error=str(error))
            continue
        total += size
        mounts.append(
            ResourceBinding(
                id=str(uuid5(NAMESPACE_URL, copied.ref.id)),
                kind="artifact",
                target_path=f"{CARRIED_FILES_DIR}/{_safe_name(artifact.filename, taken)}",
                resource=copied.ref,
            )
        )
    return tuple(mounts), tuple(mount.target_path or "" for mount in mounts)


async def _terminate(scope: Scope, old: OldSession, key: str) -> bool:
    """End the replaced session through the Sessions port: archive, else delete."""
    if old.backend is None or old.ref is None:
        return False
    for end in (old.backend.sessions.archive, old.backend.sessions.delete):
        try:
            await end(scope, old.ref, key=key)
        except UnsupportedCapability:
            continue
        except MuxError as error:
            # The successor is already published; a session left open costs only storage.
            log.warning("binding.old_session_kept", binding_id=old.binding.id, error=str(error))
            return False
        return True
    return False


def carry_framing(
    *,
    turns: list[TranscriptTurn] | None,
    files: Sequence[str],
    model_id: str,
    agent_name: str,
) -> tuple[HandoffFraming, Literal["replaced", "replaced_after_loss"]]:
    if turns is None:
        return render_lost_workspace_framing(model_id=model_id, previous_session=None), (
            "replaced_after_loss"
        )
    previous = render_previous_session(turns, from_agent_name=agent_name) if turns else None
    return render_handoff_framing(
        model_id=model_id,
        transfer_kind="transcript",
        bundle_mount_path=None,
        from_agent_name=agent_name,
        to_agent_name=agent_name,
        requested_work=None,
        previous_session=previous,
        not_carried=(),
        carried_files=files,
    ), "replaced"


async def replace_binding(
    *,
    store: StateStore,
    slot: Slot,
    scope: Scope,
    old: OldSession,
    reasons: tuple[ChangeReason, ...],
    create: CreateSuccessor,
    now: datetime,
    model_id: str,
    agent_name: str,
    target_artifacts: Artifacts | None = None,
) -> Replacement:
    """Replace `old` with a new session on the same binding, carrying its conversation."""
    expected = old.binding
    async with hold_slot(store, slot, now) as lease:
        current = await store.get_binding(slot)
        if current is None or (current.id, current.generation) != (
            expected.id,
            expected.generation,
        ):
            # Another turn changed the binding first; the next message sees its result.
            raise _busy(now, "binding_changed")
        era = f":g{expected.generation}"
        key = f"binding-replace:{expected.id}{era}"
        # Host journals serve a bound session's events only inside its turn context.
        reading = TurnPersistence(store, expected, scope, operation_key=key, session_ref=old.ref)
        with reading.holding(lease):
            turns = await read_turns(scope, old)
            mounts, files = (
                await carry_files(scope, old, target_artifacts, key)
                if target_artifacts is not None and turns is not None
                else ((), ())
            )
        created = await create(era, mounts, lease)
        successor = created.model_copy(
            update={
                "id": expected.id,
                "generation": expected.generation + 1,
                "native_refs": {**created.native_refs, ERA_REF: era},
            }
        )
        await store.renew_lease(lease, now=now, ttl=REPLACEMENT_LEASE_TTL)
        binding = await store.put_binding(successor, expected_generation=expected.generation)
        terminated = await _terminate(scope, old, key + ":terminate")
    framing, state = carry_framing(
        turns=turns,
        files=files,
        model_id=model_id,
        agent_name=agent_name,
    )
    log.info(
        "binding.replaced",
        binding_id=binding.id,
        generation=binding.generation,
        provider=binding.provider,
        profile=binding.profile,
        reasons=list(reasons),
        state=state,
        files_carried=len(files),
        old_session_terminated=terminated,
    )
    return Replacement(
        binding=binding,
        continuity=ContinuityOutcome(
            state=state,
            applied=reasons,
            transfer_kind="transcript" if state == "replaced" else "history",
            user_prefix=framing.user_prefix,
            system_blocks=framing.system.blocks if framing.system is not None else (),
        ),
    )

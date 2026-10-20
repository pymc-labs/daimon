"""Invocation checkpoints in the host's fenced journal, independent of any SDK."""

from datetime import UTC, datetime

from daimon.core.turn.persistence import TurnPersistence, current_persistence
from mux.contracts.events import AgentMessagePayload, Event, NativeProvenance, TextPart
from mux.contracts.ids import ResourceRef
from mux.errors import ScopeViolation

_BASELINE = "native.selfhosted.host_invocation_baseline"


def persistence(session: ResourceRef) -> TurnPersistence:
    active = current_persistence.get()
    if active is None:
        raise ScopeViolation(session.id, "self-hosted state requires an active admitted lease")
    active.check_session(active.scope, session)
    return active


def journal_source(event: Event) -> str:
    return event.native.event_id or event.id


async def invocation_baseline(
    session: ResourceRef, observed_roots: tuple[str, ...]
) -> tuple[tuple[str, ...], bool]:
    active = persistence(session)
    identity = "selfhosted:baseline:" + active.operation_key
    after = -1
    for _ in range(100):
        page = await active.store.read_events(session.id, after=after, limit=1000)
        for row in page:
            if row.type == _BASELINE and row.id == identity:
                roots = row.payload.get("roots")
                if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
                    raise ValueError("invalid self-hosted invocation baseline")
                operation = await active.store.get_operation(
                    active.scope, active.operation_key + ":send:0"
                )
                return tuple(
                    str(root) for root in roots
                ), operation is not None and operation.operation.status != "pending"
        if not page:
            break
        if page[-1].sequence <= after:
            raise ValueError("nonadvancing self-hosted journal")
        after = page[-1].sequence
    else:
        raise ValueError("self-hosted journal exceeds bounded read")
    await active.record(
        session,
        Event(
            id=identity,
            session_id=session.id,
            sequence=0,
            type=_BASELINE,
            observed_at=datetime.now(UTC),
            authority="record",
            payload={"roots": list(observed_roots)},
            native=NativeProvenance(provider="selfhosted", api_revision="daimon-host-journal@1"),
        ),
    )
    return observed_roots, False


async def skipped_skill_notice(session: ResourceRef, text: str) -> Event | None:
    """Journal one host-authored notice for this bound thread before display.

    The turn lease serializes the marker with sends. Reconnecting or starting
    another turn on the binding cannot repeat it or send it to the model.
    """
    if not text:
        return None
    active = persistence(session)
    identity = "selfhosted:skipped-skills:" + active.binding.id
    after = -1
    for _ in range(100):
        rows = await active.store.read_events(session.id, after=after, limit=1000)
        if any(row.id == identity for row in rows):
            return None
        if not rows:
            break
        if rows[-1].sequence <= after:
            raise ValueError("nonadvancing self-hosted notice journal")
        after = rows[-1].sequence
    else:
        raise ValueError("self-hosted notice journal exceeds bounded read")
    event = Event(
        type="agent.message",
        payload=AgentMessagePayload(
            item_id=identity, content=(TextPart(text=text),), complete=True
        ).model_dump(mode="json"),
        id=identity,
        session_id=session.id,
        sequence=0,
        observed_at=datetime.now(UTC),
        authority="record",
        native=NativeProvenance(provider="selfhosted", api_revision="daimon-host-notice@1"),
    )
    await active.record(session, event, source_key=identity)
    return event

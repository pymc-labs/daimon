"""OpenAI snapshot/revision storage within the host's fenced turn journal.

A completed recovery snapshot is appended atomically through A4. The host
invocation/revision checkpoints never become display records. Usage allocation is
serialized by that same admitted binding lease, including after process restart.
"""

from collections.abc import Mapping
from datetime import UTC, datetime

from daimon.core.turn.persistence import TurnPersistence, current_persistence
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef
from mux.drivers.openai.transport import object_json
from mux.drivers.openai.turn import merge_history
from mux.errors import ScopeViolation
from pydantic import JsonValue

_BASELINE = "native.openai.host_invocation_baseline"
_REVISION = "native.openai.host_usage_revision"
_LIMIT = 100_000


def persistence(session: ResourceRef) -> TurnPersistence:
    active = current_persistence.get()
    if active is None:
        raise ScopeViolation(session.id, "OpenAI state requires an active admitted lease")
    active.check_session(active.scope, session)
    return active


async def journal(session: ResourceRef) -> tuple[Event, ...]:
    active = persistence(session)
    result: list[Event] = []
    after = -1
    while len(result) < _LIMIT:
        page = await active.store.read_events(session.id, after=after, limit=1000)
        if not page:
            return tuple(result)
        result.extend(page)
        next_sequence = page[-1].sequence
        if next_sequence <= after:
            raise ValueError("nonadvancing OpenAI host journal")
        after = next_sequence
    raise ValueError("OpenAI host journal exceeds bounded read")


def checkpoint(
    session: ResourceRef, identity: str, type_: str, payload: dict[str, JsonValue]
) -> Event:
    return Event(
        id=identity,
        session_id=session.id,
        sequence=0,
        type=type_,
        observed_at=datetime.now(UTC),
        authority="record",
        payload=payload,
        native=NativeProvenance(provider="openai", api_revision="daimon-host-journal@1"),
    )


def journal_source(event: Event) -> str:
    """One provider item can yield two records with unchanged native provenance."""
    if event.type in ("agent.tool_use", "agent.tool_result"):
        return event.id
    return event.native.event_id or event.id


class OpenAIRecoveryJournal:
    async def read(self, session: ResourceRef) -> tuple[Event, ...] | None:
        rows = await journal(session)
        values = tuple(row for row in rows if row.type not in (_REVISION, _BASELINE))
        return merge_history((), values) or None

    async def replace(self, session: ResourceRef, events: tuple[Event, ...]) -> None:
        active = persistence(session)
        if any(e.session_id != session.id or e.native.provider != "openai" for e in events):
            raise ScopeViolation(session.id, "foreign OpenAI recovery snapshot")
        # A4 publishes all completed pages, projection and cursor in ONE commit.
        cursor = events[-1].id if events else ""
        await active.record_many(session, events, cursor=cursor, source_key=journal_source)


async def invocation_baseline(
    session: ResourceRef, observed_roots: tuple[str, ...]
) -> tuple[tuple[str, ...], bool]:
    active = persistence(session)
    identity = "openai:baseline:" + active.operation_key
    for row in await journal(session):
        if row.type == _BASELINE and row.id == identity:
            roots = row.payload.get("roots")
            if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
                raise ValueError("invalid OpenAI invocation baseline")
            operation = await active.store.get_operation(
                active.scope, active.operation_key + ":send:0"
            )
            sent = operation is not None and operation.operation.status != "pending"
            return tuple(str(root) for root in roots), sent
    await active.record(
        session, checkpoint(session, identity, _BASELINE, {"roots": list(observed_roots)})
    )
    return observed_roots, False


class OpenAIUsageRevisions:
    async def revise(
        self, session: ResourceRef, observation_id: str, meter: Mapping[str, JsonValue]
    ) -> int:
        active = persistence(session)
        current: Event | None = None
        for row in await journal(session):
            if row.type == _REVISION and row.payload.get("observation_id") == observation_id:
                current = row
        value = object_json(dict(meter))
        revision = 1
        if current is not None:
            prior = current.payload["revision"]
            if isinstance(prior, bool) or not isinstance(prior, int) or prior < 1:
                raise ValueError("invalid persisted OpenAI usage revision")
            if current.payload.get("meter") == value:
                return prior
            revision = prior + 1
        await active.record(
            session,
            checkpoint(
                session,
                "openai:revision:" + observation_id + ":" + str(revision),
                _REVISION,
                {"observation_id": observation_id, "revision": revision, "meter": value},
            ),
        )
        return revision

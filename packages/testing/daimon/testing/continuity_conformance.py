"""Opt-in host evidence collectors; provider envelopes and verdicts stay separate.

The provider owner supplies actual drivers and upstream fault barriers. These
collectors never fabricate saved events, edit projections or clear PENDING.
Controlled clocks/loss and synthetic accounting are not native provider proof.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from daimon.testing.conformance.runner import require
from mux.contracts.actions import UserMessage
from mux.contracts.events import (
    AgentMessagePayload,
    Event,
    TextPart,
    ToolResultPayload,
    TurnEndedPayload,
    UserMessagePayload,
)
from mux.contracts.ids import PageRequest, ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.resources import Session
from mux.errors import ContinuityLost


@dataclass
class ProbeClock:
    """One monotonic controlled clock per independent loss branch."""

    value: datetime

    def __post_init__(self) -> None:
        if self.value.utcoffset() is None:
            raise ValueError("probe clocks require an aware timestamp")

    def __call__(self) -> datetime:
        return self.value

    def advance_to(self, value: datetime) -> None:
        if value.utcoffset() is None or value < self.value:
            raise ValueError("probe clocks cannot move backwards")
        self.value = value


@dataclass(frozen=True)
class LossProbe:
    """Callbacks bind an owned real session and its upstream/storage boundary.

    The driver must read this clock itself. A fresh probe is required for each
    branch so unexpected loss occurs before expiry without rewinding a clock.
    """

    clock: ProbeClock
    expires_at: datetime
    scope: Scope
    session: Session
    driver: ManagedAgents
    mutation_count: Callable[[], int]
    lose_workspace: Callable[[], Awaitable[None]]

    def __post_init__(self) -> None:
        _owned_session(self.driver, self.scope, self.session.ref)
        if (
            self.session.binding.native_refs.get("session") != self.session.ref.id
            or self.session.binding.provider != self.session.ref.provider
            or self.session.binding.thread.channel.tenant_id != self.scope.tenant_id
            or self.session.binding.legacy_account_id not in (None, self.scope.account_id)
        ):
            raise ValueError("C02: binding does not own the session")


@dataclass(frozen=True)
class LossEvidence:
    kind: Literal["expiry", "unexpected_loss"]
    binding_id: str
    expires_at: datetime
    observed_at: datetime
    evidence: tuple[str, ...]
    writes_before: int
    writes_after: int
    source: Literal["controlled_host_fault"] = "controlled_host_fault"


async def collect_loss_evidence(
    probe: LossProbe, kind: Literal["expiry", "unexpected_loss"]
) -> LossEvidence:
    """Exercise the real session read, never manufacture ContinuityLost."""
    require(kind in ("expiry", "unexpected_loss"), "C02: unknown controlled loss")
    require(probe.expires_at.utcoffset() is not None, "C02: expiry must be timezone aware")
    require(probe.clock() < probe.expires_at, "C02: loss branch must begin before expiry")
    before = await probe.driver.sessions.retrieve(probe.scope, probe.session.ref)
    require(before.binding == probe.session.binding, "C02: initial binding changed")
    writes = probe.mutation_count()
    if kind == "expiry":
        probe.clock.advance_to(probe.expires_at)
    else:
        await probe.lose_workspace()
        require(probe.clock() < probe.expires_at, "C02: unexpected loss must precede expiry")
    try:
        await probe.driver.sessions.retrieve(probe.scope, probe.session.ref)
    except ContinuityLost as exc:
        require(exc.binding_id == before.binding.id and exc.evidence, "C02: unbound loss evidence")
        expired = any("expir" in item.lower() for item in exc.evidence)
        require(expired == (kind == "expiry"), "C02: expiry and unexpected loss conflated")
        require(probe.mutation_count() == writes, "C02: loss created provider resources")
        return LossEvidence(
            kind,
            exc.binding_id,
            probe.expires_at,
            probe.clock(),
            exc.evidence,
            writes,
            probe.mutation_count(),
        )
    raise ValueError("C02: loss silently continued the session")


async def collect_snapshot_bytes(
    driver: ManagedAgents, scope: Scope, session: Session, artifact: ResourceRef, *, key_prefix: str
) -> tuple[bytes, ...]:
    """Exact seeded binary bytes through two port turns, including saved inputs."""
    samples: list[bytes] = []
    _owned_session(driver, scope, session.ref)
    expected = bytes(range(256))
    previous_ids = {event.id for event in await _history(driver, scope, session.ref)}
    for index in range(3):
        samples.append(
            b"".join([part async for part in driver.artifacts.download(scope, artifact)])
        )
        require(samples[-1] == expected, "C02: exact seeded binary bytes changed")
        if index == 2:
            break
        await driver.events.send(
            scope,
            session.ref,
            (UserMessage(content=(TextPart(text=f"persistence-turn-{index}"),)),),
            key=f"{key_prefix}-{index}",
        )
        journal = [event async for event in driver.events.stream(scope, session.ref)]
        require(
            any(
                event.type == "session.turn_ended"
                and event.thread_id is None
                and event.authority in ("record", "reconciled")
                for event in journal
            ),
            "C02: turn lacks an authoritative root end",
        )
    history = await _history(driver, scope, session.ref)
    new_inputs = [
        event.typed_payload()
        for event in history
        if event.type == "user.message" and event.id not in previous_ids
    ]
    require(
        len(new_inputs) == 2
        and all(isinstance(payload, UserMessagePayload) for payload in new_inputs),
        "C02: both saved turn inputs are required",
    )
    require(
        {payload.content for payload in new_inputs if isinstance(payload, UserMessagePayload)}
        == {(TextPart(text="persistence-turn-0"),), (TextPart(text="persistence-turn-1"),)},
        "C02: saved turn inputs changed",
    )
    return tuple(samples)


@dataclass(frozen=True)
class HistoryExpectations:
    """Declared before observing output; native IDs must not be inferred later."""

    root_turn_id: str
    saved_message_item_id: str
    tool_call_id: str

    def __post_init__(self) -> None:
        if not all((self.root_turn_id, self.saved_message_item_id, self.tool_call_id)):
            raise ValueError("C05 requires independently declared native identities")


@dataclass(frozen=True)
class HistoryEvidence:
    expectations: HistoryExpectations
    buffered: tuple[Event, ...]
    saved: tuple[Event, ...]
    restarted: tuple[Event, ...]
    gaps: tuple[str, ...]


async def _history(driver: ManagedAgents, scope: Scope, session: ResourceRef) -> tuple[Event, ...]:
    events: list[Event] = []
    cursor: str | None = None
    cursors: set[str] = set()
    for _ in range(100):
        page = await driver.events.list(scope, session, page=PageRequest(cursor=cursor, limit=2))
        events.extend(page.data)
        require(
            all(
                event.session_id == session.id and event.native.provider == session.provider
                for event in page.data
            ),
            "C05: foreign journal evidence",
        )
        cursor = page.next_cursor
        if cursor is None:
            require(
                len({event.id for event in events}) == len(events), "C05: duplicate journal IDs"
            )
            return tuple(events)
        require(cursor not in cursors, "C05: pagination cursor loop")
        cursors.add(cursor)
    raise ValueError("C05: pagination exceeded bound")


async def collect_history_evidence(
    driver: ManagedAgents,
    scope: Scope,
    session: ResourceRef,
    expectations: HistoryExpectations,
    *,
    restart: Callable[[], Awaitable[ManagedAgents]],
) -> HistoryEvidence:
    """Read child-first/EOF history and prove it survives a fresh driver."""
    _owned_session(driver, scope, session)
    buffered = tuple([event async for event in driver.events.stream(scope, session, previews=True)])
    running = await driver.sessions.retrieve(scope, session)
    require(
        running.state == "running" and running.active_root_turn == expectations.root_turn_id,
        "C05: child completion or EOF released root occupancy",
    )
    projection = await driver.events.reconcile(scope, session)
    saved = await _history(driver, scope, session)
    messages = [event.typed_payload() for event in saved if event.type == "agent.message"]
    require(len(messages) == 1, "C05: expected exactly one saved message")
    message = messages[0]
    require(isinstance(message, AgentMessagePayload), "C05: message must be typed")
    if not isinstance(message, AgentMessagePayload):
        raise TypeError("C05: message must be typed")
    require(
        message.item_id == expectations.saved_message_item_id
        and message.complete
        and message.content == (TextPart(text="done"),),
        "C05: saved message changed",
    )
    results = [event for event in saved if event.type == "agent.tool_result"]
    require(
        len(results) == 1 and results[0].thread_id is None, "C05: duplicate or missing tool result"
    )
    result = results[0].typed_payload()
    require(
        isinstance(result, ToolResultPayload)
        and result.call_id == expectations.tool_call_id
        and result.content == (TextPart(text="result"),)
        and not result.is_error,
        "C05: saved tool result changed",
    )
    ends = [
        event for event in saved if event.type == "session.turn_ended" and event.thread_id is None
    ]
    require(
        len(ends) == 1 and ends[0].authority in ("record", "reconciled"),
        "C05: expected one authoritative root end",
    )
    end = ends[0].typed_payload()
    require(
        isinstance(end, TurnEndedPayload)
        and end.root_turn_id == expectations.root_turn_id
        and end.outcome == "completed",
        "C05: root identity or outcome changed",
    )
    require(
        projection.state == "idle" and projection.active_root_turn is None,
        "C05: authoritative end did not release occupancy",
    )
    require(
        projection.gaps and any(event.type == "session.history_gap" for event in saved),
        "C05: history loss was hidden",
    )
    for event in buffered:
        if event.type == "agent.message":
            payload = event.typed_payload()
            require(
                isinstance(payload, AgentMessagePayload)
                and payload.item_id == expectations.saved_message_item_id,
                "C05: buffered item disappeared",
            )
    restarted_driver = await restart()
    require(restarted_driver is not driver, "C05: restart must construct a fresh driver")
    restarted = await _history(restarted_driver, scope, session)
    resumed = await restarted_driver.events.reconcile(scope, session)
    require(
        restarted == saved
        and resumed.state == projection.state
        and resumed.active_root_turn == projection.active_root_turn
        and resumed.required_actions == projection.required_actions
        and set(projection.gaps).issubset(resumed.gaps),
        "C05: journal or projection lost on restart",
    )
    return HistoryEvidence(expectations, buffered, saved, restarted, projection.gaps)


def _owned_session(driver: ManagedAgents, scope: Scope, session: ResourceRef) -> None:
    if (
        session.kind != "session"
        or session.provider not in ("openai", "gemini")
        or session.provider != driver.capabilities().provider
        or session.tenant_id != scope.tenant_id
        or session.account_id != scope.account_id
        or scope.is_platform
        or scope.is_legacy_host_authorized
    ):
        raise ValueError("host conformance requires an explicitly authorized neutral session")

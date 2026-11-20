"""The audience gate: one rule, three callers (output latch, POST decision, protection)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from daimon.core import audience_gate, routine_hold
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_gate import POST_EXEMPTIONS, PostAudience, audience_fact
from daimon.core.audience_refusal import audience_refusal
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_audience import ChannelAudience
from daimon.core.turn import output as output_mod
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.output import OutputAuthorization, OutputGate, audience_output_check
from daimon.core.turn.protection import ProtectionState, protection_state
from daimon.core.turn.run import _install_audience_gate  # pyright: ignore[reportPrivateUsage]
from daimon.core.turn_hold import TurnHold
from structlog.testing import capture_logs

TENANT = uuid.UUID(int=7)
OPEN = ChannelAudience(contained=False, known=True, narrow=False, source="live")
CONTAINED = ChannelAudience(contained=True, known=True, narrow=False, source="live")
UNKNOWN = ChannelAudience(contained=True, known=False, narrow=True, source="none")
HOLD = TurnHold("discord", "room-1", client=False)


class _Session:
    """A session stand-in: the mode and the fact are patched, so no row is read."""

    @asynccontextmanager
    async def begin_nested(self) -> Any:
        yield self


class _Sessions:
    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> Any:
        self.opened += 1

        @asynccontextmanager
        async def session() -> Any:
            yield _Session()

        return session()


class _World:
    """The patched mode and facts, and a count of fact reads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, mode: str = "on") -> None:
        self.mode: Any = mode
        self.facts: dict[str, ChannelAudience] = {}
        self.fact_reads = 0
        self.mode_reads = 0
        monkeypatch.setattr(audience_gate, "audience_mode", self._mode)
        monkeypatch.setattr(audience_gate, "channel_has_outsiders", self._fact)
        monkeypatch.setattr(output_mod, "channel_has_outsiders", self._fact)

    async def _mode(self, session: object, place: object) -> str:
        self.mode_reads += 1
        if isinstance(self.mode, Exception):
            raise self.mode
        return self.mode

    async def _fact(
        self, session: object, *, tenant_id: uuid.UUID, platform: str, channel_id: str
    ) -> ChannelAudience:
        self.fact_reads += 1
        return self.facts.get(channel_id, UNKNOWN)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(monkeypatch)


# ── the pure rule ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fact", "hold", "channel", "parent", "refused"),
    [
        (OPEN, None, "room-1", None, False),
        (CONTAINED, None, "room-1", None, True),
        (UNKNOWN, None, "room-1", None, True),
        (CONTAINED, HOLD, "room-1", None, False),  # held, own room
        (CONTAINED, HOLD, "thread-9", "room-1", False),  # held, its thread
        (CONTAINED, HOLD, "room-2", None, True),  # held elsewhere
        (CONTAINED, HOLD, "thread-9", "room-2", True),
        (CONTAINED, HOLD, None, None, True),
        (OPEN, HOLD, "room-2", None, False),  # this rule only adds the audience term
    ],
)
def test_audience_refusal(
    fact: ChannelAudience,
    hold: TurnHold | None,
    channel: str | None,
    parent: str | None,
    refused: bool,
) -> None:
    assert audience_refusal(fact, hold, channel, parent) == (
        "audience_contained" if refused else None
    )


@pytest.mark.parametrize(
    ("fact", "hold", "channel", "parent", "allowed"),
    [
        (None, None, "room-1", None, True),  # not checked: today's decision
        (OPEN, None, "room-1", None, True),
        (CONTAINED, None, "room-1", None, False),
        (CONTAINED, HOLD, "room-1", None, True),
        (CONTAINED, HOLD, "thread-9", "room-1", True),
    ],
)
def test_the_post_decision_carries_the_audience_term(
    fact: ChannelAudience | None,
    hold: TurnHold | None,
    channel: str,
    parent: str | None,
    allowed: bool,
) -> None:
    decision = authorize(
        TenantAccessPolicy(),
        subject=Subject(),
        action=Action.POST,
        place=Place(channel_id=channel, parent_channel_id=parent),
        hold=hold,
        audience=fact,
    )
    assert decision.allowed is allowed
    assert decision.reason == (None if allowed else "audience_contained")


# ── the allow-list ───────────────────────────────────────────────────────────


def test_every_exemption_names_its_reason_and_budget_alerts_are_gated() -> None:
    assert set(POST_EXEMPTIONS) == {
        "reaction",
        "turn_notice",
        "hand_over_notice_edit",
        "setup_thread",
        "support_escalation",
        "private_reply",
    }
    assert all(len(reason.split()) >= 4 for reason in POST_EXEMPTIONS.values())
    assert "budget_alert" not in POST_EXEMPTIONS


# ── Gate A: the output latch ─────────────────────────────────────────────────


def _guarded(
    world: _World,
    *,
    hold: TurnHold | None = None,
    channel: str = "room-1",
    thread: str | None = None,
) -> tuple[OutputGate, AsyncMock]:
    check = audience_output_check(
        _Sessions(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        platform="discord",
        channel_id=channel,
        thread_id=thread,
        hold=hold,
        agent="helper",
    )
    assert check is not None
    gate = OutputGate()
    gate.set_output_authorization(OutputAuthorization(check))
    return gate, AsyncMock(return_value="sent")


async def test_mode_off_reads_no_fact_and_posts(world: _World) -> None:
    world.mode = "off"
    gate, send = _guarded(world)
    for _ in range(3):
        await gate.guarded_output(send)()
    assert send.await_count == 3
    assert (world.mode_reads, world.fact_reads) == (1, 0)


async def test_an_outsider_added_between_the_internal_read_and_the_post_blocks_the_post(
    world: _World,
) -> None:
    world.facts["room-1"] = OPEN  # what admission saw
    gate, send = _guarded(world)
    world.facts["room-1"] = CONTAINED  # a person with no internal role joins during the turn
    with pytest.raises(AdmissionDenied) as denied:
        await gate.guarded_output(send)()
    assert denied.value.detail == "audience_contained"
    send.assert_not_awaited()


async def test_an_outsider_added_after_the_first_post_blocks_each_later_edit(world: _World) -> None:
    world.facts["room-1"] = OPEN
    gate, send = _guarded(world)
    await gate.guarded_output(send)()
    world.facts["room-1"] = CONTAINED
    for _ in range(2):
        with pytest.raises(AdmissionDenied):
            await gate.guarded_output(send)()
    assert send.await_count == 1


async def test_a_held_answer_posts_in_its_own_room_and_its_thread(world: _World) -> None:
    world.facts["room-1"] = CONTAINED
    for thread in (None, "thread-9"):
        gate, send = _guarded(world, hold=HOLD, thread=thread)
        await gate.guarded_output(send)()
        await gate.guarded_output(send)()
        assert send.await_count == 2


async def test_an_unknown_room_is_contained(world: _World) -> None:
    gate, send = _guarded(world)  # no fact recorded for room-1
    with pytest.raises(AdmissionDenied):
        await gate.guarded_output(send)()
    send.assert_not_awaited()


async def test_shadow_posts_and_logs_one_record_without_text(world: _World) -> None:
    world.mode = "shadow"
    world.facts["room-1"] = CONTAINED
    gate, send = _guarded(world)
    with capture_logs() as logs:
        for _ in range(3):
            await gate.guarded_output(send)(content="the secret text")  # type: ignore[call-arg]
    assert send.await_count == 3
    records = [log for log in logs if log["event"] == "audience.shadow"]
    assert records == [
        {
            "event": "audience.shadow",
            "log_level": "info",
            "kind": "post_refused",
            "tenant_id": str(TENANT),
            "channel_id": "room-1",
            "agent": "helper",
            "reason": "audience_contained",
        }
    ]
    assert "secret" not in str(logs)


async def test_a_failed_mode_read_fails_the_post_and_a_later_post_reads_again(
    world: _World,
) -> None:
    world.mode = RuntimeError("database down")
    world.facts["room-1"] = OPEN
    gate, send = _guarded(world)
    with pytest.raises(RuntimeError):
        await gate.guarded_output(send)()
    send.assert_not_awaited()
    world.mode = "on"
    await gate.guarded_output(send)()
    send.assert_awaited_once()


@pytest.mark.parametrize(
    ("platform", "channel", "private_dm"),
    [
        ("slack", "C1", None),
        ("teams", "19:x", None),
        ("discord", None, None),
        ("discord", "d1", "d1"),
    ],
)
def test_a_turn_with_no_gated_room_installs_nothing(
    platform: str, channel: str | None, private_dm: str | None
) -> None:
    installed: list[OutputAuthorization] = []
    lifecycle = SimpleNamespace(set_output_authorization=installed.append)
    admission = SimpleNamespace(
        private_dm_id=private_dm,
        origin_channel_id=channel,
        origin_thread_id=None,
        hold=None,
        agent=SimpleNamespace(name="helper"),
        audience_mode=None,
    )
    guard = _install_audience_gate(
        SimpleNamespace(sessionmaker=_Sessions()),  # type: ignore[arg-type]
        tenant_id=TENANT,
        platform=platform,
        admission=admission,  # type: ignore[arg-type]
        lifecycle=lifecycle,  # type: ignore[arg-type]
        prior=None,
    )
    assert guard is None and installed == []


async def test_the_turn_latch_keeps_the_earlier_latch(world: _World) -> None:
    world.facts["room-1"] = OPEN
    installed: list[OutputAuthorization] = []
    latches: list[OutputAuthorization] = []
    lifecycle = SimpleNamespace(
        set_output_authorization=installed.append, set_audience_latch=latches.append
    )
    admission = SimpleNamespace(
        private_dm_id=None,
        origin_channel_id="room-1",
        origin_thread_id="room-1",  # a thread equal to its channel is none
        hold=None,
        agent=SimpleNamespace(name="helper"),
        audience_mode=None,
    )
    revoked = {"yes": False}

    async def earlier() -> None:
        if revoked["yes"]:
            raise AdmissionDenied(reason="invoker_not_allowed")

    guard = _install_audience_gate(
        SimpleNamespace(sessionmaker=_Sessions()),  # type: ignore[arg-type]
        tenant_id=TENANT,
        platform="discord",
        admission=admission,  # type: ignore[arg-type]
        lifecycle=lifecycle,  # type: ignore[arg-type]
        prior=OutputAuthorization(earlier),
    )
    # The audience latch goes apart from the earlier one, which a sweep reads as a restriction.
    assert guard is not None and installed == [] and len(latches) == 1
    await guard.check()
    revoked["yes"] = True
    with pytest.raises(AdmissionDenied) as denied:
        await guard.check()
    assert denied.value.reason == "invoker_not_allowed"


# ── Gate B: protection_state and audience_fact ───────────────────────────────


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from daimon.core.turn import protection

    monkeypatch.setattr(
        protection, "load_access_policy", AsyncMock(return_value=TenantAccessPolicy())
    )


async def _state(audience: PostAudience | None, *, thread: str | None = None) -> ProtectionState:
    return await protection_state(
        _Sessions(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        channel_id="room-1",
        thread_id=thread,
        audience=audience,
    )


@pytest.mark.parametrize(
    ("mode", "audience", "expected"),
    [
        ("on", None, ProtectionState.UNPROTECTED),  # no opt-in: nothing about the audience
        ("off", PostAudience("discord"), ProtectionState.UNPROTECTED),
        ("shadow", PostAudience("discord"), ProtectionState.UNPROTECTED),
        ("on", PostAudience("discord"), ProtectionState.PROTECTED),
        ("on", PostAudience("discord", hold=HOLD), ProtectionState.UNPROTECTED),
        ("on", PostAudience("discord", held_if_contained=True), ProtectionState.UNPROTECTED),
        ("on", PostAudience("slack"), ProtectionState.UNPROTECTED),  # no audience_hold there
    ],
)
async def test_protection_state_with_the_audience(
    world: _World,
    policy: None,
    mode: str,
    audience: PostAudience | None,
    expected: ProtectionState,
) -> None:
    world.mode = mode
    world.facts["room-1"] = CONTAINED
    assert await _state(audience) is expected


async def test_protection_state_reads_nothing_without_the_opt_in(
    world: _World, policy: None
) -> None:
    assert await _state(None) is ProtectionState.UNPROTECTED
    assert (world.mode_reads, world.fact_reads) == (0, 0)


async def test_a_thread_is_held_through_its_parent(world: _World, policy: None) -> None:
    world.facts["room-1"] = CONTAINED
    assert await _state(PostAudience("discord", hold=HOLD), thread="thread-9") is (
        ProtectionState.UNPROTECTED
    )
    assert await _state(PostAudience("discord"), thread="thread-9") is ProtectionState.PROTECTED


async def test_an_unreadable_fact_is_unknown_and_posts_nothing(world: _World, policy: None) -> None:
    world.mode = RuntimeError("down")
    assert await _state(PostAudience("discord", held_if_contained=True)) is ProtectionState.UNKNOWN


async def test_audience_fact_modes(world: _World) -> None:
    world.facts["room-1"] = CONTAINED
    args: dict[str, Any] = {
        "tenant_id": TENANT,
        "platform": "discord",
        "channel_id": "thread-9",
        "parent_id": "room-1",
        "hold": None,
    }
    world.mode = "on"
    assert await audience_fact(_Sessions(), **args) == CONTAINED  # type: ignore[arg-type]
    world.mode = "off"
    assert await audience_fact(_Sessions(), **args) is None  # type: ignore[arg-type]
    world.mode = "shadow"
    with capture_logs() as logs:
        assert await audience_fact(_Sessions(), **args) is None  # type: ignore[arg-type]
    assert [log["channel_id"] for log in logs if log["event"] == "audience.shadow"] == ["thread-9"]
    world.mode = "on"
    world.fact_reads = 0
    assert await audience_fact(_Sessions(), **{**args, "platform": "slack"}) is None  # type: ignore[arg-type]
    assert world.fact_reads == 0


# ── C3: the stored hold ──────────────────────────────────────────────────────


def _routine(**changes: Any) -> Any:
    base: dict[str, Any] = {
        "tenant_id": TENANT,
        "id": uuid.UUID(int=1),
        "agent_name": "helper",
        "delivery_hold": None,
        "channel_id": None,
    }
    return SimpleNamespace(**{**base, **changes})


def test_the_stored_hold_round_trips_and_null_is_legacy() -> None:
    held = routine_hold.delivery_hold_value(TurnHold("discord", "room-1", client=False))
    assert held == {"held": True, "channel_id": "room-1", "client": False, "relay": False}
    assert routine_hold.delivery_hold_value(None) == {"held": False}
    stored = routine_hold.stored_delivery_hold(_routine(delivery_hold=held), platform="discord")
    assert stored.recorded and stored.hold == TurnHold("discord", "room-1", client=False)
    assert routine_hold.stored_delivery_hold(
        _routine(delivery_hold={"held": False}), platform="discord"
    ) == routine_hold.StoredHold(recorded=True, hold=None)
    assert not routine_hold.stored_delivery_hold(_routine(), platform="discord").recorded
    unreadable = routine_hold.stored_delivery_hold(
        _routine(delivery_hold={"held": True}), platform="discord"
    )
    assert unreadable == routine_hold.StoredHold(recorded=True, hold=None)


@pytest.mark.parametrize(
    ("mode", "stored", "refuses", "keeps_inside"),
    [
        ("off", {"held": False}, False, False),
        ("shadow", {"held": False}, False, False),
        ("on", None, True, False),  # no stored hold counts as unheld in on
        ("on", {"held": False}, True, False),  # made outside, room now contained
        (
            "on",
            {"held": True, "channel_id": "room-1", "client": False, "relay": False},
            False,
            True,
        ),
    ],
)
async def test_delivery_gate(
    world: _World, mode: str, stored: dict[str, Any] | None, refuses: bool, keeps_inside: bool
) -> None:
    world.mode = mode
    world.facts["room-1"] = CONTAINED
    gate = await routine_hold.delivery_gate(
        _Sessions(),  # type: ignore[arg-type]
        _routine(delivery_hold=stored, destination_kind="channel", destination_id="room-1"),
        platform="discord",
    )
    fact = await gate.audience("room-1", None)
    decision = authorize(
        TenantAccessPolicy(),
        subject=Subject(),
        action=Action.POST,
        place=Place(channel_id="room-1"),
        hold=gate.post_hold,
        audience=fact,
    )
    assert decision.allowed is not refuses
    assert gate.keeps_inside(held_today=False) is keeps_inside


# ── Review fixes: shadow never blocks, and mode `off` adds no read ───────────


class _FailingFacts(_World):
    """A world whose fact read fails while the mode read works."""

    async def _fact(
        self, session: object, *, tenant_id: uuid.UUID, platform: str, channel_id: str
    ) -> ChannelAudience:
        self.fact_reads += 1
        raise RuntimeError("registry down")


@pytest.fixture
def failing(monkeypatch: pytest.MonkeyPatch) -> _FailingFacts:
    return _FailingFacts(monkeypatch)


async def test_shadow_logs_a_failed_fact_read_and_posts(failing: _FailingFacts) -> None:
    failing.mode = "shadow"
    gate, send = _guarded(failing)
    with capture_logs() as logs:
        for _ in range(2):
            await gate.guarded_output(send)(content="the secret text")  # type: ignore[call-arg]
    assert send.await_count == 2
    records = [log for log in logs if log["event"] == "audience.shadow"]
    assert [(r["kind"], r["reason"]) for r in records] == [("fact_unreadable", "RuntimeError")] * 2
    assert "secret" not in str(logs)


async def test_on_keeps_a_failed_fact_read_closed_and_a_mode_error_explicit(
    failing: _FailingFacts,
) -> None:
    gate, send = _guarded(failing)
    with pytest.raises(RuntimeError, match="registry down"):
        await gate.guarded_output(send)()
    send.assert_not_awaited()
    failing.mode = RuntimeError("database down")
    gate, send = _guarded(failing)
    with pytest.raises(RuntimeError, match="database down"):
        await gate.guarded_output(send)()
    failing.mode = "shadow"  # a mode that resolved, then a fact that did not, is the shadow case
    failing.mode = RuntimeError("database down")
    gate, send = _guarded(failing)
    with pytest.raises(RuntimeError, match="database down"):
        await gate.guarded_output(send)()


async def test_audience_fact_in_shadow_posts_when_the_fact_cannot_be_read(
    failing: _FailingFacts,
) -> None:
    failing.mode = "shadow"
    args: dict[str, Any] = {
        "tenant_id": TENANT,
        "platform": "discord",
        "channel_id": "room-1",
        "parent_id": None,
        "hold": None,
    }
    with capture_logs() as logs:
        assert await audience_fact(_Sessions(), **args) is None  # type: ignore[arg-type]
    assert [log["kind"] for log in logs if log["event"] == "audience.shadow"] == ["fact_unreadable"]
    failing.mode = "on"
    with pytest.raises(RuntimeError, match="registry down"):
        await audience_fact(_Sessions(), **args)  # type: ignore[arg-type]


@pytest.mark.parametrize("admitted", ["off", "shadow", "on"])
async def test_a_turn_admitted_under_a_mode_reads_no_mode_and_opens_no_session_in_off(
    world: _World, admitted: str
) -> None:
    """The mode comes from the admission snapshot: the latch asks the database nothing for it."""
    world.facts["room-1"] = OPEN
    sessions = _Sessions()
    check = audience_output_check(
        sessions,  # type: ignore[arg-type]
        tenant_id=TENANT,
        platform="discord",
        channel_id="room-1",
        thread_id=None,
        hold=None,
        agent="helper",
        mode=admitted,  # type: ignore[arg-type]
    )
    assert check is not None
    for _ in range(3):
        await check()
    assert world.mode_reads == 0
    assert sessions.opened == (0 if admitted == "off" else 3), "only the fact is read"


async def test_the_mode_is_read_once_per_turn_when_admission_gave_none(world: _World) -> None:
    world.mode = "off"
    gate, send = _guarded(world)
    for _ in range(4):
        await gate.guarded_output(send)()
    assert world.mode_reads == 1


async def test_protection_state_reads_the_mode_and_fact_in_its_own_session(
    world: _World, policy: None
) -> None:
    sessions = _Sessions()
    world.facts["room-1"] = OPEN
    for mode in ("off", "shadow", "on"):
        world.mode = mode
        sessions.opened = 0
        state = await protection_state(
            sessions,  # type: ignore[arg-type]
            tenant_id=TENANT,
            channel_id="room-1",
            audience=PostAudience("discord"),
        )
        assert state is ProtectionState.UNPROTECTED
        assert sessions.opened == 1, f"one session for the policy, the mode and the fact ({mode})"


def test_admission_reads_the_mode_from_its_snapshot_and_the_turn_asks_for_it() -> None:
    from daimon.core.features.resolver import ResolvedFeature
    from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot
    from daimon.core.turn.admission import (
        _TURN_FEATURE_KEYS,  # pyright: ignore[reportPrivateUsage]
        Admission,
    )

    assert "audience_hold" in _TURN_FEATURE_KEYS
    place = FeaturePlace.of(TENANT, "discord", "room-1", None)

    def mode_of(snapshot: FeatureSnapshot | None) -> Any:
        return Admission.audience_mode.fget(
            SimpleNamespace(
                resolved=lambda key: (  # type: ignore[union-attr,arg-type]
                    snapshot.features.get(key) if snapshot is not None else None
                )
            )
        )

    for value in ("on", "shadow", "off"):
        snap = FeatureSnapshot(
            place, 1, {"audience_hold": ResolvedFeature("audience_hold", value, "workspace")}
        )
        assert mode_of(snap) == value
    assert mode_of(None) is None, "a hand-built admission reads the mode at the first output"

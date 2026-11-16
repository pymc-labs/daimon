"""The wait before a direct message starts a turn: setting, bursts and approvals."""

from __future__ import annotations

import asyncio
from time import monotonic

import pytest
from daimon.core import dm_debounce
from daimon.core.dm_debounce import DmBurst, DmBursts, debounce_seconds, split_approvals
from daimon.core.features.registry import FEATURES
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.factories import make_tenant


def test_the_setting_defaults_to_three_seconds_with_off_and_presets() -> None:
    spec = FEATURES["dm_debounce_seconds"]
    assert spec.default == "3"
    assert {"0", "1", "3", "5"} <= set(spec.values)
    assert spec.platforms == frozenset({"discord", "slack", "teams"}), (
        "web has no hold-and-merge path"
    )
    assert dm_debounce.MAX_WAIT_S == 10.0


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_the_wait_follows_the_workspace_setting(db_session_factory, platform) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform=platform)
    assert await debounce_seconds(db_session_factory, None, tenant.id, platform) == 3.0
    for chosen in ("0", "5"):
        async with db_session_factory.begin() as session:
            await set_feature_value(
                session,
                tenant_id=tenant.id,
                target=FeatureTarget("workspace"),
                key="dm_debounce_seconds",
                value=chosen,
                set_by=None,
                surface="chat",
            )
        assert await debounce_seconds(db_session_factory, None, tenant.id, platform) == float(
            chosen
        )


def test_approvals_stand_alone_between_runs_of_ordinary_messages() -> None:
    texts = ["a", "b", "yes", "c", "ok", "ok", "d"]
    groups = split_approvals(texts, lambda text: text)
    assert groups == [["a", "b"], ["yes"], ["c"], ["ok"], ["ok"], ["d"]]
    assert split_approvals(["a", "b"], lambda text: text) == [["a", "b"]]
    assert split_approvals(["yes but not now"], str) == [["yes but not now"]], "not an approval"


class _Turns:
    """Records each turn's group and every mark put on or taken off."""

    def __init__(self, during=None) -> None:
        self.groups: list[list[str]] = []
        self.marks: list[tuple[str, bool]] = []
        self._during = during

    async def react(self, item: str, on: bool) -> None:
        self.marks.append((item, on))

    async def answer(self, group: list[str]) -> None:
        self.groups.append(group)
        if self._during is not None:
            await self._during(group)


async def _later(delay: float, action) -> None:
    await asyncio.sleep(delay)
    await action()


async def test_a_burst_is_answered_after_a_quiet_gap_in_one_turn() -> None:
    burst = DmBurst("one", str)
    turns = _Turns()

    async def second() -> None:
        burst.add("two")
        await burst.mark("two", turns.react)

    started = monotonic()
    later = asyncio.create_task(_later(0.15, second))
    await burst.answer(0.3, turns.react, turns.answer)
    await later
    assert turns.groups == [["one", "two"]], "one turn answers both"
    assert turns.marks == [("one", True), ("two", True), ("one", False), ("two", False)], (
        "each message is marked while it waits and unmarked when its turn ends"
    )
    assert 0.4 <= monotonic() - started < 1.0, "the gap restarts at the second message"
    assert burst.closed and not burst.unanswered


async def _first_turn_at(burst: DmBurst[str], started: float) -> float:
    """When the first turn starts, while someone keeps typing every 0.05 s."""
    first: list[float] = []

    async def during(group: list[str]) -> None:
        first.append(monotonic() - started)
        burst.stop()  # the rest is answered at once

    async def keep_typing() -> None:
        for number in range(1, 40):
            burst.add(str(number))
            await asyncio.sleep(0.05)

    typing = asyncio.create_task(keep_typing())
    await burst.answer(0.3, _Turns().react, _Turns(during).answer)
    typing.cancel()
    return first[0]


async def test_a_steady_stream_is_cut_off_at_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(dm_debounce, "MAX_WAIT_S", 0.5)
    started = monotonic()
    assert 0.45 <= await _first_turn_at(DmBurst("0", str), started) < 0.9, (
        "the cap starts the turn though the quiet gap never came"
    )


async def test_an_approval_reply_ends_the_wait_and_stands_alone() -> None:
    burst = DmBurst("question", str)
    turns = _Turns()

    async def approve() -> None:
        burst.add("yes")

    started = monotonic()
    later = asyncio.create_task(_later(0.05, approve))
    await burst.answer(5.0, turns.react, turns.answer)
    await later
    assert turns.groups == [["question"], ["yes"]], "the approval gets its own turn"
    assert monotonic() - started < 1.0, "no wait once it arrives"


async def test_an_approval_alone_is_answered_unmarked() -> None:
    turns = _Turns()
    await DmBurst("yes", str).answer(5.0, turns.react, turns.answer)
    assert turns.groups == [["yes"]] and turns.marks == [], "nothing waited"


async def test_zero_seconds_answers_at_once_and_still_merges_what_joined() -> None:
    """With the wait off, a message that joined during the lookups is held and merged."""
    burst = DmBurst("one", str)
    turns = _Turns()
    burst.add("two")
    await burst.mark("two", turns.react)
    started = monotonic()
    await burst.answer(0, turns.react, turns.answer)
    assert turns.groups == [["one", "two"]], "nothing more is awaited"
    assert turns.marks == [("two", True), ("two", False)], "only the held one was marked"
    assert monotonic() - started < 0.2, "no wait"


async def test_the_cap_counts_from_the_first_message_not_the_wait(monkeypatch) -> None:
    """Lookups between the first message and the wait do not extend the cap."""
    monkeypatch.setattr(dm_debounce, "MAX_WAIT_S", 0.5)
    burst = DmBurst("0", str)
    await asyncio.sleep(0.3)
    assert await _first_turn_at(burst, monotonic()) < 0.4, "the cap ends 0.5 s after it opened"


async def test_messages_sent_during_a_turn_are_answered_by_the_next() -> None:
    burst: DmBurst[str]
    marks_during: list[list[tuple[str, bool]]] = []

    async def during(group: list[str]) -> None:
        if group == ["one"]:
            burst.add("two")
            await burst.mark("two", turns.react)
        marks_during.append(list(turns.marks))

    turns = _Turns(during)
    burst = DmBurst("one", str)
    await burst.answer(0.05, turns.react, turns.answer)
    assert turns.groups == [["one"], ["two"]], "the second turn answers what the first missed"
    assert ("one", False) in marks_during[1] and ("two", False) not in marks_during[1], (
        "the first is unmarked after its turn; the second stays marked through its own"
    )
    assert turns.marks[-1] == ("two", False)


async def test_a_mark_after_its_turn_ended_comes_off_at_once() -> None:
    burst = DmBurst("one", str)
    turns = _Turns()
    burst.add("two")
    await burst.answer(0, turns.react, turns.answer)
    await burst.mark("two", turns.react)
    assert turns.marks == [("two", True), ("two", False)], "no mark is left behind"


async def test_a_failure_leaves_the_rest_unanswered_and_unmarked() -> None:
    async def fail(group: list[str]) -> None:
        raise RuntimeError("down")

    bursts: DmBursts[str, str] = DmBursts(str)
    burst = bursts.open("k", "one")
    turns = _Turns()
    burst.add("two")
    await burst.mark("two", turns.react)
    with pytest.raises(RuntimeError):
        await burst.answer(0, turns.react, fail)
    assert await bursts.end("k", burst) == ["one", "two"], "the caller tells the person"
    assert turns.marks[-1] == ("two", False) and await bursts.end("k", burst) == [], "once"


async def test_stopping_ends_a_wait_now() -> None:
    burst = DmBurst("one", str)
    turns = _Turns()
    asyncio.get_running_loop().call_later(0.05, burst.stop)
    started = monotonic()
    await burst.answer(5.0, turns.react, turns.answer)
    assert turns.groups == [["one"]] and monotonic() - started < 1.0, "answered as it is"


def test_a_message_joins_only_an_open_burst() -> None:
    bursts: DmBursts[str, str] = DmBursts(str)
    assert bursts.join("k", "early") is None
    burst = bursts.open("k", "first")
    assert bursts.join("k", "second") is burst and burst.unanswered == ["first", "second"]
    burst.closed = True
    assert bursts.join("k", "late") is None, "a closed burst takes nothing more"


async def test_ending_a_stale_burst_keeps_the_newer_one() -> None:
    bursts: DmBursts[str, str] = DmBursts(str)
    old = bursts.open("k", "first")
    newer = bursts.open("k", "next")
    await bursts.end("k", old)
    assert bursts.join("k", "still open") is newer and newer.unanswered == ["next", "still open"]


async def test_stopping_ends_every_wait_and_skips_later_ones() -> None:
    """A burst opened just after the stop does not wait either."""
    bursts: DmBursts[str, str] = DmBursts(str)
    turns = _Turns()
    started = monotonic()
    waiting = asyncio.create_task(bursts.open("a", "one").answer(5.0, turns.react, turns.answer))
    await asyncio.sleep(0.05)
    bursts.stop()
    await waiting
    await bursts.open("b", "two").answer(5.0, turns.react, turns.answer)
    assert turns.groups == [["one"], ["two"]], "both answered as they are"
    assert monotonic() - started < 1.0, "neither waited"
    assert ("two", True) not in turns.marks, "a burst that never waits is never marked"

"""The wait before a direct message starts a turn: setting, window and approvals."""

from __future__ import annotations

import asyncio
from time import monotonic

import pytest
from daimon.core import dm_debounce
from daimon.core.dm_debounce import (
    DmDebouncer,
    DmWindow,
    bypasses_wait,
    debounce_seconds,
    split_approvals,
)
from daimon.core.features.registry import FEATURES
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.factories import make_tenant


def test_the_setting_defaults_to_three_seconds_with_off_and_presets() -> None:
    spec = FEATURES["dm_debounce_seconds"]
    assert spec.default == "3"
    assert {"0", "1", "3", "5"} <= set(spec.values)
    assert spec.platforms == frozenset({"discord", "slack"}), "web has no hold-and-merge path"
    assert dm_debounce.MAX_WAIT_S == 10.0


@pytest.mark.parametrize("platform", ["discord", "slack"])
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


def test_an_approval_reply_is_never_held() -> None:
    assert bypasses_wait("yes") and bypasses_wait("Yes, please!")
    assert not bypasses_wait("yes but not now") and not bypasses_wait("what is this?")


def test_approvals_stand_alone_between_runs_of_ordinary_messages() -> None:
    texts = ["a", "b", "yes", "c", "ok", "ok", "d"]
    groups = split_approvals(texts, lambda text: text)
    assert groups == [["a", "b"], ["yes"], ["c"], ["ok"], ["ok"], ["d"]]
    assert split_approvals(["a", "b"], lambda text: text) == [["a", "b"]]


async def _later(delay: float, action) -> None:
    await asyncio.sleep(delay)
    action()


async def test_the_window_closes_after_a_quiet_gap_with_every_message() -> None:
    window = DmWindow("one")
    reacted: list[str] = []

    async def seen(item: str) -> None:
        reacted.append(item)

    started = monotonic()
    later = asyncio.create_task(_later(0.15, lambda: window.add("two")))
    messages = await window.settle(0.3, on_arrival=seen)
    await later
    assert messages == ["one", "two"]
    assert reacted == ["one", "two"], "each message is marked once, as it arrives"
    assert 0.4 <= monotonic() - started < 1.0, "the gap restarts at the second message"


async def test_a_steady_stream_is_cut_off_at_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(dm_debounce, "MAX_WAIT_S", 0.5)
    window = DmWindow(0)

    async def keep_typing() -> None:
        for number in range(1, 20):
            await asyncio.sleep(0.1)
            window.add(number)

    typing = asyncio.create_task(keep_typing())
    started = monotonic()
    messages = await window.settle(0.3)
    elapsed = monotonic() - started
    typing.cancel()
    assert 0.45 <= elapsed < 0.9, "closed by the cap, not the quiet gap"
    assert len(messages) >= 4


async def test_an_approval_reply_ends_the_wait_now() -> None:
    window = DmWindow("question")
    started = monotonic()
    later = asyncio.create_task(_later(0.05, lambda: window.add("yes", flush=True)))
    messages = await window.settle(5.0)
    await later
    assert messages == ["question", "yes"] and monotonic() - started < 1.0


async def test_zero_seconds_takes_only_what_already_joined() -> None:
    window = DmWindow("one")
    window.add("two")
    reacted: list[str] = []

    async def seen(item: str) -> None:
        reacted.append(item)

    started = monotonic()
    assert await window.settle(0, on_arrival=seen) == ["one", "two"]
    assert not reacted, "no wait, no mark"
    assert monotonic() - started < 0.2


async def test_a_stop_signal_ends_the_wait() -> None:
    window = DmWindow("one")
    started = monotonic()
    flag = {"stop": False}
    asyncio.get_running_loop().call_later(0.1, lambda: flag.update(stop=True))
    await window.settle(5.0, stop=lambda: flag["stop"])
    assert monotonic() - started < 2.0, "noticed within one slice"


def test_a_message_joins_only_an_open_window() -> None:
    bursts: DmDebouncer[str, str] = DmDebouncer()
    assert not bursts.join("k", "early")
    window = bursts.open("k", "first")
    assert bursts.join("k", "second") and window.items == ["first", "second"]
    bursts.close("k", window)
    assert not bursts.join("k", "late"), "after the close, a message opens its own window"
    newer = bursts.open("k", "next")
    bursts.close("k", window)
    assert bursts.join("k", "still open") and newer.items == ["next", "still open"]

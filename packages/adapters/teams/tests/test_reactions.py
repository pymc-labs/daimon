"""TeamsReactions: background, rate-spaced reactions that never fail their caller."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Coroutine
from typing import Any

import httpx
from daimon.adapters.teams.reactions import (
    CHECK,
    EYES,
    HOURGLASS,
    MAX_WAIT_S,
    MIN_INTERVAL_S,
    ReactionTarget,
    TeamsReactions,
    reaction_target,
)

from .conftest import make_inbound


class Calls:
    """A `React` recording each call; reactions in `fail` raise a 429 first."""

    def __init__(self, *, fail: frozenset[str] = frozenset()) -> None:
        self.made: list[tuple[str, str, bool]] = []
        self.chats: list[str] = []
        self.fail = fail

    async def __call__(self, target: ReactionTarget, reaction: str, *, add: bool) -> None:
        self.made.append((target.activity_id, reaction, add))
        self.chats.append(target.conversation_id)
        if reaction in self.fail:
            request = httpx.Request("PUT", "https://smba.example.test/v3/conversations/c")
            response = httpx.Response(429, headers={"Retry-After": "1"}, request=request)
            raise httpx.HTTPStatusError("throttled", request=request, response=response)


class Harness:
    """A frozen clock, recorded sleeps and the tasks the reactions spawned."""

    def __init__(self, react: Calls | None) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.reactions = TeamsReactions(
            react, spawn=self.spawn, clock=lambda: self.now, sleep=self.sleep
        )

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks)


async def test_a_turn_reacts_eyes_then_swaps_them_for_a_check_in_order() -> None:
    calls = Calls()
    harness = Harness(calls)
    inbound = make_inbound()
    acknowledge = harness.reactions.acknowledger(inbound)
    assert acknowledge is not None, "a person's message can be reacted to"
    acknowledge("accepted")
    acknowledge("done")
    await harness.settle()
    key = inbound.activity_id
    assert calls.made == [(key, EYES, True), (key, CHECK, True), (key, EYES, False)], (
        "👀 lands first, then ✅, and only then do the eyes go"
    )
    assert harness.slept == [MIN_INTERVAL_S, 2 * MIN_INTERVAL_S], (
        "calls are spaced to the bot's two-a-second limit"
    )


async def test_a_queued_message_shows_an_hourglass_until_its_work_starts() -> None:
    calls = Calls()
    harness = Harness(calls)
    first, second = make_inbound("one"), make_inbound("two")
    harness.reactions.mark_queued(first)
    harness.reactions.mark_queued(second)
    composed = dataclasses.replace(second, composed_ids=(first.activity_id,))
    harness.reactions.clear_queued([composed])
    harness.reactions.clear_queued([composed])
    await harness.settle()
    assert sorted(calls.made) == sorted(
        [
            (first.activity_id, HOURGLASS, True),
            (second.activity_id, HOURGLASS, True),
            (first.activity_id, HOURGLASS, False),
            (second.activity_id, HOURGLASS, False),
        ]
    ), "each queued message folded into the turn loses its ⌛ once, a second clear is a no-op"
    assert calls.made.index((first.activity_id, HOURGLASS, True)) < calls.made.index(
        (first.activity_id, HOURGLASS, False)
    ), "a removal never overtakes the add it undoes"


async def test_the_same_activity_id_in_two_chats_keeps_two_hourglasses() -> None:
    """Activity ids repeat across conversations: each chat's ⌛ comes off its own message."""
    calls = Calls()
    harness = Harness(calls)
    chat_a, chat_b = make_inbound(conversation="chat-a"), make_inbound(conversation="chat-b")
    chat_b = dataclasses.replace(chat_b, activity_id=chat_a.activity_id)
    harness.reactions.mark_queued(chat_a)
    harness.reactions.mark_queued(chat_b)
    harness.reactions.clear_queued([chat_a])
    harness.reactions.clear_queued([chat_b])
    await harness.settle()
    removed = sorted(
        chat for chat, (_, _, add) in zip(calls.chats, calls.made, strict=True) if not add
    )
    assert removed == ["chat-a", "chat-b"], "neither chat's mark overwrote the other's"


async def test_a_reaction_still_throttled_after_the_client_retry_is_dropped() -> None:
    """The bot client retries a 429 once below `react`; one that still fails reaches here."""
    calls = Calls(fail=frozenset({CHECK}))
    harness = Harness(calls)
    target = reaction_target(make_inbound())
    assert target is not None, "a person's message can be reacted to"
    harness.reactions.acknowledge(target, "accepted")
    harness.reactions.acknowledge(target, "done")
    await harness.settle()
    assert [reaction for _, reaction, _ in calls.made] == [EYES, CHECK, EYES], (
        "no second retry here, and the calls after it still run"
    )
    assert all(task.exception() is None for task in harness.tasks), "no failure escapes"


async def test_reactions_past_the_wait_budget_are_skipped_not_queued() -> None:
    calls = Calls()
    harness = Harness(calls)
    budget = int(MAX_WAIT_S / MIN_INTERVAL_S) + 1
    for _ in range(budget + 3):
        harness.reactions.mark_queued(make_inbound())
    await harness.settle()
    assert len(calls.made) == budget, "a burst beyond the bounded wait drops its extra reactions"


async def test_a_removal_whose_add_was_sent_runs_even_past_the_wait_budget() -> None:
    """A burst of ⌛ fills the wait budget; the removal still comes off message one."""
    calls = Calls()
    harness = Harness(calls)
    burst = [make_inbound(str(n)) for n in range(int(MAX_WAIT_S / MIN_INTERVAL_S) + 1)]
    for inbound in burst:
        harness.reactions.mark_queued(inbound)
    harness.reactions.clear_queued(burst[:1])
    await harness.settle()
    first = burst[0].activity_id
    assert (first, HOURGLASS, True) in calls.made, "the first ⌛ went out"
    assert (first, HOURGLASS, False) in calls.made, "so its removal is never dropped"


async def test_eyes_come_off_an_answered_message_under_a_burst() -> None:
    """👀 went out before the burst: its removal runs though the ✅ add may be dropped."""
    calls = Calls()
    harness = Harness(calls)
    target = reaction_target(make_inbound())
    assert target is not None, "a person's message can be reacted to"
    harness.reactions.acknowledge(target, "accepted")
    for n in range(int(MAX_WAIT_S / MIN_INTERVAL_S) + 1):
        harness.reactions.mark_queued(make_inbound(str(n)))
    harness.reactions.acknowledge(target, "done")
    await harness.settle()
    key = target.activity_id
    assert (key, EYES, True) in calls.made, "👀 went out first"
    assert (key, EYES, False) in calls.made, "👀 never outlives the answer"
    assert (key, CHECK, True) not in calls.made, "an add past the budget is still dropped"


async def test_a_removal_is_skipped_when_its_add_was_dropped_or_refused() -> None:
    calls = Calls(fail=frozenset({EYES}))
    harness = Harness(calls)
    burst = [make_inbound(str(n)) for n in range(int(MAX_WAIT_S / MIN_INTERVAL_S) + 2)]
    for inbound in burst:
        harness.reactions.mark_queued(inbound)
    harness.reactions.clear_queued(burst[-1:])
    refused = reaction_target(make_inbound())
    assert refused is not None, "a person's message can be reacted to"
    harness.reactions.acknowledge(refused, "accepted")
    await harness.settle()
    harness.now = 60.0  # the burst is long gone
    harness.reactions.acknowledge(refused, "done")
    await harness.settle()
    dropped = burst[-1].activity_id
    assert all(key != dropped for key, _, _ in calls.made), "no ⌛ went out, so none comes off"
    removals = [(key, reaction) for key, reaction, add in calls.made if not add]
    assert removals == [], "a refused 👀 is not removed either"


async def test_a_message_held_across_a_restart_loses_its_hourglass_once_picked_up() -> None:
    """This process never marked it, but the one before may have: the removal goes anyway."""
    calls = Calls()
    harness = Harness(calls)
    held = make_inbound()
    harness.reactions.clear_held([held])
    await harness.settle()
    assert calls.made == [(held.activity_id, HOURGLASS, False)], "⌛ is taken off regardless"


async def test_close_cancels_waiting_reactions_and_sends_no_more() -> None:
    """Shutdown never waits on a reaction, whether it waits for Teams or for its slot."""
    stuck = asyncio.Event()

    async def react(target: ReactionTarget, reaction: str, *, add: bool) -> None:
        await stuck.wait()

    reactions = TeamsReactions(react)
    reactions.mark_queued(make_inbound("one"))
    reactions.mark_queued(make_inbound("two"))
    await asyncio.sleep(0)
    async with asyncio.timeout(1):
        await reactions.close()
    assert reactions._running == set(), "every reaction task ended"
    reactions.mark_queued(make_inbound("three"))
    assert reactions._running == set(), "nothing new starts after close"


async def test_a_refused_hourglass_is_never_added_nor_removed_though_its_turn_starts_first() -> (
    None
):
    """The gate answers late: the removal queued meanwhile still waits for it."""
    calls = Calls()
    harness = Harness(calls)
    answer = asyncio.Event()

    async def refused() -> bool:
        await answer.wait()
        return False

    queued = make_inbound()
    harness.reactions.mark_queued(queued, allowed=refused)
    harness.reactions.clear_queued([queued])
    answer.set()
    await harness.settle()
    assert calls.made == [], "no ⌛ for a message admission would refuse, so none to remove"


async def test_nothing_reacts_to_a_form_a_reply_nobody_addressed_or_without_a_client() -> None:
    form = dataclasses.replace(make_inbound(), from_form=True)
    unprompted = dataclasses.replace(make_inbound(), unprompted=True)
    assert reaction_target(form) is None, "a form's submit names no message"
    assert reaction_target(unprompted) is None, "nobody asked, so nothing is signalled"
    harness = Harness(None)
    assert harness.reactions.acknowledger(make_inbound()) is None, "no client, no reactions"
    harness.reactions.mark_queued(make_inbound())
    assert harness.tasks == [], "without a client nothing is spawned"

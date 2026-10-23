"""Reactions on the asker's message: 👀 once a turn is accepted, ✅ once answered, ⌛ while queued.

Bot Framework rate-limits a bot's reactions to two a second across every
conversation, answering 429 past that
(https://learn.microsoft.com/microsoftteams/platform/agents-in-teams/agent-reactions).
A reaction is a nicety, so none may fail or delay a turn or intake: each call
runs in the background, spaced to that limit in this process. An add that
would wait longer than `MAX_WAIT_S` for its slot is dropped rather than queued
behind a burst. A removal is never dropped, since a lost one would leave ⌛ or
👀 on the message for good; it only waits its turn, and is skipped when its add
was dropped or refused, as there is nothing to remove. Calls on one message run
in order, so a removal never overtakes the add it undoes. The bot client
retries a 429 once, after the wait Teams asks for and within the call's
timeout, as for every Bot Framework call; a call that still fails is logged and
dropped, never retried here.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
import structlog
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.core.turn.lifecycle import Acknowledgment

log = structlog.get_logger(__name__)

# Teams' reaction ids: the code point, then the emoji's name, as listed at
# https://learn.microsoft.com/microsoftteams/platform/agents-in-teams/teams-reactions-reference
EYES: Final = "1f440_eyes"
CHECK: Final = "2705_whiteheavycheckmark"
HOURGLASS: Final = "231b_hourglassdone"

MIN_INTERVAL_S = 0.5
MAX_WAIT_S = 5.0
CALL_TIMEOUT_S = 5.0
# Adds dropped or refused, remembered until their removal comes. A turn that never
# answers never removes its 👀, so the oldest are forgotten past this many.
MAX_ABSENT = 1024


# Activity ids are unique only within a conversation, as the app's delivery dedupe keys them.
MessageKey = tuple[str, str]


@dataclass(frozen=True)
class ReactionTarget:
    """A message a person sent, where Bot Framework can react to it."""

    conversation_id: str
    activity_id: str
    service_url: str | None

    @property
    def key(self) -> MessageKey:
        return (self.conversation_id, self.activity_id)


def reaction_target(inbound: TeamsInbound) -> ReactionTarget | None:
    """The message `inbound` answers; None for a submitted form or a reply nobody addressed."""
    if inbound.from_form or inbound.unprompted:
        return None
    return ReactionTarget(
        inbound.conversation_id,
        inbound.handover_notice_id or inbound.activity_id,
        inbound.service_url,
    )


class React(Protocol):
    """Adds (`add`) or removes one of the bot's reactions on `target`."""

    def __call__(self, target: ReactionTarget, reaction: str, *, add: bool) -> Awaitable[None]: ...


Spawn = Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]


def _spawn_reaction(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    return asyncio.create_task(coro, name="teams.reaction")


class TeamsReactions:
    """Background, rate-spaced reactions, with the ⌛ marks still owed a removal.

    Without `react` (no Bot Framework client) every method does nothing. Its
    tasks are its own, outside the app's: shutdown cancels them (`close`)
    rather than wait on a reaction.
    """

    def __init__(
        self,
        react: React | None,
        *,
        spawn: Spawn = _spawn_reaction,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._react = react
        self._spawn = spawn
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        # The last call queued per message, which the next one on it waits for.
        self._chains: dict[MessageKey, asyncio.Task[None]] = {}
        # Messages showing ⌛.
        self._queued: dict[MessageKey, ReactionTarget] = {}
        # Reactions whose add never reached the message: their removal is skipped.
        self._absent: OrderedDict[tuple[MessageKey, str], None] = OrderedDict()
        self._running: set[asyncio.Task[None]] = set()
        self._closed = False

    def acknowledger(self, inbound: TeamsInbound) -> Callable[[Acknowledgment], None] | None:
        """The lifecycle's `acknowledge` for a turn answering `inbound`, if it can react."""
        if self._react is None or (target := reaction_target(inbound)) is None:
            return None
        return functools.partial(self.acknowledge, target)

    def acknowledge(self, target: ReactionTarget, phase: Acknowledgment) -> None:
        """👀 when the turn is accepted; ✅ in its place once it answered."""
        if phase == "accepted":
            self._queue(target, EYES, add=True)
            return
        self._queue(target, CHECK, add=True)
        self._queue(target, EYES, add=False)

    def mark_queued(
        self, inbound: TeamsInbound, *, allowed: Callable[[], Awaitable[bool]] | None = None
    ) -> None:
        """⌛ on a message waiting behind the turn running in its chat, once `allowed` says yes.

        `allowed` runs in the background, ahead of the add on the message's
        chain, so a removal queued meanwhile still waits for its answer.
        """
        if self._react is None or (target := reaction_target(inbound)) is None:
            return
        self._queued[(inbound.conversation_id, inbound.activity_id)] = target
        self._queue(target, HOURGLASS, add=True, allowed=allowed)

    def clear_queued(self, items: Iterable[TeamsInbound]) -> None:
        """Remove the ⌛ from these messages, if they show one: their work started or settled."""
        for item in items:
            for activity_id in item.message_ids:
                key = (item.conversation_id, activity_id)
                if (target := self._queued.pop(key, None)) is not None:
                    self._queue(target, HOURGLASS, add=False)

    def clear_held(self, items: Iterable[TeamsInbound]) -> None:
        """Remove ⌛ from messages an earlier process may have marked, now picked up again.

        A restart can cut off that process's removal, and this one cannot tell
        which messages it marked, so the removal is sent whatever they show.
        """
        for item in items:
            if (target := reaction_target(item)) is not None:
                self._queued.pop((item.conversation_id, item.activity_id), None)
                self._queue(target, HOURGLASS, add=False, known=False)

    async def close(self) -> None:
        """Cancel every reaction still waiting or in flight, and send no more."""
        self._closed = True
        running = list(self._running)
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    def _queue(
        self,
        target: ReactionTarget,
        reaction: str,
        *,
        add: bool,
        known: bool = True,
        allowed: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        react = self._react
        if react is None or self._closed:
            return
        key = target.key
        previous = self._chains.get(key)
        run = self._run(react, previous, target, reaction, add=add, known=known, allowed=allowed)
        task = self._spawn(run)
        self._chains[key] = task
        self._running.add(task)

        def forget(done: asyncio.Task[None]) -> None:
            self._running.discard(done)
            if self._chains.get(key) is done:
                del self._chains[key]

        task.add_done_callback(forget)

    async def _run(
        self,
        react: React,
        previous: asyncio.Task[None] | None,
        target: ReactionTarget,
        reaction: str,
        *,
        add: bool,
        known: bool,
        allowed: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        if previous is not None:
            await asyncio.wait([previous])  # never raises: its outcome is its own
        mark = (target.key, reaction)
        if allowed is not None and not await allowed():
            self._remember_absent(mark)
            log.debug("teams.reaction.skipped", reaction=reaction, add=add, reason="not_allowed")
            return
        if not add and mark in self._absent:
            del self._absent[mark]
            if known:  # this process's own add never landed: nothing to take off
                log.debug("teams.reaction.skipped", reaction=reaction, add=add, reason="not_added")
                return
        now = self._clock()
        slot = max(now, self._next_slot)
        if add and slot - now > MAX_WAIT_S:
            self._remember_absent(mark)
            log.debug("teams.reaction.skipped", reaction=reaction, add=add, reason="rate")
            return
        self._absent.pop(mark, None)
        self._next_slot = slot + MIN_INTERVAL_S
        if slot > now:
            await self._sleep(slot - now)
        try:
            async with asyncio.timeout(CALL_TIMEOUT_S):
                await react(target, reaction, add=add)
        except (*TEAMS_SEND_ERRORS, TimeoutError) as err:
            # Only a refusal is known not to have landed; a timeout may have.
            if add and isinstance(err, httpx.HTTPStatusError):
                self._remember_absent(mark)
            status = getattr(getattr(err, "response", None), "status_code", None)
            log.debug(
                "teams.reaction.failed",
                reaction=reaction,
                add=add,
                status=status,
                error=type(err).__name__,
            )

    def _remember_absent(self, mark: tuple[MessageKey, str]) -> None:
        self._absent[mark] = None
        if len(self._absent) > MAX_ABSENT:
            self._absent.popitem(last=False)

"""A typed "stop" in a thread whose reply is running: the same stop as the card's Stop button.

Deterministic, no model call: only a message that is nothing but a stop phrase
(after the bot's own mention, case and trailing punctuation are removed), sent in
the thread of a running turn by the person who asked or a server admin, sets that
turn's cancel event. Anything longer is an ordinary message.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal

STOP_PHRASES = frozenset(
    {
        "stop",
        "stop it",
        "stop that",
        "stop this",
        "stop now",
        "stop the reply",
        "cancel",
        "cancel it",
        "cancel that",
        "cancel this",
        "abort",
        "never mind",
        "nevermind",
    }
)

_SPACES = re.compile(r"\s+")

StopOutcome = Literal["stopped", "already_stopping", "not_allowed", "no_running_turn"]

STOPPED = "Stopping this reply."
ALREADY_STOPPING = "This reply is already stopping."


def is_stop_request(text: str, *, bot_id: int | None) -> bool:
    """Whether `text` is only a stop phrase, e.g. "stop", "@Daimon cancel that!", "please stop".

    Only the bot's own mention is removed: "@Alice stop" is a message to Alice.
    """
    if bot_id is not None:
        text = re.sub(rf"<@!?{bot_id}>", " ", text)
    words = _SPACES.sub(" ", text).strip().casefold().rstrip(".!")
    words = words.removeprefix("please ").removesuffix(" please").strip(" ,")
    return words in STOP_PHRASES


@dataclass(frozen=True)
class _RunningTurn:
    requester_id: int
    cancel: asyncio.Event


class TypedStops:
    """The running turn of each thread, so a typed stop can set its cancel event."""

    def __init__(self) -> None:
        self._turns: dict[int, _RunningTurn] = {}

    def watch(self, thread_id: int, *, requester_id: int, cancel: asyncio.Event) -> None:
        """This thread's running turn is now the one `cancel` stops (a recovery replaces it)."""
        self._turns[thread_id] = _RunningTurn(requester_id=requester_id, cancel=cancel)

    def release(self, thread_id: int) -> None:
        """The thread's turn is over."""
        self._turns.pop(thread_id, None)

    def stop(self, thread_id: int, *, author_id: int, is_admin: bool) -> StopOutcome:
        """Stop the thread's running turn for its requester or a server admin; idempotent."""
        turn = self._turns.get(thread_id)
        if turn is None:
            return "no_running_turn"
        if author_id != turn.requester_id and not is_admin:
            return "not_allowed"
        if turn.cancel.is_set():
            return "already_stopping"
        turn.cancel.set()
        return "stopped"

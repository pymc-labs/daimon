"""Human principal provenance, independent of the tool confirmation policy."""

from __future__ import annotations

HUMAN_MESSAGE_ORIGINS = frozenset({"chat", "handoff", "task_handoff"})


def human_invocation(origin: str, attended: bool | None) -> bool:
    """Only a person's message or click carries a principal.

    Every wake, headless send and unknown future origin lacks a human principal,
    even when it retains a recorded owner whose current membership can be looked up.
    """
    return origin in HUMAN_MESSAGE_ORIGINS and attended is not False


def unattended_invocation(origin: str, attended: bool | None) -> bool:
    """Nobody waits on this turn: its caller says so, or, unsaid, a routine started it.

    Wakes always say. Decides the unattended tool rules and the unattended run limit alike.
    """
    return attended is False or (attended is None and origin == "routine")

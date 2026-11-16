"""Human principal provenance, independent of the tool confirmation policy."""

from __future__ import annotations

HUMAN_MESSAGE_ORIGINS = frozenset({"chat", "handoff", "task_handoff"})


def human_invocation(origin: str, attended: bool | None) -> bool:
    """Only a person's message or click carries a principal.

    Every wake, headless send and unknown future origin lacks a human principal,
    even when it retains a recorded owner whose current membership can be looked up.
    """
    return origin in HUMAN_MESSAGE_ORIGINS and attended is not False


#: Origins nobody is waiting on, when the caller does not say whether anyone is.
UNATTENDED_ORIGINS = frozenset({"routine", "timer", "watch_fired", "watch_report", "job_finished"})


def unattended_turn(origin: str, attended: bool | None) -> bool:
    """Nobody waits on this turn: its caller says so, or a routine or wake started it.

    Decides the unattended tool rules and the unattended run limit alike.
    """
    return attended is False or (attended is None and origin in UNATTENDED_ORIGINS)

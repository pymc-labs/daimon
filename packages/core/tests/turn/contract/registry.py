"""The backends every contract case runs on, and what each one fails today.

A backend joins by adding an entry here. `test_registry.py` fails CI when a turn
profile Daimon can run has no entry.

`pending` names the finding (FINAL-REVIEW.md F1 to F16, plus N1 and N2 found by
this suite) and the PR that fixes it. Each is a strict xfail: the fixing PR turns
the case green and must delete the entry. N1, N2 and the Gemini F15 case have no
owner yet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest

from . import anthropic_fake, gemini_fake, openai_fake, selfhosted_fake
from .drive import Result
from .script import Script


@dataclass(frozen=True)
class Backend:
    name: str
    profiles: tuple[str, ...]
    run: Callable[[pytest.MonkeyPatch, Script], Awaitable[Result]]
    pending: dict[str, str] = field(default_factory=dict)
    """Case name to the finding and PR that make it pass. A strict xfail."""
    not_applicable: dict[str, str] = field(default_factory=dict)
    """Case name to why the backend's wire cannot express it. A skip."""


N1 = (
    "N1: the 2-attempt drop budget is per stream generation and never resets after a "
    "reconnect delivers records (driver.py:1123), so a second drop loses the turn. No owner yet"
)
STALLED = "PR 3a enforced: a give-up logs turn.stalled with its backend (root decision)"
F6 = "F6: no bounded io.interrupt settles the native turn before a give-up (PR 3a enforced)"
M2 = "M2: no backoff between eventless reconnects (PR 3a enforced)"
COPY = (
    "Root decision: the CEILING notice says try again, not ask what it finished "
    "(notices.py:105, PR 3a enforced)"
)
QUIET_IS_A_DROP = (
    "PR 4: a quiet OpenAI stream (read timeout) is a dropped connection "
    "(openai_io.py:84), so it spends the drop budget; with N1 the turn is lost"
)


def _after_pr4(cause: str) -> dict[str, str]:
    """The stuck cases a backend fails because it loses the turn before the cap."""
    reason = f"{cause}, so a stuck turn ends CONNECTION_LOST before its cap (PR 4, then PR 3a)"
    return {
        "stuck_running_ends_ceiling": reason,
        "stuck_running_logs_turn_stalled": reason,
        "stuck_running_reconnects_back_off": reason,
        "give_up_notice_asks_what_it_finished": reason,
    }


BACKENDS: dict[str, Backend] = {
    "anthropic": Backend(
        "anthropic",
        (anthropic_fake.PROFILE,),
        anthropic_fake.run,
        pending={
            "two_drops_with_progress_between": N1,
            "stuck_running_logs_turn_stalled": STALLED,
            "stuck_running_settles_the_native_turn": F6,
            "stuck_running_reconnects_back_off": M2,
            "give_up_notice_asks_what_it_finished": COPY,
        },
    ),
    "openai": Backend(
        "openai",
        (openai_fake.PROFILE,),
        openai_fake.run,
        pending={
            "same_second_burst": QUIET_IS_A_DROP,
            "resume_across_many_connections": QUIET_IS_A_DROP,
            "two_clean_eofs_in_a_row": (
                "PR 4: a clean EOF raises TurnConnectionLost (openai_io.py:163-167); the "
                "second one in a row loses the turn"
            ),
            "two_drops_with_progress_between": N1,
            "stuck_running_settles_the_native_turn": F6,
            **_after_pr4("a clean EOF on a running OpenAI turn is a dropped connection"),
        },
    ),
    "gemini": Backend(
        "gemini",
        (gemini_fake.PROFILE,),
        gemini_fake.run,
        pending={
            "clock_skew_reads_behind": (
                "PR 2: a read stamped before the create is discarded by the time "
                "watermark (gemini core.py:389), so the turn never sees its end"
            ),
            "two_clean_eofs_in_a_row": (
                "PR 4: a clean SSE EOF raises TurnConnectionLost (gemini.py _LazyStream); "
                "the second one in a row loses the turn"
            ),
            "status_always_failing_gives_up_as_lost": (
                "F15: Gemini retries failing reads inside its IO until the ceiling, so the "
                "turn ends CEILING, not CONNECTION_LOST. No owner yet"
            ),
            **_after_pr4("a clean EOF on a running Gemini turn is a dropped connection"),
        },
    ),
    "selfhosted": Backend(
        "selfhosted",
        (selfhosted_fake.PROFILE,),
        selfhosted_fake.run,
        pending={
            "stuck_running_logs_turn_stalled": STALLED,
            "stuck_running_settles_the_native_turn": F6,
            "give_up_notice_asks_what_it_finished": COPY,
            "status_always_failing_gives_up_as_lost": (
                "N2: a retryable harness 503 on every read ends UPSTREAM (try again), not "
                "CONNECTION_LOST (F2(b) remainder, selfhosted transport.py:71). No owner yet"
            ),
        },
        not_applicable={
            "clean_eof_mid_turn": "a polled harness journal has no stream to close",
            "two_clean_eofs_in_a_row": "a polled harness journal has no stream to close",
            "stuck_running_reconnects_back_off": (
                "the harness poll backs off 0.1 to 2 s by itself; there are no reconnects"
            ),
        },
    ),
}

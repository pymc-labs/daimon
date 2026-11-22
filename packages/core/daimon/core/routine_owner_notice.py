"""What a routine's owner is told in a direct message when a run fails. Pure, no I/O.

The routine's agent writes the message from fixed facts only: the routine, why the run
ended in plain words, when, whether its channel was told, the next run and how to pause.
Never the raw error, which can quote a provider message or a file name. When the agent's
turn cannot run, `owner_notice_card` says the same facts plainly.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime

from daimon.core.cron import InvalidScheduleError, validated_next_slot
from daimon.core.cron_words import routine_phrase
from daimon.core.ops_catalog import reason_label
from daimon.core.stores.domain import RoutineRow
from daimon.core.turn.termination import TerminationReason

_PAUSE = "Pause it from /routines."


def _when(at: datetime) -> str:
    """Discord shows this markup in the reader's own time zone."""
    return f"<t:{int(at.timestamp())}:f>"


@dataclass(frozen=True)
class OwnerNoticeFacts:
    """Everything the owner is told; nothing else reaches the agent."""

    routine: str
    agent: str
    what_failed: str
    failed_at: str
    channel_told: bool
    next_run: str | None
    pause: str
    link: str | None


def owner_notice_facts(
    row: RoutineRow,
    reason: TerminationReason,
    *,
    agent_name: str,
    channel_told: bool,
    now: datetime,
    link: str | None,
) -> OwnerNoticeFacts:
    try:
        next_run: str | None = _when(validated_next_slot(row.cron_expr, row.timezone, now))
    except InvalidScheduleError:
        next_run = None
    return OwnerNoticeFacts(
        routine=row.name or routine_phrase(row.cron_expr, row.timezone),
        agent=agent_name,
        what_failed=reason_label(reason.value) or "Unknown",
        failed_at=_when(now),
        channel_told=channel_told,
        next_run=next_run,
        pause=_PAUSE,
        link=link,
    )


def owner_notice_prompt(facts: OwnerNoticeFacts) -> str:
    """The notice turn's whole prompt: host facts, then what to write."""
    return (
        f"<routine_failure>{json.dumps(asdict(facts))}</routine_failure>\n"
        "A run of one of your routines failed. Write the direct message its owner reads now: "
        "which routine, what went wrong, when, whether its channel was told, when it runs "
        "next and how to pause it. Use only the facts above; look nothing up and call no "
        "tools. Copy <t:...> times and the link as they are. At most 600 characters; your "
        "reply is sent as it is."
    )


def owner_notice_card(facts: OwnerNoticeFacts) -> tuple[str, str]:
    """The plain error card sent when the agent's turn cannot run: title and body."""
    told = " Its channel was told." if facts.channel_told else ""
    next_run = f"It runs again {facts.next_run}. " if facts.next_run else ""
    lines = (
        f"It stopped {facts.failed_at}: {facts.what_failed.lower()}.{told}",
        f"{next_run}{facts.pause}",
        facts.link or "",
    )
    return f'Your routine "{facts.routine}" failed', "\n".join(line for line in lines if line)

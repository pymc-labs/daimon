"""Plain words for a turn that stopped or was refused, shared by all adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.errors import TurnError
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.core.turn.termination import TerminationReason

__all__ = [
    "RefusalNouns",
    "TerminationNotice",
    "admission_refusal_text",
    "fit_notice",
    "is_stuck_session",
    "render_termination_notice",
    "short_ref",
]

_IN_FLIGHT_SHOWN = 5
_SERVERS_SHOWN = 3
_NAME_MAX = 60
_STUCK_SESSION_MARK = "waiting on responses to events"


def short_ref(request_id: str) -> str:
    """The last six characters of a request id, for a person to quote."""
    return request_id[-6:].upper()


def is_stuck_session(reason: TerminationReason, error: BaseException | None) -> bool:
    """Whether MA refused a session still waiting on old confirmations."""
    if reason is not TerminationReason.UPSTREAM or not isinstance(error, TurnError):
        return False
    cause = error.cause
    return isinstance(cause, anthropic.BadRequestError) and _STUCK_SESSION_MARK in str(cause)


@dataclass(frozen=True, slots=True)
class _Copy:
    title: str
    next_step: str
    dm_next_step: str | None = None


_RETRY = "@mention Daimon to try again."
_CARRY = "@mention Daimon to carry on."
_COPY: dict[TerminationReason, _Copy] = {
    TerminationReason.INTERRUPTED: _Copy(
        "Stopped. Changes already made stay made.", _CARRY, "Send a message to carry on."
    ),
    TerminationReason.INTERRUPT_TIMEOUT: _Copy(
        "Stopping, but not confirmed. It may still be finishing.",
        "Wait a minute before you @mention Daimon again.",
        "Wait a minute before you send a message again.",
    ),
    TerminationReason.CONNECTION_LOST: _Copy(
        "Lost the connection. Daimon may still be working.",
        "@mention Daimon to ask what it finished.",
        "Send a message to ask what it finished.",
    ),
    TerminationReason.UPSTREAM: _Copy(
        "Daimon's AI service failed on this reply.", _RETRY, "Send your message again."
    ),
    TerminationReason.RATE_LIMITED: _Copy("Daimon's AI service is busy.", "Try again in a minute."),
    TerminationReason.SESSION_TERMINATED: _Copy(
        "Daimon stopped mid-reply. Its working files may be gone.",
        _CARRY,
        "Send a message to carry on.",
    ),
    TerminationReason.MCP_DEGRADED_EMPTY: _Copy(
        "The connection to {servers} failed, so there's no reply.",
        "Ask to reconnect it, or ask again without it.",
    ),
    TerminationReason.RETRYING_UNSETTLED: _Copy(
        "The AI kept failing, so there's no reply.",
        "Wait a moment, then @mention Daimon again.",
        "Wait a moment, then send your message again.",
    ),
    TerminationReason.REQUIRES_ACTION: _Copy(
        "Daimon couldn't get the approval it needed.", _RETRY, "Send your message again."
    ),
    TerminationReason.CEILING: _Copy(
        "This took too long, so Daimon stopped waiting.", _RETRY, "Send your message again."
    ),
    TerminationReason.RECOVERY_CANCELLED: _Copy(
        "Stopped. Daimon lost its working files.", _CARRY, "Send a message to carry on."
    ),
    TerminationReason.RECOVERY_FAILED: _Copy(
        "Daimon lost its working files and couldn't continue.",
        _RETRY,
        "Send your message again.",
    ),
    TerminationReason.ADMISSION_CONCURRENCY_SHED: _Copy(
        "Daimon is too busy right now.",
        "@mention Daimon again in a moment.",
        "Send your message again in a moment.",
    ),
    TerminationReason.ADMISSION_BALANCE_DEPLETED: _Copy(
        "Your team's Daimon credit has run out.", "Ask an admin to top up."
    ),
    TerminationReason.ADMISSION_CAP_EXCEEDED: _Copy(
        "You've used your monthly limit.", "Ask the team running Daimon to raise it."
    ),
    TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED: _Copy(
        "This channel's budget is used up.", "An admin can raise it."
    ),
    TerminationReason.ADMISSION_CHANNEL_PROTECTED: _Copy(
        "Daimon can't reply in this channel.", "Ask somewhere else, or ask an admin."
    ),
    TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE: _Copy(
        "{agent} only works in other channels.", "Ask it there."
    ),
    TerminationReason.ADMISSION_CHANNEL_ISOLATED: _Copy(
        "This channel only uses its own agents.", "Ask an admin to pick one."
    ),
    TerminationReason.ADMISSION_DENIED: _Copy(
        "Daimon couldn't accept this request.", "Ask an admin."
    ),
    TerminationReason.MISSING_CONFIG: _Copy(
        "Daimon isn't set up in this channel yet.", "Ask an admin to run {setup}."
    ),
    TerminationReason.RESOLVER_MISS: _Copy(
        "This channel's setup is out of date.", "Ask an admin to check {setup}."
    ),
    TerminationReason.SESSION_PREPARATION_FAILED: _Copy(
        "A settings change hasn't applied yet.",
        "@mention Daimon again in a little while.",
        "Send your message again in a little while.",
    ),
    TerminationReason.SESSION_BUSY: _Copy(
        "Daimon is still working in this conversation.",
        "Wait for it to finish, then @mention Daimon.",
        "Wait for it to finish, then send a message.",
    ),
    TerminationReason.SESSION_AGENT_MISMATCH: _Copy(
        "{agent} can't continue this conversation.",
        "Start a new conversation to talk to it.",
    ),
}
_FALLBACK = _Copy("Something went wrong.", _RETRY, "Send your message again.")


@dataclass(frozen=True, slots=True)
class RefusalNouns:
    """Platform nouns and command spelling for a refusal."""

    scope: str
    admin: str
    billing: str
    setup: str = "`/agent-setup`"


_REFUSALS: dict[AdmissionDenialReason, _Copy] = {
    "balance_depleted": _Copy("Your team's Daimon credit has run out.", "Ask an admin to top up."),
    "cap_exceeded": _Copy(
        "You've used your monthly limit.", "Ask the team running Daimon to raise it."
    ),
    "channel_budget_exceeded": _Copy("This channel's budget is used up.", "An admin can raise it."),
    "invoker_not_allowed": _Copy(
        "Daimon can't accept your request here.", "Ask an admin to add you."
    ),
    "runs_elsewhere": _COPY[TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE],
    "own_agents_only": _COPY[TerminationReason.ADMISSION_CHANNEL_ISOLATED],
    "writers_none": _COPY[TerminationReason.ADMISSION_CHANNEL_PROTECTED],
    "external_participant": _Copy(
        "Daimon can't accept this request from another organisation.",
        "Ask in a channel that uses its own agents.",
    ),
}
_DM_REFUSALS: dict[AdmissionDenialReason, _Copy] = {
    "runs_elsewhere": _Copy("{agent} only works in other channels.", "Ask it there."),
    "own_agents_only": _Copy("This channel only uses its own agents.", "Ask in that channel."),
    "writers_none": _Copy(
        "Daimon can't reply in this channel.", "Ask somewhere else, or ask an admin."
    ),
    "external_participant": _Copy(
        "Daimon can't move this conversation to a DM.",
        "Ask in a channel that uses its own agents.",
    ),
}


def admission_refusal_text(
    reason: AdmissionDenialReason,
    nouns: RefusalNouns,
    *,
    bot_name: str | None = None,
    agent_name: str | None = None,
    in_dm: bool = False,
) -> str:
    """Two approved lines for an admission refusal.

    `bot_name` remains accepted for adapter callers; a pinned agent's name
    must come from `agent_name`, not the deployment bot's display name.
    """
    copy = (_DM_REFUSALS.get(reason) if in_dm else None) or _REFUSALS[reason]
    agent = agent_name or "This agent"
    return "\n\n".join(
        (
            copy.title.format(agent=agent, setup=nouns.setup),
            copy.next_step.format(agent=agent, setup=nouns.setup),
        )
    )


@dataclass(frozen=True, slots=True)
class TerminationNotice:
    """The two main lines and optional small work and reference lines."""

    reason: TerminationReason
    title: str
    next_step: str
    in_flight: tuple[str, ...] = ()
    finished_tools: int = 0
    request_id: str | None = None

    @property
    def headline(self) -> str:
        return self.title

    def work_line(self, quote: Callable[[str], str] = str) -> str | None:
        parts: list[str] = []
        if self.in_flight:
            parts.append(
                "Still running when it ended: "
                + _listed(self.in_flight, shown=_IN_FLIGHT_SHOWN, quote=quote)
                + "."
            )
        if self.finished_tools:
            noun = "tool call" if self.finished_tools == 1 else "tool calls"
            parts.append(f"Finished before that: {self.finished_tools} {noun}.")
        return " ".join(parts) or None

    @property
    def ref_line(self) -> str | None:
        return f"Ref {short_ref(self.request_id)}" if self.request_id else None

    def plain_text(self) -> str:
        lines = [self.title, self.next_step]
        if (work := self.work_line()) is not None:
            lines.append(work)
        if (ref := self.ref_line) is not None:
            lines.append(ref)
        return "\n\n".join(lines)


def render_termination_notice(
    reason: TerminationReason,
    *,
    state: TurnState | None = None,
    request_id: str | None = None,
    error: BaseException | None = None,
    in_dm: bool = False,
    setup_command: str = "`/agent-setup`",
    agent_name: str | None = None,
) -> TerminationNotice | None:
    """Render one reason using the same words on every platform."""
    if reason is TerminationReason.COMPLETED:
        return None
    copy = _COPY.get(reason, _FALLBACK)
    if spend_limit_error(error or (state.error if state is not None else None)) is not None:
        copy = _Copy(
            "Daimon has reached its usage limit.",
            "Ask the team running it to check the limit.",
        )
    elif is_stuck_session(reason, error or (state.error if state is not None else None)):
        copy = _Copy(
            "This conversation is stuck on an earlier request.",
            "Start a new conversation to carry on.",
        )
    title = copy.title.format(
        servers="a tool",
        agent=agent_name or "This agent",
        setup=setup_command,
    )
    next_step = (copy.dm_next_step if in_dm and copy.dm_next_step else copy.next_step).format(
        setup=setup_command
    )
    in_flight: tuple[str, ...] = ()
    finished = 0
    if state is not None:
        tools = [b for b in state.content if isinstance(b, ToolUseBlock)]
        in_flight = tuple(b.name for b in tools if b.status == "pending")
        finished = sum(1 for b in tools if b.status != "pending")
        if copy is _COPY[TerminationReason.MCP_DEGRADED_EMPTY] and state.mcp_failures:
            names = _listed([f.server_name for f in state.mcp_failures], shown=_SERVERS_SHOWN)
            if len(state.mcp_failures) == 1:
                title = copy.title.format(
                    servers=names, agent=agent_name or "This agent", setup=setup_command
                )
            else:
                title = f"The connections to {names} failed, so there's no reply."
        if reason is TerminationReason.RATE_LIMITED and state.rate_limit_until is not None:
            next_step = f"Try again after {_clock(state.rate_limit_until)}."
    return TerminationNotice(
        reason=reason,
        title=title,
        next_step=next_step,
        in_flight=in_flight,
        finished_tools=finished,
        request_id=request_id,
    )


def _clock(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%H:%M UTC")


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _listed(names: Sequence[str], *, shown: int, quote: Callable[[str], str] = str) -> str:
    listed = ", ".join(quote(_clip(name, _NAME_MAX)) for name in names[:shown])
    hidden = len(names) - shown
    return f"{listed} and {hidden} more" if hidden > 0 else listed


def fit_notice(lines: Sequence[str], *, tail: str | None, limit: int) -> str:
    """Join spaced notice blocks, clipping the body while keeping a short ref."""
    body = "\n\n".join(lines)
    if tail is None:
        return _clip(body, limit)
    room = limit - len(tail) - 2
    return f"{_clip(body, room)}\n\n{tail}"

"""Words for a turn that ended early, keyed to its `TerminationReason`.

Pure, no platform types. `render_termination_notice` answers the four things
a person needs when a turn stops short -- what happened, what was still
running, what survived, what to do next -- plus the request id an operator
greps the logs for. Adapters draw the fields in their own markup;
`TerminationNotice.plain_text` is the fallback for a surface that has none.

What survives is stated per reason rather than guessed from the state: the
reason alone decides whether the session was kept (most failures) or retired
(the ceiling marks the mapping dead), and that is the fact the person acts on.

`admission_refusal_text` is the one wording of a turn refused at admission, so
every platform says the same thing about the same reason in its own nouns.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.turn.ceiling import TURN_CEILING_S
from daimon.core.turn.errors import AdmissionDenialReason
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.core.turn.termination import TerminationReason

__all__ = [
    "RefusalNouns",
    "TerminationNotice",
    "admission_refusal_text",
    "fit_notice",
    "render_termination_notice",
]

# Every name in a notice comes from outside (tool and MCP server names), so
# each is clipped and only the first few are listed: the notice has to fit a
# Slack section (3,000 chars) and a Discord embed description (4,096) with room
# to spare, whatever the turn was doing.
_IN_FLIGHT_SHOWN = 5
_SERVERS_SHOWN = 3
_NAME_MAX = 60
_KEPT = "The conversation and its workspace are kept."
_RETRY = "Send your message again."
_SHARE_RID = "If it keeps happening, share the request id with an admin."
_SPEND_LIMIT_MESSAGE = (
    "Daimon has reached its model usage limit for now. The operators have been notified."
)


@dataclass(frozen=True, slots=True)
class _Copy:
    headline: str
    cause: str
    survived: str
    next_step: str


_COPY: dict[TerminationReason, _Copy] = {
    TerminationReason.INTERRUPTED: _Copy(
        "Stopped",
        "You stopped this turn before it finished.",
        "Anything the agent already did stays done. " + _KEPT,
        "Send a new message to carry on.",
    ),
    TerminationReason.INTERRUPT_TIMEOUT: _Copy(
        "Stop not confirmed",
        "You asked to stop, but the agent service never confirmed it, "
        "so the agent may still be finishing its last step.",
        _KEPT,
        "Give it a minute before sending a new message.",
    ),
    TerminationReason.CONNECTION_LOST: _Copy(
        "Connection lost",
        "The connection to the agent dropped and could not be re-established.",
        "The agent may have kept working after the drop. " + _KEPT,
        "Ask what it finished, or send your message again.",
    ),
    TerminationReason.UPSTREAM: _Copy(
        "Agent service error",
        "The agent service returned an error this turn could not continue past.",
        _KEPT,
        f"{_RETRY} {_SHARE_RID}",
    ),
    TerminationReason.RATE_LIMITED: _Copy(
        "Rate limited",
        "The model provider is limiting requests right now.",
        _KEPT,
        "Wait a minute, then send your message again.",
    ),
    TerminationReason.SESSION_TERMINATED: _Copy(
        "Session ended",
        "The agent service ended this conversation's session mid-turn.",
        "Files in the session's workspace may no longer be available.",
        "Send a new message; if the session cannot be reused, a replacement starts "
        "from this conversation.",
    ),
    TerminationReason.MCP_DEGRADED_EMPTY: _Copy(
        "Tool connection failed",
        "A connected tool server failed and the agent had no reply without it.",
        _KEPT,
        "Ask to reconnect the server, or ask again without it.",
    ),
    TerminationReason.RETRYING_UNSETTLED: _Copy(
        "Model unavailable",
        "The model kept failing and the agent service stopped retrying before a reply.",
        _KEPT,
        f"Wait a moment, then send your message again. {_SHARE_RID}",
    ),
    TerminationReason.REQUIRES_ACTION: _Copy(
        "Approval needed",
        "The agent asked for approval to run a tool, which cannot be given here yet.",
        _KEPT,
        "Ask for the result another way, or ask an admin to change that tool's permission.",
    ),
    # "Retired" holds because both ceiling handlers mark the thread's mapping
    # dead; a headless routine has no mapping, but it also has no one to read this.
    TerminationReason.CEILING: _Copy(
        "Turn timed out",
        f"This turn ran past the {int(TURN_CEILING_S // 60)}-minute limit and was abandoned.",
        "This conversation's session was retired so the stuck one is not reused.",
        "Send a new message to start fresh.",
    ),
    TerminationReason.RECOVERY_CANCELLED: _Copy(
        "Stopped after the session was lost",
        "The session was lost, and you stopped the turn before it could be retried.",
        "Files in the lost session's workspace are gone.",
        "Send a new message to continue in a fresh session.",
    ),
    TerminationReason.RECOVERY_FAILED: _Copy(
        "Session could not be replaced",
        "The session was lost, and starting a replacement failed.",
        "Files in the lost session's workspace are gone.",
        f"Send a new message to try again. {_SHARE_RID}",
    ),
    TerminationReason.ADMISSION_CONCURRENCY_SHED: _Copy(
        "Too busy",
        "Too many turns are running right now, so this one did not start.",
        "Nothing was changed.",
        "Send your message again in a moment.",
    ),
    TerminationReason.ADMISSION_BALANCE_DEPLETED: _Copy(
        "Credit depleted",
        "This workspace has no credit left, so the turn did not run.",
        _KEPT,
        "An admin can top up.",
    ),
    TerminationReason.ADMISSION_CAP_EXCEEDED: _Copy(
        "Usage cap reached",
        "You've reached your monthly usage cap, so the turn did not run.",
        _KEPT,
        # The cap is per person and set by the operator, not by an admin command.
        "An operator can raise it.",
    ),
    TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED: _Copy(
        "Channel budget reached",
        "This channel has used its spending budget, so the turn did not run.",
        _KEPT,
        "An admin can raise or clear the channel's budget.",
    ),
    TerminationReason.ADMISSION_CHANNEL_PROTECTED: _Copy(
        "Channel closed to daimon",
        "This channel's rule lets nobody write in it, so the agent can't answer here.",
        _KEPT,
        "Ask somewhere else, or ask an admin about the channel's rule.",
    ),
    TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE: _Copy(
        "Agent runs elsewhere",
        "This agent's rule runs it only in other channels, so the turn did not run.",
        _KEPT,
        "Ask it in one of those channels.",
    ),
    TerminationReason.ADMISSION_CHANNEL_ISOLATED: _Copy(
        "Channel kept to its own agents",
        "This channel is kept to its own agents and the one that would answer isn't one of "
        "them, so the turn did not run.",
        _KEPT,
        "An admin must set the channel's agent.",
    ),
    TerminationReason.ADMISSION_DENIED: _Copy(
        "Not allowed",
        "This turn was refused before it ran.",
        _KEPT,
        "Ask an admin what is allowed here.",
    ),
    TerminationReason.MISSING_CONFIG: _Copy(
        "Not set up",
        "No agent or environment is configured for this channel, so the turn did not run.",
        "Nothing was changed.",
        "Ask an admin to choose an agent for this channel.",
    ),
    TerminationReason.RESOLVER_MISS: _Copy(
        "Agent not found",
        "The configured agent or environment no longer exists, so the turn did not run.",
        "Nothing was changed.",
        "Ask an admin to pick an existing agent.",
    ),
    TerminationReason.SESSION_PREPARATION_FAILED: _Copy(
        "Change not applied",
        "A configuration change could not be applied to this conversation's session.",
        "The existing session was left as it was.",
        "Send your message again in a little while; the change is retried then.",
    ),
    TerminationReason.SESSION_BUSY: _Copy(
        "Still busy",
        "The previous turn in this conversation is still running.",
        _KEPT,
        "Wait for it to finish, then send your message again.",
    ),
    TerminationReason.SESSION_AGENT_MISMATCH: _Copy(
        "Different agent",
        "This conversation's session belongs to a different agent.",
        "The existing session and workspace were kept.",
        "Start a new conversation to talk to this agent.",
    ),
}


@dataclass(frozen=True, slots=True)
class RefusalNouns:
    """How a platform names what an admission refusal mentions."""

    scope: str
    """The tenant's name there: "server", "workspace", "organisation"."""
    admin: str
    """Who administers it, with its article: "a server admin"."""
    billing: str
    """Where an admin tops up, in the platform's markup: "`/billing`"."""


# `{admin}` is capitalised where it starts a sentence. A cap is per person and
# set by the operator, not by an admin command.
_REFUSALS: dict[AdmissionDenialReason, str] = {
    "balance_depleted": (
        "This {scope}'s {bot}credit is depleted. {Admin} can top up with {billing}."
    ),
    "cap_exceeded": "You've reached your monthly usage cap. An operator can raise it.",
    "channel_budget_exceeded": (
        "This channel has used its spending budget. {Admin} can raise or clear it."
    ),
    "invoker_not_allowed": (
        "You aren't on this {scope}'s list of people who can start a turn. {Admin} can add you."
    ),
    "runs_elsewhere": (
        "This agent's rule runs it only in other channels, so it can't answer here."
    ),
    "own_agents_only": (
        "This channel is kept to its own agents and the one that would answer isn't one of "
        "them. {Admin} must set the channel's agent."
    ),
    "writers_none": "This channel's rule lets nobody write in it, so the agent can't answer.",
    # Only Teams marks people from another organisation (shared channels).
    "external_participant": (
        "People from another organisation can use this agent only in a channel kept to its "
        "own agents."
    ),
}
# The place-bound refusals, worded for a conversation moved to or held in a DM.
_DM_REFUSALS: dict[AdmissionDenialReason, str] = {
    "runs_elsewhere": (
        "This channel's agent has a rule running it only in certain channels, "
        "so it can't continue in a DM."
    ),
    "own_agents_only": (
        "This channel is kept to its own agents, so its conversations stay in it and can't "
        "move to a DM."
    ),
    "writers_none": "This channel's rule lets nobody write in it, so it can't move to a DM.",
    "external_participant": "People from another organisation can't move a conversation to a DM.",
}


def admission_refusal_text(
    reason: AdmissionDenialReason,
    nouns: RefusalNouns,
    *,
    bot_name: str | None = None,
    in_dm: bool = False,
) -> str:
    """What a person is told when admission refuses their turn for `reason`.

    `bot_name`, already escaped for the surface, names whose credit ran out.
    `in_dm` words the place-bound refusals for a DM. Whether to post at all
    (nothing goes into a protected channel) stays the caller's call.
    """
    template = (_DM_REFUSALS.get(reason) if in_dm else None) or _REFUSALS[reason]
    return template.format(
        scope=nouns.scope,
        admin=nouns.admin,
        Admin=nouns.admin[:1].upper() + nouns.admin[1:],
        billing=nouns.billing,
        bot=f"{bot_name} " if bot_name else "",
    )


_FALLBACK = _Copy(
    "Something went wrong",
    "The turn ended unexpectedly.",
    _KEPT,
    f"{_RETRY} {_SHARE_RID}",
)


@dataclass(frozen=True, slots=True)
class TerminationNotice:
    """What a person is told when a turn ends early. Plain text in every field."""

    reason: TerminationReason
    headline: str
    """A few words: fits a status footer."""
    cause: str
    survived: str
    next_step: str
    in_flight: tuple[str, ...] = ()
    """Tool names still running when the turn ended, in call order."""
    finished_tools: int = 0
    request_id: str | None = None

    def work_line(self, quote: Callable[[str], str] = str) -> str | None:
        """One sentence on the tool work cut short, or None when there was none.

        `quote` wraps each tool name in the surface's code markup.
        """
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

    def plain_text(self) -> str:
        """Every field on its own line, no markup -- the default any surface can post."""
        lines = [f"{self.headline}: {self.cause}"]
        if (work := self.work_line()) is not None:
            lines.append(work)
        lines.extend([self.survived, self.next_step])
        if self.request_id is not None:
            lines.append(f"Request id: {self.request_id}")
        return "\n".join(lines)


def render_termination_notice(
    reason: TerminationReason,
    *,
    state: TurnState | None = None,
    request_id: str | None = None,
    error: BaseException | None = None,
) -> TerminationNotice | None:
    """The notice for `reason`, or None when the turn completed.

    `state` is optional because refusals end before any state exists; when
    given it supplies the tool work in flight, the servers that failed and
    the rate-limit horizon.
    """
    if reason is TerminationReason.COMPLETED:
        return None
    copy = _COPY.get(reason, _FALLBACK)
    if spend_limit_error(error or (state.error if state is not None else None)) is not None:
        copy = _Copy("Model usage limit reached", _SPEND_LIMIT_MESSAGE, _KEPT, "")
    cause = copy.cause
    next_step = copy.next_step
    in_flight: tuple[str, ...] = ()
    finished = 0
    if state is not None:
        tools = [b for b in state.content if isinstance(b, ToolUseBlock)]
        in_flight = tuple(b.name for b in tools if b.status == "pending")
        finished = sum(1 for b in tools if b.status != "pending")
        if reason is TerminationReason.MCP_DEGRADED_EMPTY and state.mcp_failures:
            failed = [f.server_name for f in state.mcp_failures]
            names = _listed(failed, shown=_SERVERS_SHOWN)
            cause = (
                f"The tool server {names} failed and the agent had no reply without it."
                if len(failed) == 1
                else f"The tool servers {names} failed and the agent had no reply without them."
            )
        if reason is TerminationReason.RATE_LIMITED and state.rate_limit_until is not None:
            next_step = f"Send your message again after {_clock(state.rate_limit_until)}."
    return TerminationNotice(
        reason=reason,
        headline=copy.headline,
        cause=cause,
        survived=copy.survived,
        next_step=next_step,
        in_flight=in_flight,
        finished_tools=finished,
        request_id=request_id,
    )


def _clock(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%H:%M UTC")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _listed(names: Sequence[str], *, shown: int, quote: Callable[[str], str] = str) -> str:
    """The first `shown` names, each clipped, then how many were left out."""
    listed = ", ".join(quote(_clip(name, _NAME_MAX)) for name in names[:shown])
    hidden = len(names) - shown
    return f"{listed} and {hidden} more" if hidden > 0 else listed


def fit_notice(lines: Sequence[str], *, tail: str | None, limit: int) -> str:
    """Join a drawn notice, clipped to `limit` characters with `…`.

    `tail` (the request id line) is always kept whole: it is the one part an
    operator needs back. The bounded fields keep real notices far below every
    platform limit; this is the last guard, not the usual path.
    """
    body = "\n".join(lines)
    if tail is None:
        return _clip(body, limit)
    room = limit - len(tail) - 1
    return f"{_clip(body, room)}\n{tail}"

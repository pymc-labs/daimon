"""Plain words for a turn that stopped or was refused, shared by all adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.errors import TurnError
from daimon.core.github_app_session import GitHubTokenMintUnavailable
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.platform_words import NEUTRAL_SPACE, Place, words_for
from daimon.core.stores.domain import CHAT_PLATFORMS, ChatPlatform
from daimon.core.turn.errors import AdmissionDenialReason, AdmissionDenied, BackendRefusalCause
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from mux.contracts.config import DEFAULT_BACKEND
from mux.profiles import INLINE_REUSE, MANAGED_AGENTS, PERSISTENT_WORKSPACE
from mux.profiles.selfhosted import LOOP

__all__ = [
    "RefusalNouns",
    "TerminationNotice",
    "admission_refusal_text",
    "fit_notice",
    "is_stuck_session",
    "render_termination_notice",
    "resolver_miss_text",
    "short_ref",
]

_IN_FLIGHT_SHOWN = 5
_SERVERS_SHOWN = 3
_CHANNELS_SHOWN = 3
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
# Names who can top up and where: `{billing}` is the platform's billing command, and
# `{Who}` is "An admin", or for a sender from another organisation, who to ask instead.
_TOP_UP = "{Who} can top up with {billing}."

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
    TerminationReason.REFUSED: _Copy(
        "Daimon's AI declined this request.",
        "Rephrase it and @mention Daimon again.",
        "Rephrase it and send it again.",
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
        "Your team's Daimon credit has run out.", _TOP_UP
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
        "Daimon isn't set up in this channel yet.", "Ask {who} to {verb} {setup}."
    ),
    TerminationReason.RESOLVER_MISS: _Copy(
        "This channel's setup is out of date.", "Ask {who} to check {setup}."
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
# A `RESOLVER_MISS` whose missing piece is a named agent: it was deleted.
_AGENT_GONE = _Copy(
    "This channel's agent {agent} no longer exists.", "Ask {who} to pick another in {setup}."
)


# Discord and Slack both render `<#id>` as a link to the channel; Teams has no such syntax.
_CHANNEL_MENTIONS: dict[ChatPlatform, str] = {"discord": "<#{}>", "slack": "<#{}>"}


@dataclass(frozen=True, slots=True)
class RefusalNouns:
    """Platform nouns and command spelling for a refusal."""

    scope: str
    admin: str
    billing: str
    setup: str = "`/agent-setup`"
    #: How a person reaches `setup`: "run" a slash command, "send" a Teams word.
    verb: str = "run"
    #: Who to ask for what needs an admin; for a sender from another organisation, who
    #: acts for them, since Teams opens no private chat with them.
    who: str = "an admin"
    #: How a channel id is written so it reads as a link to the channel ("<#{}>"),
    #: or None where the platform has no such syntax and channels go unnamed.
    channel_mention: str | None = None

    @classmethod
    def for_platform(
        cls, platform: ChatPlatform, *, place: Place = "channel", foreign: bool = False
    ) -> RefusalNouns:
        """The nouns and commands `platform` uses, from the shared table.

        `foreign` marks a sender from another organisation, who has no private chat with Daimon."""
        words = words_for(platform)
        return cls(
            scope=words.space,
            admin=words.admin,
            billing=words.top_up(place=place, foreign=foreign),
            setup=words.phrase("setup", place=place, foreign=foreign) or "an admin",
            verb=words.verb,
            who=words.host_admin if foreign and place != "dm" else "an admin",
            channel_mention=_CHANNEL_MENTIONS.get(platform),
        )

    @classmethod
    def for_caller(cls, platform: str | None, *, place: Place = "channel") -> RefusalNouns:
        """`for_platform` when the caller's platform is a chat platform, else words
        that name no command (a CLI or an MCP client with no chat behind it)."""
        if platform in CHAT_PLATFORMS:
            return cls.for_platform(platform, place=place)
        return cls(
            scope="workspace",
            admin="an admin",
            billing="the billing command",
            setup="the setup panel",
            verb="open",
        )


_REFUSALS: dict[AdmissionDenialReason, _Copy] = {
    "balance_depleted": _COPY[TerminationReason.ADMISSION_BALANCE_DEPLETED],
    "cap_exceeded": _COPY[TerminationReason.ADMISSION_CAP_EXCEEDED],
    "channel_budget_exceeded": _COPY[TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED],
    "invoker_not_allowed": _Copy(
        "Daimon can't accept your request here.", "Ask an admin to add you."
    ),
    "runs_elsewhere": _COPY[TerminationReason.ADMISSION_AGENT_PINNED_ELSEWHERE],
    "own_agents_only": _COPY[TerminationReason.ADMISSION_CHANNEL_ISOLATED],
    "writers_none": _COPY[TerminationReason.ADMISSION_CHANNEL_PROTECTED],
    "client_requester": _Copy("I only answer in the channel we share.", "Mention me there."),
    "backend_unsupported": _Copy(
        "This agent's model provider isn't available here right now.",
        "Ask {who} to check which provider {agent_mid} uses.",
    ),
    # The hold frees itself as running attempts settle; nobody needs to act.
    "provider_hold_cap_reached": _Copy(
        "This model is at its spending limit for the moment.",
        "It's temporary. Try again in a few minutes.",
    ),
    "provider_setup_pending": _Copy(
        "Couldn't set up GPT for this {scope} yet.",
        "Try again shortly.",
    ),
}
# A provider refusal names its real cause and the chat words that fix it.
_BACKEND_REFUSALS: dict[BackendRefusalCause, _Copy] = {
    "channel_pin": _Copy(
        "This channel is pinned to {provider}, which can't run here.",
        'Ask {who} to say "clear this channel\'s pin", so {agent_mid} answers on its own provider.',
    ),
    "provider_not_allowed": _Copy(
        "{Provider} isn't turned on for this {scope}.",
        'Ask {who} to turn it on, or to say "switch {agent_mid} to {default}".',
    ),
    "provider_not_configured": _Copy(
        "{Provider} isn't set up on this Daimon install yet.",
        "Ask whoever runs Daimon to add the {provider} key.",
    ),
    "agent_backend": _Copy(
        "{agent} runs on {provider}, which isn't set up here yet.",
        'Ask {who} to say "switch {agent_mid} to {default}", or to finish setting up {provider}.',
    ),
}
_PROVIDER_NAMES = {
    MANAGED_AGENTS.provider: "Anthropic",
    PERSISTENT_WORKSPACE.provider: "OpenAI",
    INLINE_REUSE.provider: "Gemini",
    LOOP.provider: "a self-hosted model",
}


def provider_name(provider: str | None) -> str:
    """How copy names a model provider: "OpenAI", or "a model provider" when unknown."""
    return _PROVIDER_NAMES.get(provider or "", "a model provider")


# A pinned agent's refusal when its channels can be named.
_RUNS_IN = _Copy("{agent} only works in {channels}.", "Ask it there.")

_DM_REFUSALS: dict[AdmissionDenialReason, _Copy] = {
    "runs_elsewhere": _Copy("{agent} only works in other channels.", "Ask it there."),
    "own_agents_only": _Copy("This channel only uses its own agents.", "Ask in that channel."),
    "writers_none": _Copy(
        "Daimon can't reply in this channel.", "Ask somewhere else, or ask an admin."
    ),
}


def admission_refusal_text(
    reason: AdmissionDenialReason,
    nouns: RefusalNouns,
    *,
    bot_name: str | None = None,
    agent_name: str | None = None,
    in_dm: bool = False,
    cause: BackendRefusalCause | None = None,
    provider: str | None = None,
    channels: Sequence[str] = (),
) -> str:
    """Two approved lines for an admission refusal.

    `bot_name` remains accepted for adapter callers; a pinned agent's name
    must come from `agent_name`, not the deployment bot's display name.
    A `backend_unsupported` refusal with a `cause` names it and its fix; a
    `runs_elsewhere` one names the `channels` the agent runs in where the
    platform can link them.
    """
    copy = (_DM_REFUSALS.get(reason) if in_dm else None) or _REFUSALS[reason]
    if reason == "backend_unsupported" and cause is not None:
        copy = _BACKEND_REFUSALS[cause]
    named_channels = ""
    if reason == "runs_elsewhere" and channels and nouns.channel_mention is not None:
        copy = _RUNS_IN
        named_channels = _listed(
            [nouns.channel_mention.format(channel) for channel in channels],
            shown=_CHANNELS_SHOWN,
        )
    agent = agent_name or "This agent"
    named = provider_name(provider)
    words = {
        "agent": agent,
        "agent_mid": agent_name or "this agent",
        "provider": named,
        "default": provider_name(DEFAULT_BACKEND),
        "Provider": _capitalised(named),
        "scope": nouns.scope,
        "setup": nouns.setup,
        "billing": nouns.billing,
        "verb": nouns.verb,
        "who": nouns.who,
        "Who": _capitalised(nouns.who),
        "channels": named_channels,
    }
    return "\n\n".join((copy.title.format(**words), copy.next_step.format(**words)))


def resolver_miss_text(nouns: RefusalNouns, *, kind: str, name: str | None) -> str:
    """The refusal for a channel whose agent or environment can't be found.

    A missing agent is named, quoted so its name can't render as markup, with
    where to pick another; anything else keeps the out-of-date wording."""
    copy = _AGENT_GONE if kind == "agent" and name else _COPY[TerminationReason.RESOLVER_MISS]
    words = {
        "agent": _quoted(name or ""),
        "who": nouns.who,
        "verb": nouns.verb,
        "setup": nouns.setup,
    }
    return "\n\n".join((copy.title.format(**words), copy.next_step.format(**words)))


def _quoted(name: str) -> str:
    """`name` as inline code, so a chat renders no mention or markup inside it."""
    return f"`{_clip(name.replace('`', ''), _NAME_MAX)}`"


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
    billing_command: str = "`/billing`",
    setup_verb: str = "run",
    admin: str = "an admin",
    agent_name: str | None = None,
    nouns: RefusalNouns | None = None,
) -> TerminationNotice | None:
    """Render one reason using the same words on every platform.

    An admission refusal or a missing agent raised after the turn started is
    worded from the error itself, as at admission; `nouns` (else words built
    from the command arguments) supply the platform's words for it.
    """
    if reason is TerminationReason.COMPLETED:
        return None
    copy = _COPY.get(reason, _FALLBACK)
    failure = error or (state.error if state is not None else None)
    chain = _error_chain(failure)
    nouns = nouns or RefusalNouns(
        scope=NEUTRAL_SPACE,
        admin=admin,
        billing=billing_command,
        setup=setup_command,
        verb=setup_verb,
        who=admin,
    )
    if (specific := _specific_lines(chain, nouns, agent_name=agent_name, in_dm=in_dm)) is not None:
        title, next_step = specific
        return TerminationNotice(
            reason=reason, title=title, next_step=next_step, request_id=request_id
        )
    if any(isinstance(cause, GitHubTokenMintUnavailable) for cause in chain):
        copy = _Copy(
            "GitHub access is temporarily unavailable.",
            "@mention Daimon to try again.",
            "Send your message again.",
        )
    elif spend_limit_error(failure) is not None:
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
        setup=setup_command,
        billing=billing_command,
        verb=setup_verb,
        who=admin,
        Who=_capitalised(admin),
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


def _specific_lines(
    chain: Sequence[BaseException],
    nouns: RefusalNouns,
    *,
    agent_name: str | None,
    in_dm: bool,
) -> tuple[str, str] | None:
    """The admission or missing-agent wording an error in `chain` carries, if any.

    Every denial reason has its own copy (`_REFUSALS`), including those that have no
    `TerminationReason` member of their own and would otherwise read as the generic
    `ADMISSION_DENIED` notice."""
    for cause in chain:
        if isinstance(cause, AdmissionDenied):
            text = admission_refusal_text(
                cause.reason,
                nouns,
                agent_name=agent_name,
                in_dm=in_dm,
                cause=cause.cause,
                provider=cause.provider,
                channels=cause.channels,
            )
        elif isinstance(cause, MAResolverMissError) and cause.kind == "agent":
            text = resolver_miss_text(nouns, kind=cause.kind, name=cause.daimon_tag)
        else:
            continue
        title, _, next_step = text.partition("\n\n")
        return title, next_step
    return None


def _capitalised(text: str) -> str:
    return text[:1].upper() + text[1:]


def _error_chain(error: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    while error is not None and error not in chain:
        chain.append(error)
        next_error = error.cause if isinstance(error, TurnError) else error.__cause__
        error = next_error if isinstance(next_error, BaseException) else None
    return chain


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

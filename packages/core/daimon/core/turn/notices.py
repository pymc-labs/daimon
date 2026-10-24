"""Plain words for a turn that stopped or was refused, shared by all adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
from daimon.core.anthropic_spend import spend_limit_error
from daimon.core.errors import TurnError
from daimon.core.github_app_session import GitHubTokenMintUnavailable
from daimon.core.platform_words import Place, words_for
from daimon.core.stores.domain import CHAT_PLATFORMS, ChatPlatform
from daimon.core.turn.errors import AdmissionDenialReason, BackendRefusalCause
from daimon.core.turn.state import ToolUseBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from mux.contracts.config import DEFAULT_BACKEND
from mux.errors import ProviderError
from mux.profiles import INLINE_REUSE, MANAGED_AGENTS, PERSISTENT_WORKSPACE
from mux.profiles.selfhosted import LOOP

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


# A server that never had credit (a join with no signup credit): nothing "ran out".
_NO_CREDIT_YET = _Copy("Your team has no Daimon credit yet.", "{Who} can add some with {billing}.")

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
    "provider_hold_cap_reached": _Copy(
        "Too many replies are running or still being settled on this model for this account.",
        "Try again in a few minutes.",
    ),
    "provider_setup_pending": _Copy(
        "Couldn't set up GPT for this {scope} yet.",
        "Try again shortly.",
    ),
    "provider_price_unavailable": _Copy(
        "Daimon can't price {model} right now, so it won't run it.",
        "Use another model, or try again later.",
    ),
    "provider_daily_cap_reached": _Copy(
        "Today's {model} budget for this {scope} is used up.",
        "It resets at midnight UTC; use another model until then.",
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
    # The provider is set up; the stored switch consent is stale. Never say the
    # provider is missing (J6g): a fresh preview and Confirm is the fix.
    "re_confirm": _Copy(
        "{agent}'s model-provider switch needs to be confirmed again.",
        "Ask again to preview the switch to {provider}, then Confirm on the new card.",
    ),
}
_PROVIDER_NAMES = {
    MANAGED_AGENTS.provider: "Anthropic",
    PERSISTENT_WORKSPACE.provider: "OpenAI",
    INLINE_REUSE.provider: "Gemini",
    LOOP.provider: "a self-hosted model",
}


def _model_label(model: str | None) -> str:
    from daimon.core.agent_model_names import model_name
    from mux.contracts.ids import ModelRef

    if not model:
        return "model"
    provider = (
        INLINE_REUSE.provider
        if model.startswith(INLINE_REUSE.provider)
        else PERSISTENT_WORKSPACE.provider
    )
    return model_name(ModelRef(provider=provider, id=model))


def provider_name(provider: str | None) -> str:
    """How copy names a model provider: "OpenAI", or "a model provider" when unknown."""
    return _PROVIDER_NAMES.get(provider or "", "a model provider")


def switched_meanwhile_text(agent: str, now_on: str | None, wanted: str | None) -> str:
    """Two switches of one agent at once: the other landed first. The same on every platform."""
    where = f"; it's now on {provider_name(now_on)}" if now_on else ""
    still = provider_name(wanted) if wanted else "that change"
    return f"Someone just switched {agent}{where}. Ask again if you still want {still}."


def default_agent_switch_text(agent: str, wanted: str, *, place: str, me: bool) -> str:
    """A chat switch of the server's default agent, not said to be for everyone."""
    provider = provider_name(wanted)
    return (
        f"Switching {agent} changes it for everyone on this {place}. To try {provider} "
        f"just here, I can make a copy of {'me' if me else agent} on {provider}."
    )


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
) -> str:
    """Two approved lines for an admission refusal.

    `bot_name` remains accepted for adapter callers; a pinned agent's name
    must come from `agent_name`, not the deployment bot's display name.
    A `backend_unsupported` refusal with a `cause` names it and its fix.
    """
    copy = (_DM_REFUSALS.get(reason) if in_dm else None) or _REFUSALS[reason]
    if reason == "backend_unsupported" and cause is not None:
        copy = _BACKEND_REFUSALS[cause]
    elif reason == "balance_depleted" and cause == "no_credit_yet":
        copy = _NO_CREDIT_YET
    agent = agent_name or "This agent"
    named = provider_name(provider)
    # A daily model cap refusal carries the model id where `provider` would be.
    model = (
        _model_label(provider)
        if reason in ("provider_daily_cap_reached", "provider_price_unavailable")
        else named
    )
    words = {
        "model": model,
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
    }
    return "\n\n".join((copy.title.format(**words), copy.next_step.format(**words)))


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
) -> TerminationNotice | None:
    """Render one reason using the same words on every platform."""
    if reason is TerminationReason.COMPLETED:
        return None
    copy = _COPY.get(reason, _FALLBACK)
    failure = error or (state.error if state is not None else None)
    quota = provider_quota_copy(failure)
    if any(isinstance(cause, GitHubTokenMintUnavailable) for cause in _error_chain(failure)):
        copy = _Copy(
            "GitHub access is temporarily unavailable.",
            "@mention Daimon to try again.",
            "Send your message again.",
        )
    elif quota is not None:
        copy = _Copy(*quota)
    elif any(
        isinstance(cause, ProviderError) and cause.native_code == "gemini_no_tools_loop"
        for cause in _error_chain(failure)
    ):
        # The model kept reaching for a tool this agent doesn't have.
        copy = _Copy(
            "Daimon couldn't answer that without tools.",
            "Ask again, a little more directly.",
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
        # A quota's own words already say when to retry, or that waiting won't help.
        if (
            reason is TerminationReason.RATE_LIMITED
            and state.rate_limit_until is not None
            and quota is None
        ):
            next_step = f"Try again after {_clock(state.rate_limit_until)}."
    return TerminationNotice(
        reason=reason,
        title=title,
        next_step=next_step,
        in_flight=in_flight,
        finished_tools=finished,
        request_id=request_id,
    )


def provider_quota_copy(error: BaseException | None) -> tuple[str, str] | None:
    """Plain words for a provider quota refusal anywhere in `error`'s chain, else None.

    A model this deployment's provider plan gives no quota at all, or one that
    is only rate-limited for now. Shared by a turn's notice and a fleet
    worker's failure line, so both name the same cause and fix.
    """
    for cause in _error_chain(error):
        if isinstance(cause, ProviderError) and cause.category == "rate_limited":
            if cause.native_code not in ("quota_zero", "quota_exhausted"):
                continue
            model = _model_words(cause.native_param)
            if cause.native_code == "quota_zero":
                return (
                    f"{_capitalised(model)} isn't available on this server's Google plan.",
                    "Pick another model or ask an admin to upgrade it.",
                )
            return (
                f"Google is rate-limiting {model} right now.",
                "Try again in a minute.",
            )
    return None


def _model_words(model_id: str | None) -> str:
    """ "Gemini 4 Argon" for a known id, else "this model"."""
    if not model_id:
        return "this model"
    from daimon.core.agent_model_names import model_name
    from mux.contracts.ids import ModelRef

    return model_name(ModelRef(provider=INLINE_REUSE.provider, id=model_id))


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

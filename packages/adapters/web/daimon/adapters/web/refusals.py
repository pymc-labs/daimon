"""The cards a web turn ends on when it can't answer."""

from __future__ import annotations

import uuid
from typing import Literal

from daimon.core.errors import UserFacingError
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.domain import WebRefusalCard
from daimon.core.turn.admission import AdmissionDenied, MissingTurnConfigError
from daimon.core.turn.errors import SessionBusyError
from daimon.core.turn.notices import provider_name

# Every way spending is stopped reads the same to the person: ask an admin for credit.
_OUT_OF_CREDIT = frozenset(
    {"balance_depleted", "cap_exceeded", "channel_budget_exceeded", "allowance_exhausted"}
)


def _refusal(
    title: str, body: str, help: str, action: Literal["switch_back_to_claude"] | None = None
) -> WebRefusalCard:
    return WebRefusalCard(
        id=f"c_{uuid.uuid4().hex[:12]}", title=title, body=body, help=help, action=action
    )


_NO_ANSWER = "No answer was given."


def backend_refusal(err: AdmissionDenied, *, agent: str | None, admin: bool) -> WebRefusalCard:
    """The card for a provider refusal: its real cause, and the way back to Claude for an admin."""
    named = provider_name(err.provider)
    who = agent or "This agent"
    ask = "Ask an admin."
    way_back: Literal["switch_back_to_claude"] | None = "switch_back_to_claude" if admin else None
    match err.cause:
        case "channel_pin":
            return _refusal(
                f"This chat is pinned to {named}, which can't run here.",
                _NO_ANSWER,
                "Switch back to Claude." if admin else ask,
                way_back,
            )
        case "provider_not_allowed":
            return _refusal(
                f"{named} isn't turned on here.",
                _NO_ANSWER,
                "Switch back to Claude, or turn it on." if admin else ask,
                way_back,
            )
        case "provider_not_configured":
            return _refusal(
                f"{named} isn't set up here yet.",
                _NO_ANSWER,
                "Switch back to Claude, or add its key." if admin else ask,
                way_back,
            )
        case "agent_backend":
            return _refusal(
                f"{who} runs on {named}, which isn't set up here yet.",
                _NO_ANSWER,
                "Switch back to Claude, or finish setting it up." if admin else ask,
                way_back,
            )
        case "re_confirm":
            return _refusal(
                f"{who}'s model switch needs to be confirmed again.",
                _NO_ANSWER,
                "Ask an admin or an owner to apply it again.",
            )
        case "no_credit_yet":
            return _refusal(
                "Your team has no credit yet.", _NO_ANSWER, "Ask an admin to add credit."
            )
        case "preparation_busy":
            return _refusal(
                f"{who} isn't ready on {named} yet.", _NO_ANSWER, "Try again in a minute."
            )
        case "preparation_failed":
            return _refusal(
                f"{who} couldn't be set up on {named}.",
                _NO_ANSWER,
                "Try again in a moment, then ask an admin.",
            )
        case "channel_row_missing":
            return _refusal(
                f"{named} isn't set up for this chat's pin yet.",
                _NO_ANSWER,
                "Switch back to Claude." if admin else ask,
                way_back,
            )
        case "anthropic_unconfigured":
            return _refusal(f"{who} runs on Anthropic, which isn't set up here.", _NO_ANSWER, ask)
        case None:
            return _refusal(f"{named} can't run right now.", _NO_ANSWER, ask, way_back)


def failed() -> WebRefusalCard:
    return _refusal("Something went wrong.", "No answer was given.", "Try again in a moment.")


def busy() -> WebRefusalCard:
    return _refusal("Too many chats are running.", "No answer was given.", "Try again in a moment.")


def refusal_for(err: Exception, *, agent: str | None = None, admin: bool = False) -> WebRefusalCard:
    """The card for a turn that could not start; `agent` and `admin` word a provider refusal."""
    if isinstance(err, AdmissionDenied):
        if err.reason == "backend_unsupported":
            return backend_refusal(err, agent=agent, admin=admin)
        if err.cause == "no_credit_yet":
            return backend_refusal(err, agent=agent, admin=admin)
        if err.reason in _OUT_OF_CREDIT:
            return _refusal(
                "Your credit has run out.", "No answer was given.", "Ask an admin to add credit."
            )
        if err.reason == "runs_elsewhere":
            return _refusal(
                "This agent only answers in its own channels.",
                "It can't chat here.",
                "Pick another agent, or ask an admin.",
            )
        return _refusal(
            "This agent can't answer you here.", "No answer was given.", "Ask an admin or an owner."
        )
    if isinstance(err, UserFacingError):
        title, _, help = str(err).partition("\n")
        return _refusal(title, "No answer was given.", help.strip() or "Start a new chat.")
    if isinstance(err, MissingTurnConfigError | MAResolverMissError):
        return _refusal(
            "This agent isn't available.", "It may have been removed.", "Start a new chat."
        )
    if isinstance(err, SessionBusyError):
        return _refusal(
            "This chat is still busy.", "No answer was given.", "Try again in a moment."
        )
    return failed()

"""The cards a web turn ends on when it can't answer."""

from __future__ import annotations

import uuid

from daimon.core.errors import UserFacingError
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.domain import WebRefusalCard
from daimon.core.turn.admission import AdmissionDenied, MissingTurnConfigError
from daimon.core.turn.errors import SessionBusyError

# Every way spending is stopped reads the same to the person: ask an admin for credit.
_OUT_OF_CREDIT = frozenset(
    {"balance_depleted", "cap_exceeded", "channel_budget_exceeded", "allowance_exhausted"}
)


def _refusal(title: str, body: str, help: str) -> WebRefusalCard:
    return WebRefusalCard(id=f"c_{uuid.uuid4().hex[:12]}", title=title, body=body, help=help)


def failed() -> WebRefusalCard:
    return _refusal("Something went wrong.", "No answer was given.", "Try again in a moment.")


def busy() -> WebRefusalCard:
    return _refusal("Too many chats are running.", "No answer was given.", "Try again in a moment.")


def refusal_for(err: Exception) -> WebRefusalCard:
    """The card for a turn that could not start."""
    if isinstance(err, AdmissionDenied):
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

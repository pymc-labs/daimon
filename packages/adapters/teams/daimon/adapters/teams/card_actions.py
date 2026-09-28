"""What every panel's card-action handler shares: the clicker, the answers, the error boundary."""

from __future__ import annotations

from collections.abc import Awaitable

import anthropic
import structlog
from daimon.adapters.teams.interactions import Actor, resolve_actor
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.errors import DaimonError
from daimon.core.observability import capture_exception_with_scope
from microsoft_teams.api import (
    AdaptiveCardActionCardResponse,
    AdaptiveCardActionMessageResponse,
    AdaptiveCardInvokeResponse,
    InvokeActivity,
)
from microsoft_teams.cards import (
    ActionSet,
    ActionStyle,
    AdaptiveCard,
    CardElement,
    ExecuteAction,
    TextBlock,
)
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

FAILED = "Sorry, something went wrong. Please try again."
PANEL_ERRORS = (DaimonError, anthropic.APIError, SQLAlchemyError)


async def guarded[T](work: Awaitable[T], failed: T, event: str) -> T:
    """Invoke boundary: log and report a failure, answer with `failed`."""
    try:
        return await work
    except PANEL_ERRORS as exc:
        log.error(event, exc_info=exc)
        capture_exception_with_scope(exc)
        return failed


async def card_actor(runtime: TeamsRuntime, activity: InvokeActivity) -> Actor | None:
    """The verified clicker of a card action or dialog, or None to refuse."""
    return await resolve_actor(
        runtime, conversation=activity.conversation, aad_object_id=activity.from_.aad_object_id
    )


def replace_card(card: AdaptiveCard) -> AdaptiveCardInvokeResponse:
    return AdaptiveCardActionCardResponse(value=card)


def toast(text: str) -> AdaptiveCardInvokeResponse:
    return AdaptiveCardActionMessageResponse(value=text)


def button(
    verb: str, title: str, op: str, *, style: ActionStyle | None = None, **data: str
) -> ExecuteAction:
    """An Action.Execute routed to `verb`'s handler; `op` names the button."""
    return ExecuteAction(
        title=title, verb=verb, data={"action": verb, "op": op} | data, style=style
    )


def heading(text: str) -> TextBlock:
    return TextBlock(text=text, weight="Bolder", size="Medium", wrap=True)


def text_card(title: str, *lines: str, back: ExecuteAction | None = None) -> AdaptiveCard:
    """A titled card of wrapped lines, with an optional way back to the panel."""
    body: list[CardElement] = [heading(title), *(TextBlock(text=line, wrap=True) for line in lines)]
    if back is not None:
        body.append(ActionSet(actions=[back]))
    return AdaptiveCard(body=body, fallback_text=title)

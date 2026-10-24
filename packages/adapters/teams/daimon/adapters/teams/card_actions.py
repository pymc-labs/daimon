"""What every card action and dialog shares: the verified clicker, the answers, the error boundary.

Invokes skip `parse_inbound`, so every handler re-verifies the organisation and
the clicker's Entra id through `card_actor` before acting.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import cast

import anthropic
import structlog
from daimon.adapters.teams.deployment_admins import admin_team_owners, is_deployment_admin
from daimon.adapters.teams.identity import canonical_uuid, foreign_tenant, live_tenant_id
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.errors import DaimonError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.platform_names import remember_user_name
from daimon.core.stores.identity import get_or_create_platform_principal
from microsoft_teams.api import (
    AdaptiveCardActionCardResponse,
    AdaptiveCardActionMessageResponse,
    AdaptiveCardAttachment,
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    CardTaskModuleTaskInfo,
    InvokeActivity,
    MessageActivityInput,
    TaskModuleContinueResponse,
    TaskModuleMessageResponse,
    TaskModuleResponse,
    TaskSubmitInvokeActivity,
    card_attachment,
)
from microsoft_teams.apps import ActivityContext
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
# For handlers that also send from inside the invoke.
SENDING_PANEL_ERRORS = (*PANEL_ERRORS, *TEAMS_SEND_ERRORS)
_EDIT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Actor:
    """A verified clicker in a live tenant."""

    user_id: str
    tenant_id: uuid.UUID
    is_admin: bool
    conversation_id: str


async def card_actor(
    runtime: TeamsRuntime, activity: InvokeActivity, *, remember_name: bool = True
) -> Actor | None:
    """The clicker, or None when the org, the id or the tenant does not check out.

    The conversation must be the configured tenant's. Teams opens no 1:1 chat
    across organisations, so a 1:1 click naming a foreign tenant is refused.
    """
    teams = runtime.settings.teams
    conversation = activity.conversation
    if teams is None or canonical_uuid(conversation.tenant_id) != teams.tenant_id:
        return None
    user_id = canonical_uuid(activity.from_.aad_object_id)
    if user_id is None:
        return None
    home = foreign_tenant(activity.from_, activity.channel_data, ours=teams.tenant_id)
    if home is not None and conversation.conversation_type == "personal":
        return None
    tenant_id = await live_tenant_id(runtime.sessionmaker, teams.tenant_id)
    if tenant_id is None:
        return None
    # For the billing card's names; in the background, never failing the click.
    if remember_name:
        remember_user_name(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="teams",
            user_id=user_id,
            display_name=activity.from_.name,
        )
    return Actor(
        user_id=user_id,
        tenant_id=tenant_id,
        is_admin=await is_deployment_admin(teams, admin_team_owners(runtime), user_id=user_id),
        conversation_id=conversation.id,
    )


async def get_or_create_account(runtime: TeamsRuntime, actor: Actor) -> uuid.UUID:
    async with runtime.sessionmaker.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=actor.tenant_id, platform="teams", external_id=actor.user_id
        )
    return principal.account_id


async def guarded[T](
    work: Awaitable[T],
    failed: T,
    event: str,
    *,
    errors: tuple[type[Exception], ...] = PANEL_ERRORS,
) -> T:
    """Invoke boundary: log and report a failure, answer with `failed`."""
    try:
        return await work
    except errors as exc:
        log.error(event, exc_info=exc)
        capture_exception_with_scope(exc)
        return failed


async def edit_origin_card(
    ctx: ActivityContext[TaskSubmitInvokeActivity] | ActivityContext[AdaptiveCardInvokeActivity],
    card: AdaptiveCard,
) -> None:
    """Best effort: show `card` in place of the one the dialog was opened from.

    In a channel that is a command panel only its clicker sees, edited as one;
    a 404 says it expired after Teams' 24 hours, so the panel stays closed.
    """
    activity = ctx.activity
    edit = MessageActivityInput(id=activity.reply_to_id).add_card(card)
    if activity.conversation.conversation_type in ("channel", "groupChat"):
        edit.with_recipient(activity.from_, is_targeted=True)
    with contextlib.suppress(*TEAMS_SEND_ERRORS):
        await asyncio.wait_for(ctx.send(edit), _EDIT_TIMEOUT_S)


def submitted_fields(data: object) -> Mapping[str, object]:
    """A card or dialog payload as a mapping; anything else reads as empty."""
    return cast(Mapping[str, object], data) if isinstance(data, Mapping) else {}


def replace_card(card: AdaptiveCard) -> AdaptiveCardInvokeResponse:
    return AdaptiveCardActionCardResponse(value=card)


def toast(text: str) -> AdaptiveCardInvokeResponse:
    return AdaptiveCardActionMessageResponse(value=text)


def dialog(title: str, card: AdaptiveCard) -> TaskModuleResponse:
    info = CardTaskModuleTaskInfo(
        title=title, card=card_attachment(AdaptiveCardAttachment(content=card))
    )
    return TaskModuleResponse(task=TaskModuleContinueResponse(value=info))


def dialog_message(text: str) -> TaskModuleResponse:
    """A dialog's closing note: the answer to its submit (`task/submit`), not its opening."""
    return TaskModuleResponse(task=TaskModuleMessageResponse(value=text))


def dialog_notice(title: str, text: str) -> TaskModuleResponse:
    """A one-line dialog: the answer to a click that opens one (`task/fetch`).

    Teams shows "Unable to reach app" for a `dialog_message` there, though the
    click's work was done.
    """
    card = AdaptiveCard(body=[TextBlock(text=text, wrap=True)], fallback_text=text)
    return dialog(title, card)


def button(
    verb: str, title: str, op: str, *, style: ActionStyle | None = None, **data: str | int
) -> ExecuteAction:
    """An Action.Execute routed to `verb`'s handler; `op` names the button."""
    return ExecuteAction(
        title=title, verb=verb, data={"action": verb, "op": op} | data, style=style
    )


def clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, marked with an ellipsis when cut."""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def heading(text: str) -> TextBlock:
    return TextBlock(text=text, weight="Bolder", size="Medium", wrap=True)


def error_text(text: str) -> TextBlock:
    """The retry notice atop a form."""
    return TextBlock(text=text, color="Attention", wrap=True)


def text_lines(*lines: str) -> list[CardElement]:
    return [TextBlock(text=line, wrap=True) for line in lines]


def text_card(title: str, *lines: str, back: ExecuteAction | None = None) -> AdaptiveCard:
    """A titled card of wrapped lines, with an optional way back to the panel."""
    body: list[CardElement] = [heading(title), *text_lines(*lines)]
    if back is not None:
        body.append(ActionSet(actions=[back]))
    return AdaptiveCard(body=body, fallback_text=title)

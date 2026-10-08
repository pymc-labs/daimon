"""What every card action and dialog shares: the verified clicker, the answers, the error boundary.

Invokes skip `parse_inbound`, so every handler re-verifies the organisation and
the clicker's Entra id through `card_actor` before acting. A clicker from
another organisation (`externals`) is refused unless the handler opts in.
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
from daimon.adapters.teams.identity import canonical_uuid, foreign_tenant, live_tenant_id
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.errors import DaimonError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.platform_names import remember_user_name
from daimon.core.stores.accounts import get_external
from daimon.core.stores.identity import (
    find_platform_principal,
    get_or_create_platform_principal,
)
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    # Answered as from another organisation, never an admin; known on positive evidence.
    is_external: bool = False
    is_external_known: bool = False
    home_tenant_id: str | None = None


async def card_actor(
    runtime: TeamsRuntime, activity: InvokeActivity, *, allow_external: bool = False
) -> Actor | None:
    """The clicker, or None when the org, the id or the tenant does not check out.

    The conversation must be the configured tenant's. A clicker from another
    organisation (or an unlisted guest) is classified as a message sender is,
    and gets None unless `allow_external`; in a 1:1 chat a foreign tenant is
    refused outright.
    """
    teams = runtime.settings.teams
    conversation = activity.conversation
    if teams is None or canonical_uuid(conversation.tenant_id) != teams.tenant_id:
        return None
    user_id = canonical_uuid(activity.from_.aad_object_id)
    if user_id is None:
        return None
    channel_data = activity.channel_data
    home = foreign_tenant(activity.from_, channel_data, ours=teams.tenant_id)
    is_channel = conversation.conversation_type == "channel"
    if home is not None and not is_channel:
        return None
    tenant_id = await live_tenant_id(runtime.sessionmaker, teams.tenant_id)
    if tenant_id is None:
        return None
    # For the billing card's names; in the background, never failing the click.
    remember_user_name(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="teams",
        user_id=user_id,
        display_name=activity.from_.name,
    )
    is_external = is_known = home is not None
    if runtime.externals is not None:
        channel = channel_data.channel if channel_data is not None else None
        team = channel_data.team if channel_data is not None else None
        membership = await runtime.externals.classify(
            foreign_tenant=home,
            kind="channel" if is_channel else "dm",
            conversation_id=conversation.id.split(";", 1)[0],
            user_id=user_id,
            team_id=team.id if team is not None else None,
            team_group_id=canonical_uuid(team.aad_group_id) if team is not None else None,
            channel_type=channel.type if channel is not None else None,
        )
        is_external, is_known = membership.is_external, membership.is_known
        home = membership.home_tenant_id
        if not is_known and not is_external:
            # No evidence either way: what was last known about them stands.
            is_external = await stored_external(runtime.sessionmaker, tenant_id, user_id)
    if is_external and not allow_external:
        return None
    return Actor(
        user_id=user_id,
        tenant_id=tenant_id,
        is_admin=not is_external and user_id in teams.admin_user_ids,
        conversation_id=conversation.id,
        is_external=is_external,
        is_external_known=is_known,
        home_tenant_id=home,
    )


async def stored_external(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, user_id: str
) -> bool:
    """Whether the last evidence about this Teams user placed them in another organisation."""
    async with sessionmaker() as session:
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="teams", external_id=user_id
        )
        return principal is not None and await get_external(session, principal.account_id)


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
    """Best effort: show `card` in place of the one the dialog was opened from."""
    edit = MessageActivityInput(id=ctx.activity.reply_to_id).add_card(card)
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
    return TaskModuleResponse(task=TaskModuleMessageResponse(value=text))


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

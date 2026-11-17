"""The `support` command: ask a person, paid for from the person's support limit.

Discord asks with a reaction on an answer; here `support` posts a form in the
1:1 chat, answered there like every command, and sending it spends from the
limit (`daimon.core.support`). With no limit, a request spends one of
`credits_per_user`; a limit with a menu first offers its options, then the
picked option's form, on a card the chat replaces (a dialog's `continue` for
Ask a human). An admin of the deployment asks for free. It is a bare word, so
prose such as "support vector machines" stays a turn. As on Discord, the row
is committed before the post and stamped delivered only once the post lands,
so a failed post loses nothing. Delivery runs in the background once the row
is committed: a Teams invoke must be answered within seconds, and the post
can take longer, so a send waits on it only briefly: a post that has failed by
then is told as undelivered, any other reads as recorded. `enabled` decides whether
the command is registered.

A request asked from a channel is open to the people who could have asked the
agent there (`answer_access`), as on Slack, decided when the form is shown and
again on Send. This bot posts the request in the workspace's request channel
(`support_routing.support_destination`); `daimon.core.support_routing` has no
DM tier, so no one is messaged directly. A request whose charge is still to be
set carries the charge and Set charge / Decline (`support_settle`). A channel
read only from inside is marked in the post, so whoever picks it up answers
there, and the form warns that the note leaves it.

When it is, every answer also gets an Ask a human button (`card.ASK_HUMAN_DIALOG`),
Slack's: it opens the same form in a dialog only the clicker sees, for the
people who could have asked the agent there (`answer_access`), and the request
links to the message the button is on, the card just below the answer.
One person asks once per answer and option: a second Send is told the first
is in hand and spends nothing. Access is decided for the last time under the
ledger and policy locks, in the transaction that spends. The same post path
carries a place's routed 👎 forms (`feedback`), which spend nothing. A long
post is cut line by line, each cut marked, to fit Teams' cap (`post_size`).
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final

import structlog
from daimon.adapters.teams.admin_caller import admin_caller
from daimon.adapters.teams.answer_access import (
    NOT_ALLOWED,
    POLICY_UNREADABLE,
    AnswerAccess,
    AnswerPlace,
    check_answer_access,
    clicker,
    may_start_turn_at,
    refusal_text,
    sealed_at,
)
from daimon.adapters.teams.card import ASK_HUMAN_DIALOG, notice_card
from daimon.adapters.teams.card_actions import (
    FAILED,
    Actor,
    button,
    card_actor,
    dialog,
    dialog_message,
    dialog_notice,
    error_text,
    guarded,
    heading,
    replace_card,
    submitted_fields,
    text_card,
    text_lines,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.identity import DENIED, TeamsInbound
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.output_delivery import Spawn
from daimon.adapters.teams.post_size import fit_lines
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.support_menu_card import (
    MAX_NOTE_CHARS,
    MENU_PROMPT,
    NEXT,
    OPTION_INPUT,
    SEND,
    footer,
    menu_body,
    option_body,
    read_choice,
    request_facts,
    settle_card,
    subtle,
)
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support_escalation import (
    SubmitOutcome,
    asked_options,
    mark_delivered,
    resolve_limit,
    submit_request,
)
from daimon.core.stores.support_rows import LimitView, SupportEscalationRow
from daimon.core.stores.teams_installations import list_teams_installations
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support import (
    CHOOSE_OPTION,
    OPTIONS_CHANGED,
    RequestChoice,
    SupportError,
    SupportOption,
    SupportPlace,
    balance_text,
    ended_text,
    offer_refusal,
    option_value,
    pick_refusal,
    picked_option,
    received_text,
)
from daimon.core.support import refusal_text as limit_refusal
from daimon.core.support_changes import ChannelPick
from daimon.core.support_escalation import (
    ALREADY_REQUESTED,
    ESCALATE,
    OUT_OF_CREDITS,
    RECEIVED,
    RECORDED_UNDELIVERED,
    SEALED_NOTE_HINT,
    SEALED_POST_MARKER,
    UNAVAILABLE,
)
from daimon.core.support_routing import (
    SupportDestination,
    deployment_channel,
    log_undelivered,
    own_channel_allowed,
    support_destination,
)
from daimon.core.teams_bot_framework import SERVICE_URL
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
    TaskFetchInvokeActivity,
    TaskModuleResponse,
    TaskSubmitInvokeActivity,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    CardElement,
    SubmitAction,
    SubmitData,
    TextInput,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ALREADY_REQUESTED",
    "OUT_OF_CREDITS",
    "RECEIVED",
    "SEALED_LINE",
    "VERB",
    "SupportCommand",
    "enabled",
    "requester",
    "support_post",
    "post_card_to_support_channel",
    "NOT_INSTALLED",
    "ChannelNames",
    "channel_pick",
    "post_blocker",
    "post_to_support_channel",
]

log = structlog.get_logger(__name__)

VERB = "support"
NOTE_INPUT = "note"
_DM_LINK = "(sent in the 1:1 chat)"
_DELIVERY_WAIT = 2.0  # seconds a send waits on its post before answering anyway
_MAX_PENDING = 256  # forms whose "asked in" is remembered; older ones name the 1:1 chat
TITLE = "Ask a person"
FORM_TEXT = MENU_PROMPT + "\n{remaining} {request_word} left"
USAGE = "Write a few words first."
NOT_ALLOWED_THERE = "You can't ask the agent in that channel, so you can't ask a person there."
SEALED_HINT = SEALED_NOTE_HINT
SEALED_LINE = f"_{SEALED_POST_MARKER}_"
# ALREADY_REQUESTED, OUT_OF_CREDITS and RECEIVED are the shared core copy
# (`daimon.core.support_escalation`), re-exported for this module's callers.

# This module's own name for the core call, as on Slack.
record_escalation_once = submit_request


def _link_text(link: str) -> str:
    """A channel's long link reads as short words."""
    return f"[Open the message]({link})" if link.startswith("https://") else link


def support_post(head: str, link: str, *lines: str) -> str:
    """`head`, the answer's `link`, then `lines`, a paragraph each: a Teams message
    drops single line breaks."""
    return "\n\n".join(
        fit_lines([line for line in (head, _link_text(link), *lines) if line.strip()])
    )


def requester(user_name: str | None, user_id: str) -> str:
    return f"{user_name or 'Someone'} (Teams user `{user_id}`)"


def closed_post_lines(
    row: SupportEscalationRow, *, user_name: str | None, entra_tenant_id: str
) -> list[str]:
    """A settled request's post lines, rebuilt from the stored row: who asked, its terms and
    status, and the answer's link. Never read back from a card a client sent."""
    head = f"**{ESCALATE} Human support requested** by {requester(user_name, row.platform_user_id)}"
    place = AnswerPlace(
        row.channel_id, row.message_id, row.channel_id.split(";", 1)[0].endswith("@thread.tacv2")
    )
    facts = request_facts(row, menu=row.option is not None, balance=None)
    return [head, *facts, _link_text(place.link(entra_tenant_id))]


def enabled(settings: Settings) -> bool:
    """Whether the command is registered: a request could have a channel to go to.

    `support_escalation` at the answer's place decides each request.
    """
    deployment = deployment_channel(settings.support, "teams")
    return deployment is not None or own_channel_allowed(settings)


def _refusal(access: AnswerAccess) -> str:
    """What someone who may not ask from a channel is told on a typed `support`."""
    return POLICY_UNREADABLE if access.access == "unreadable" else NOT_ALLOWED_THERE


def _dialog_refusal(access: AnswerAccess) -> str:
    return refusal_text(access.access)


@dataclass(frozen=True)
class _Asked:
    """Where a request was made, for its row, its link and its routing.

    `place` is the channel message it was asked from, None for the 1:1 chat;
    `sealed` is decided from the policy at that place, False for the chat.
    """

    user_id: str
    user_name: str | None
    conversation_id: str
    thread_id: str
    message_id: str
    link: str
    place: AnswerPlace | None = None
    sealed: bool = False

    @classmethod
    def of(cls, inbound: TeamsInbound) -> _Asked:
        place = None
        if inbound.kind == "channel":
            place = AnswerPlace(inbound.conversation_id, inbound.activity_id, True)
        return cls(
            inbound.user_id,
            inbound.user_name,
            inbound.conversation_id,
            inbound.thread_id,
            inbound.activity_id,
            _DM_LINK if place is None else place.link(inbound.entra_tenant_id),
            place,
        )

    @property
    def head(self) -> str:
        return (
            f"**{ESCALATE} Human support requested** by {requester(self.user_name, self.user_id)}"
        )

    def lines(self, facts: Sequence[str] = ()) -> list[str]:
        """The post's lines before the note: who, the limit's facts, a link, the marker."""
        sealed = SEALED_LINE if self.sealed else ""
        parts = (self.head, *facts, _link_text(self.link), sealed)
        return [line for line in parts if line.strip()]

    def body(self, note: str, facts: Sequence[str] = ()) -> str:
        """The request as the support team reads it: who, a limit's facts, a link, the note."""
        return support_post(
            "\n\n".join([self.head, *facts]),
            self.link,
            SEALED_LINE if self.sealed else "",
            *note.splitlines(),
        )


@dataclass
class _Delivery:
    """Whether the post landed: None until it is known."""

    landed: bool | None = None


@dataclass(frozen=True)
class _Origin:
    """The answer a request is about, and the place its limit and feature are read at."""

    tenant_id: uuid.UUID
    channel_id: str | None
    thread_id: str
    agent_id: str | None
    ma_session_id: str | None
    budget_channel_id: str | None

    @property
    def support_place(self) -> SupportPlace:
        return SupportPlace.of_thread(
            channel_id=self.channel_id,
            budget_channel_id=self.budget_channel_id,
            agent_id=self.agent_id,
        )

    @property
    def feature_place(self) -> FeaturePlace:
        return FeaturePlace.of(
            self.tenant_id, "teams", self.channel_id, self.thread_id, agent_id=self.agent_id
        )


async def _origin(runtime: TeamsRuntime, tenant_id: uuid.UUID, asked: _Asked) -> _Origin:
    """Read where the answer sits now: its thread's agent and session (decision 9)."""
    async with runtime.sessionmaker() as session:
        row = await get_latest_thread_session(
            session, tenant_id=tenant_id, platform="teams", thread_id=asked.thread_id
        )
    place = asked.place
    return _Origin(
        tenant_id=tenant_id,
        channel_id=place.channel_id if place is not None and place.is_channel else None,
        thread_id=asked.thread_id,
        agent_id=None if row is None else row.ma_agent_id,
        ma_session_id=None if row is None else row.ma_session_id,
        budget_channel_id=None if row is None else row.budget_channel_id,
    )


async def _native_admin(runtime: TeamsRuntime, inbound: TeamsInbound) -> bool:
    """Whether a typed `support` comes from a deployment admin, who asks for free."""
    if inbound.is_foreign:
        return False
    return (await admin_caller(runtime, user_id=inbound.user_id)).is_server_admin


@dataclass(frozen=True)
class _Decided:
    """Who may ask here, on which limit, and whether the note leaves a sealed channel."""

    origin: _Origin
    view: LimitView
    sealed: bool
    repeats: frozenset[str]
    """The options this person already asked with on this message (`asked_options`)."""

    def refusal(self) -> str | None:
        """Why nothing can be asked here, with the balance; None while an option is open."""
        return offer_refusal(self.view, asked=self.repeats)


def form_text(remaining: int | None) -> str:
    """The note form's prompt, with the requests left on the default allowance."""
    if remaining is None:
        return MENU_PROMPT
    return FORM_TEXT.format(
        remaining=remaining, request_word="request" if remaining == 1 else "requests"
    )


def _form_hints(view: LimitView) -> tuple[int | None, str | None]:
    """The default allowance counts requests; a limit shows its balance or the exemption."""
    left = view.available if view.charged_to == "default" else None
    if left is not None:
        return int(max(left, 0)), None
    return None, footer(view)


def _pick(decided: _Decided, data: Mapping[str, object]) -> SupportOption | str:
    """The option a menu sent, or why not: OPTIONS_CHANGED ends the form, the rest re-ask."""
    view = decided.view
    if view.menu is None:
        return OPTIONS_CHANGED
    value = str(data.get(OPTION_INPUT) or "")
    if not value:
        return CHOOSE_OPTION
    option = picked_option(view.spec, value)
    if option is None:
        return OPTIONS_CHANGED
    refused = pick_refusal(view, option, asked=decided.repeats)
    return option if refused is None else refused


def _hints(note: str | None, sealed: bool) -> list[CardElement]:
    return [subtle(text) for text in (note, SEALED_HINT if sealed else None) if text]


def _note(value: str = "") -> TextInput:
    return TextInput(
        id=NOTE_INPUT, is_multiline=True, max_length=MAX_NOTE_CHARS, value=value or None
    )


def form_card(
    token: str, remaining: int | None, *, sealed: bool = False, footer: str | None = None
) -> AdaptiveCard:
    return AdaptiveCard(
        body=[
            heading(TITLE),
            *text_lines(form_text(remaining)),
            *_hints(footer, sealed),
            _note(),
            ActionSet(actions=[button(VERB, SEND, "send", ask=token)]),
        ],
        fallback_text=TITLE,
    )


def menu_card(
    token: str, view: LimitView, *, sealed: bool, error: str | None = None
) -> AdaptiveCard:
    """A limit's menu in the chat; Next replaces it with the picked option's form."""
    pick = button(VERB, NEXT, "pick", ask=token)
    return AdaptiveCard(
        body=[
            heading(TITLE),
            *menu_body(view, sealed=sealed, error=error),
            ActionSet(actions=[pick]),
        ],
        fallback_text=TITLE,
    )


def option_card(
    token: str,
    option: SupportOption,
    *,
    sealed: bool,
    error: str | None = None,
    values: Mapping[str, object] | None = None,
) -> AdaptiveCard:
    send = button(VERB, SEND, "send", ask=token, option=option_value(option))
    return AdaptiveCard(
        body=[
            heading(TITLE),
            *option_body(option, error=error, values=values),
            *_hints(None, sealed),
            ActionSet(actions=[send]),
        ],
        fallback_text=TITLE,
    )


def ask_form(
    message_id: str,
    remaining: int | None,
    *,
    sealed: bool = False,
    note: str = "",
    error: str | None = None,
    footer: str | None = None,
) -> AdaptiveCard:
    """The same form in Ask a human's dialog, which carries the answer it was opened on."""
    body: list[CardElement] = [error_text(error)] if error else []
    body += [*text_lines(form_text(remaining)), *_hints(footer, sealed)]
    body.append(_note(note))
    send = SubmitAction(title=SEND, data=SubmitData(ASK_HUMAN_DIALOG, {"message": message_id}))
    return AdaptiveCard(body=body, actions=[send], fallback_text=TITLE)


def _menu_dialog(
    message_id: str, view: LimitView, *, sealed: bool, error: str | None = None
) -> AdaptiveCard:
    """The menu as a dialog's first card; Next answers with the option's (`continue`)."""
    data = {"message": message_id, "stage": "pick"}
    pick = SubmitAction(title=NEXT, data=SubmitData(ASK_HUMAN_DIALOG, data))
    body = menu_body(view, sealed=sealed, error=error)
    return AdaptiveCard(body=body, actions=[pick], fallback_text=TITLE)


def _option_dialog(
    message_id: str,
    option: SupportOption,
    *,
    sealed: bool,
    error: str | None = None,
    values: Mapping[str, object] | None = None,
) -> AdaptiveCard:
    data = {"message": message_id, "option": option_value(option), "stage": "send"}
    send = SubmitAction(title=SEND, data=SubmitData(ASK_HUMAN_DIALOG, data))
    body = [*option_body(option, error=error, values=values), *_hints(None, sealed)]
    return AdaptiveCard(body=body, actions=[send], fallback_text=TITLE)


def _facts(row: SupportEscalationRow, view: LimitView) -> list[str]:
    """A request's lines for the post: its option, status and balance.

    Whatever the ledger: a default-charged pick still names its option and a
    pending one its price. Only a plain default request, with no menu and
    nothing to settle, posts as before.
    """
    if row.charged_to == "default" and view.menu is None and row.status != "pending":
        return []
    balance = balance_text(view.balance, view.period) if row.charged_to == "limit" else None
    return request_facts(row, menu=view.menu is not None, balance=balance)


def _log_not_recorded(outcome: SubmitOutcome, tenant_id: uuid.UUID) -> None:
    if outcome.status == "duplicate":
        log.info("support.duplicate_request", tenant_id=str(tenant_id))
    elif outcome.status == "out_of_credits":
        charged_to = None if outcome.view is None else outcome.view.charged_to
        log.info("support.out_of_credits", tenant_id=str(tenant_id), charged_to=charged_to)
    else:
        log.info("support.request_not_recorded", tenant_id=str(tenant_id), status=outcome.status)


class SupportCommand:
    """`support` posts the form; its Send records and posts the request.

    Where `support` was typed is held here under a token the form carries and
    honoured only for the person who typed it; after a restart, or for anyone
    else, the request names the 1:1 chat it was sent from.
    """

    def __init__(self, runtime: TeamsRuntime, direct: DirectChats | None, *, spawn: Spawn) -> None:
        self._runtime = runtime
        self._direct = direct
        # Tracked, so a shutdown waits for a request's delivery to finish.
        self._spawn = spawn
        self._asked: OrderedDict[str, _Asked] = OrderedDict()

    async def _offered(self, origin: _Origin) -> bool:
        """Whether `support_escalation` is on where the answer sits."""
        placed = await place_feature(
            self._runtime.sessionmaker,
            self._runtime.settings,
            origin.feature_place,
            "support_escalation",
        )
        return placed.on

    async def _decide(
        self, actor: Actor, asked: _Asked, *, refuse: Callable[[AnswerAccess], str]
    ) -> _Decided | str:
        """Whether `actor` may ask here and on which limit; a str is the refusal.

        Read each time a form is shown; `_escalate` decides again under the locks.
        """
        origin = await _origin(self._runtime, actor.tenant_id, asked)
        if not await self._offered(origin):
            return UNAVAILABLE
        sealed = False
        if asked.place is not None:
            access = await check_answer_access(self._runtime, actor, asked.place)
            if access.access != "allowed":
                return refuse(access)
            sealed = access.sealed
        async with self._runtime.sessionmaker() as session:
            view = await resolve_limit(
                session,
                tenant_id=actor.tenant_id,
                place=origin.support_place,
                platform_user_id=actor.user_id,
                default_allowance=self._runtime.settings.support.credits_per_user,
                exempt=actor.is_platform_admin,
            )
            repeats = await asked_options(
                session,
                tenant_id=actor.tenant_id,
                platform="teams",
                platform_user_id=actor.user_id,
                channel_id=asked.conversation_id,
                message_id=asked.message_id,
            )
        if not view.active:
            return ended_text(view, now=datetime.now(UTC))
        return _Decided(origin, view, sealed, repeats)

    def _remember(self, asked: _Asked) -> str:
        token = secrets.token_urlsafe(16)
        self._asked[token] = asked
        while len(self._asked) > _MAX_PENDING:
            self._asked.popitem(last=False)
        return token

    async def command(self, context: CommandContext) -> None:
        inbound = context.asked_in or context.inbound
        asked = _Asked.of(inbound)
        exempt = await _native_admin(self._runtime, inbound)
        actor = Actor(
            asked.user_id,
            context.tenant_id,
            context.is_admin,
            asked.conversation_id,
            is_platform_admin=exempt,
        )
        decided = await self._decide(actor, asked, refuse=_refusal)
        if isinstance(decided, str):
            await context.send(notice_card(decided))
            return
        asked, view = replace(asked, sealed=decided.sealed), decided.view
        if (refused := decided.refusal()) is not None:
            await context.send(notice_card(refused))
            return
        if view.menu is not None:
            await context.send_card(menu_card(self._remember(asked), view, sealed=asked.sealed))
            return
        remaining, hint = _form_hints(view)
        token = self._remember(asked)
        await context.send_card(form_card(token, remaining, sealed=asked.sealed, footer=hint))

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx.activity), toast(FAILED), "teams.support.failed")

    async def _act(self, activity: AdaptiveCardInvokeActivity) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        data = submitted_fields(activity.value.action.data)
        op, token = str(data.get("op") or ""), str(data.get("ask") or "")
        asked = self._asked.get(token)
        # The card the click came from names the request, so a second Send on it
        # finds the first one, before or after a restart.
        card_id = activity.reply_to_id or activity.id
        if asked is None or asked.user_id != actor.user_id:
            chat = actor.conversation_id
            asked = _Asked(actor.user_id, activity.from_.name, chat, chat, card_id, _DM_LINK)
        else:
            asked = replace(asked, message_id=card_id)
        if op == "pick" or (op == "send" and data.get(OPTION_INPUT)):
            return await self._act_menu(actor, token, asked, data, send=op == "send")
        note = str(data.get(NOTE_INPUT) or "").strip()[:MAX_NOTE_CHARS]
        if not note:
            return toast(USAGE)
        return await self._send(actor, token, asked, RequestChoice.default(note))

    async def _act_menu(
        self,
        actor: Actor,
        token: str,
        asked: _Asked,
        data: Mapping[str, object],
        *,
        send: bool,
    ) -> AdaptiveCardInvokeResponse:
        """The menu's Next, or the option form's Send, in the chat."""
        decided = await self._decide(actor, asked, refuse=_refusal)
        if isinstance(decided, str):
            return replace_card(text_card(TITLE, *decided.splitlines()))
        view, sealed = decided.view, decided.sealed
        picked = _pick(decided, data)
        if picked == OPTIONS_CHANGED:
            return replace_card(text_card(TITLE, OPTIONS_CHANGED))
        if isinstance(picked, str):
            return replace_card(menu_card(token, view, sealed=sealed, error=picked))
        if not send:
            return replace_card(option_card(token, picked, sealed=sealed))
        try:
            choice = read_choice(data, picked)
        except SupportError as err:
            card = option_card(token, picked, sealed=sealed, error=str(err), values=data)
            return replace_card(card)
        return await self._send(actor, token, asked, choice)

    async def _send(
        self, actor: Actor, token: str, asked: _Asked, choice: RequestChoice
    ) -> AdaptiveCardInvokeResponse:
        if (kept := self._asked.get(token)) is not None and kept.user_id == asked.user_id:
            del self._asked[token]
        # Access is decided again in `_escalate`: the policy may have changed while
        # the form sat in the chat.
        reply = await self._escalate(actor, asked, choice, not_allowed=NOT_ALLOWED_THERE)
        return replace_card(text_card(TITLE, *reply.splitlines()))

    async def on_ask_open(
        self, ctx: ActivityContext[TaskFetchInvokeActivity]
    ) -> TaskModuleResponse:
        """Ask a human on an answer: the form, for someone who could have asked there."""
        return await guarded(
            self._ask_open(ctx.activity), dialog_notice(TITLE, FAILED), "teams.support.failed"
        )

    async def on_ask_submit(
        self, ctx: ActivityContext[TaskSubmitInvokeActivity]
    ) -> TaskModuleResponse:
        return await guarded(
            self._ask_submit(ctx.activity), dialog_message(FAILED), "teams.support.failed"
        )

    def _dialog_asked(self, actor: Actor, name: str | None, place: AnswerPlace) -> _Asked:
        teams = self._runtime.settings.teams
        link = place.link(teams.tenant_id) if teams is not None else place.message_id
        conversation = place.conversation_id
        return _Asked(
            actor.user_id, name, conversation, conversation, place.message_id, link, place
        )

    async def _ask_open(self, activity: TaskFetchInvokeActivity) -> TaskModuleResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None or not activity.reply_to_id:
            return dialog_notice(TITLE, DENIED)
        place = AnswerPlace.of(activity, activity.reply_to_id)
        asked = self._dialog_asked(actor, activity.from_.name, place)
        decided = await self._decide(actor, asked, refuse=_dialog_refusal)
        if isinstance(decided, str):
            return dialog_notice(TITLE, decided)
        view, sealed = decided.view, decided.sealed
        if (refused := decided.refusal()) is not None:
            return dialog_notice(TITLE, refused)
        if view.menu is not None:
            return dialog(TITLE, _menu_dialog(place.message_id, view, sealed=sealed))
        remaining, hint = _form_hints(view)
        return dialog(TITLE, ask_form(place.message_id, remaining, sealed=sealed, footer=hint))

    async def _ask_submit(self, activity: TaskSubmitInvokeActivity) -> TaskModuleResponse:
        """Decided again in `_escalate`: the policy may have changed while the dialog was open.

        The answer is the invoke's `replyToId` when Teams sends one, else the
        id the form carries; a forged one only points the submitter's own
        request at another message in the same conversation.
        """
        actor = await card_actor(self._runtime, activity)
        data = submitted_fields(activity.value.data)
        message_id = activity.reply_to_id or str(data.get("message") or "")
        if actor is None or not message_id:
            return dialog_message(DENIED)
        place = AnswerPlace.of(activity, message_id)
        asked = self._dialog_asked(actor, activity.from_.name, place)
        stage = data.get("stage")
        if stage in ("pick", "send"):
            return await self._ask_menu(actor, asked, data, send=stage == "send")
        note = str(data.get(NOTE_INPUT) or "").strip()[:MAX_NOTE_CHARS]
        if not note:
            # Only someone who may ask there gets the form back to fix.
            decided = await self._decide(actor, asked, refuse=_dialog_refusal)
            if isinstance(decided, str):
                return dialog_message(decided)
            remaining, hint = _form_hints(decided.view)
            form = ask_form(message_id, remaining, sealed=decided.sealed, error=USAGE, footer=hint)
            return dialog(TITLE, form)
        choice = RequestChoice.default(note)
        return dialog_message(await self._escalate(actor, asked, choice, not_allowed=NOT_ALLOWED))

    async def _ask_menu(
        self, actor: Actor, asked: _Asked, data: Mapping[str, object], *, send: bool
    ) -> TaskModuleResponse:
        """The dialog's menu Next (answered with the option's card), or its Send."""
        decided = await self._decide(actor, asked, refuse=_dialog_refusal)
        if isinstance(decided, str):
            return dialog_message(decided)
        view, sealed, message_id = decided.view, decided.sealed, asked.message_id
        picked = _pick(decided, data)
        if picked == OPTIONS_CHANGED:
            return dialog_message(OPTIONS_CHANGED)
        if isinstance(picked, str):
            return dialog(TITLE, _menu_dialog(message_id, view, sealed=sealed, error=picked))
        if not send:
            return dialog(TITLE, _option_dialog(message_id, picked, sealed=sealed))
        try:
            choice = read_choice(data, picked)
        except SupportError as err:
            form = _option_dialog(message_id, picked, sealed=sealed, error=str(err), values=data)
            return dialog(TITLE, form)
        return dialog_message(await self._escalate(actor, asked, choice, not_allowed=NOT_ALLOWED))

    async def _escalate(
        self, actor: Actor, asked: _Asked, choice: RequestChoice, *, not_allowed: str
    ) -> str:
        """Record the request once per message and option, start its delivery, and say so.

        A request from a channel is decided under the ledger and policy locks
        (`record_escalation_once`), so the policy it is allowed by is the one
        the credit is spent against; `not_allowed` is the refusal's wording.
        """
        runtime = self._runtime
        tenant_id = actor.tenant_id
        origin = await _origin(runtime, tenant_id, asked)
        destination = None
        if await self._offered(origin):
            async with runtime.sessionmaker() as session:
                destination = await support_destination(
                    session, tenant_id=tenant_id, platform="teams", settings=runtime.settings
                )
        if destination is None:
            return UNAVAILABLE
        refusal, sealed = not_allowed, False
        source_allowed = None
        if asked.place is not None:
            place = asked.place
            # Looked up with no session open: Graph may be slow.
            caller = await clicker(runtime, actor)

            async def allowed_at_place(locked: AsyncSession) -> bool:
                nonlocal refusal, sealed
                try:
                    policy = await load_access_policy(locked, tenant_id=tenant_id)
                except AccessPolicyUnreadable:
                    refusal = POLICY_UNREADABLE
                    return False
                sealed = sealed_at(policy, place)
                return await may_start_turn_at(locked, policy, actor, caller, place)

            source_allowed = allowed_at_place
        try:
            async with runtime.sessionmaker.begin() as session:
                principal = await find_platform_principal(
                    session, tenant_id=tenant_id, platform="teams", external_id=asked.user_id
                )
                outcome = await record_escalation_once(
                    session,
                    tenant_id=tenant_id,
                    account_id=principal.account_id if principal is not None else None,
                    platform="teams",
                    platform_user_id=asked.user_id,
                    channel_id=asked.conversation_id,
                    message_id=asked.message_id,
                    ma_session_id=origin.ma_session_id,
                    place=origin.support_place,
                    choice=choice,
                    default_allowance=runtime.settings.support.credits_per_user,
                    exempt=actor.is_platform_admin,
                    source_allowed=source_allowed,
                )
        except SupportError as err:
            # A missing step or a bad answer: nothing was recorded.
            return str(err)
        if outcome.status == "refused":
            log.info("support.refused", tenant_id=str(tenant_id))
            return refusal
        row, view = outcome.row, outcome.view
        if outcome.status != "recorded" or row is None or view is None:
            _log_not_recorded(outcome, tenant_id)
            return limit_refusal(outcome, now=datetime.now(UTC))
        log.info("support.escalation_recorded", escalation_id=str(row.id))
        asked = replace(asked, sealed=sealed)
        outcome = _Delivery()
        delivery = self._spawn(
            self._deliver(asked, row, view, destination, outcome), name="teams.support.deliver"
        )
        # A post that fails fast is told as undelivered, as on Discord and Slack; a
        # slow one is not waited for past the invoke's budget, and reads as received.
        await asyncio.wait({delivery}, timeout=_DELIVERY_WAIT)
        return RECORDED_UNDELIVERED if outcome.landed is False else received_text(row, view)

    async def _deliver(
        self,
        asked: _Asked,
        row: SupportEscalationRow,
        view: LimitView,
        destination: SupportDestination,
        outcome: _Delivery,
    ) -> None:
        """Best effort: the row is committed, and stays undelivered unless a post lands.

        A request still to be charged posts as a card: its post is where staff
        set it. `outcome` tells `_escalate` whether the post landed, if it is
        still waiting.
        """
        facts = _facts(row, view)
        post_id = None
        if row.status == "pending":
            card = settle_card(row, asked.lines(facts))
            post_id = await post_card_to_support_channel(
                self._runtime, self._direct, destination, card
            )
            delivered = post_id is not None
        else:
            body = asked.body(row.note, facts)
            delivered = await post_to_support_channel(
                self._runtime, self._direct, destination, body
            )
        outcome.landed = delivered
        if delivered:
            channel = destination.channel_id
            try:
                async with self._runtime.sessionmaker.begin() as session:
                    await mark_delivered(
                        session,
                        escalation_id=row.id,
                        support_channel_id=channel if post_id is not None else None,
                        support_post_id=post_id,
                    )
            except (SQLAlchemyError, OSError) as exc:
                # It landed; an unstamped row is never re-posted, so only log it.
                log.warning(
                    "support.delivery_unstamped",
                    escalation_id=str(row.id),
                    err_type=type(exc).__name__,
                )
        else:
            log_undelivered(row)
        log.info("support.escalation_delivered", escalation_id=str(row.id), delivered=delivered)


async def post_to_support_channel(
    runtime: TeamsRuntime, direct: DirectChats | None, destination: SupportDestination, body: str
) -> bool:
    """Post `body` to the request channel; False when it did not land."""

    async def send(chats: DirectChats, channel: str) -> str:
        return await chats.post(channel, body)

    return await _post_in_support_channel(runtime, direct, destination, send) is not None


async def post_card_to_support_channel(
    runtime: TeamsRuntime,
    direct: DirectChats | None,
    destination: SupportDestination,
    card: AdaptiveCard,
) -> str | None:
    """Post `card` to the request channel; its activity id, None when it did not land."""

    async def send(chats: DirectChats, channel: str) -> str:
        return await chats.post_card(channel, MessageActivityInput().add_card(card))

    return await _post_in_support_channel(runtime, direct, destination, send)


async def _post_in_support_channel(
    runtime: TeamsRuntime,
    direct: DirectChats | None,
    destination: SupportDestination,
    send: Callable[[DirectChats, str], Awaitable[str]],
) -> str | None:
    """`send` to the request channel when this bot may post there.

    The channel is asked `authorize(POST)` with no agent, on a policy read
    just before the send, as Slack does, so a channel protected meanwhile is
    refused. No lock is held across the send. This process posts through one
    pinned service URL (`SERVICE_URL`), the one a picked channel stores.
    """
    channel = destination.channel_id
    try:
        if direct is None or not await _may_post_to(runtime, channel):
            return None
        return await send(direct, channel)
    except (SQLAlchemyError, *TEAMS_SEND_ERRORS) as exc:
        log.warning("support.channel_undeliverable", err_type=type(exc).__name__)
        return None


async def _may_post_to(runtime: TeamsRuntime, channel: str) -> bool:
    """Whether the deployment's organisation lets this bot post in `channel`."""
    teams = runtime.settings.teams
    if teams is None:
        return False
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=teams.tenant_id)
    async with runtime.sessionmaker() as session:
        try:
            policy = await load_access_policy(session, tenant_id=tenant_id)
        except AccessPolicyUnreadable:
            log.warning("support.destination_policy_unreadable")
            return False
    place = Place(channel_id=channel)
    if not authorize(policy, subject=Subject(), action=Action.POST, place=place):
        log.warning("support.destination_protected", channel_id=channel)
        return False
    return True


ChannelNames = Callable[[str], Awaitable[Mapping[str, str | None]]]
"""A team's standard channels by id (`http_service.channel_names`); empty when unreadable."""

NOT_INSTALLED: Final = "Add Daimon to this channel's team."


async def channel_pick(
    runtime: TeamsRuntime, channels: ChannelNames, *, tenant_id: uuid.UUID, channel_id: str
) -> ChannelPick | str:
    """`channel_id` as a request channel, with its team and service URL; a str says why not.

    Daimon posts only in a team it is installed in, so the channel is looked
    up in each installed team's channel list.
    """
    async with runtime.sessionmaker() as session:
        installs = await list_teams_installations(session, tenant_id=tenant_id)
    for install in installs:
        if channel_id in await channels(install.team_id):
            return ChannelPick(
                platform="teams",
                channel_id=channel_id,
                teams_team_id=install.team_id,
                teams_service_url=SERVICE_URL,
            )
    return NOT_INSTALLED


async def post_blocker(
    runtime: TeamsRuntime, channels: ChannelNames, *, tenant_id: uuid.UUID, channel_id: str
) -> str | None:
    """Why Daimon can't post requests in `channel_id`, or None when it can."""
    pick = await channel_pick(runtime, channels, tenant_id=tenant_id, channel_id=channel_id)
    return pick if isinstance(pick, str) else None

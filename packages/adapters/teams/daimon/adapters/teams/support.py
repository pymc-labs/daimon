"""The `support` command: ask a person, paid for from the person's support plan.

Discord asks with a reaction on an answer; here `support` posts a form in the
1:1 chat, answered there like every command, and sending it spends from the
plan (`daimon.core.support_plans`). With no plan, a request spends one of
`credits_per_user`; a plan with a menu first offers its options, then the
picked option's form, on a card the chat replaces (a dialog's `continue` for
Ask a human). An admin of the deployment asks for free. It is a bare word, so
prose such as "support vector machines" stays a turn. As on Discord, the row
is committed before the post and stamped delivered only once the post lands,
so a failed post loses nothing. Delivery runs in the background once the row
is committed: a Teams invoke must be answered within seconds, and walking a
tier of admins' chats can take longer, so the person is told the request is
recorded before it lands. `enabled` decides whether the command is registered.

A request asked from a channel is open to the people who could have asked the
agent there (`answer_access`), as on Slack, decided when the form is shown and
again on Send. If that channel has its own admins, the request goes to them in
1:1 chats first, then to the tenant's admins (`daimon.core.support_routing`),
each held to the tenant's DM policy and found on the channel's team roster.
Otherwise, or when no chat landed, this bot posts it in
`DAIMON_SUPPORT__TEAMS_ESCALATION_CHANNEL_ID`, Teams' own channel, as Slack
has its own. A request whose charge is still to be set is always posted
there too, with the charge and Set charge / Decline (`support_settle`). A
channel read only from inside is marked in the post, so whoever picks it up
answers there, and the form warns that the note leaves it.

When it is, every answer also gets an Ask a human button (`card.ASK_HUMAN_DIALOG`),
Slack's: it opens the same form in a dialog only the clicker sees, for the
people who could have asked the agent there (`answer_access`), and the request
links to the message the button is on, the card just below the answer.
One person asks once per answer and option: a second Send is told the first
is in hand and spends nothing. Access is decided for the last time under the
ledger and policy locks, in the transaction that spends. The same post path
carries a tenant's routed 👎 forms (`routes_feedback`, `feedback`), which
spend nothing.
"""

from __future__ import annotations

import secrets
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

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
from daimon.adapters.teams.deployment_admins import live_admin_ids
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.identity import DENIED, TeamsInbound
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.output_delivery import Spawn
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.support_plan_card import (
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
from daimon.adapters.teams.team_owners import team_owner_lookup
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support_escalation import (
    SubmitOutcome,
    mark_delivered,
    resolve_plan,
    submit_request,
)
from daimon.core.stores.support_rows import PlanView, SupportEscalationRow
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support_escalation import (
    ALREADY_REQUESTED,
    ESCALATE,
    OUT_OF_CREDITS,
    RECEIVED,
    SEALED_NOTE_HINT,
    SEALED_POST_MARKER,
    UNAVAILABLE,
)
from daimon.core.support_plans import (
    CHOOSE_OPTION,
    NOT_ENOUGH_CREDITS,
    OPTIONS_CHANGED,
    RequestChoice,
    SupportOption,
    SupportPlace,
    SupportPlanError,
    balance_text,
    cheapest_bounds,
    ended_text,
    out_of_credits_text,
    price_bounds,
    received_text,
)
from daimon.core.support_plans import refusal_text as plan_refusal
from daimon.core.support_routing import support_recipient_tiers
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
    "post_to_support_channel",
    "routes_feedback",
]

log = structlog.get_logger(__name__)

VERB = "support"
NOTE_INPUT = "note"
_DM_LINK = "(sent in the 1:1 chat)"
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
    return "\n\n".join(line for line in (head, _link_text(link), *lines) if line.strip())


def requester(user_name: str | None, user_id: str) -> str:
    return f"{user_name or 'Someone'} (Teams user `{user_id}`)"


def enabled(settings: Settings) -> bool:
    """Whether escalation is on: a Teams channel to post in and credits to spend."""
    support = settings.support
    return bool(support.teams_escalation_channel_id) and support.credits_per_user > 0


def routes_feedback(settings: Settings, tenant_id: uuid.UUID) -> bool:
    """Whether this tenant's submitted 👎 forms also go to the escalation channel.

    Off unless the tenant turned it on and a Teams channel is set; credits play
    no part, since a form spends none.
    """
    support = settings.support
    return bool(support.teams_escalation_channel_id) and support.routes_feedback(tenant_id)


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
        """The post's lines before the note: who, the plan's facts, a link, the marker."""
        sealed = SEALED_LINE if self.sealed else ""
        parts = (self.head, *facts, _link_text(self.link), sealed)
        return [line for line in parts if line.strip()]

    def body(self, note: str, facts: Sequence[str] = ()) -> str:
        """The request as the support team reads it: who, a plan's facts, a link, the note."""
        return support_post(
            "\n\n".join([self.head, *facts]),
            self.link,
            SEALED_LINE if self.sealed else "",
            *note.splitlines(),
        )


@dataclass(frozen=True)
class _Origin:
    """The answer a request is about, and the place its plan and feature are read at."""

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
    """Who may ask here, on which plan, and whether the note leaves a sealed channel."""

    origin: _Origin
    view: PlanView
    sealed: bool


def form_text(remaining: int | None) -> str:
    """The note form's prompt, with the requests left on the default plan."""
    if remaining is None:
        return MENU_PROMPT
    return FORM_TEXT.format(
        remaining=remaining, request_word="request" if remaining == 1 else "requests"
    )


def _form_hints(view: PlanView) -> tuple[int | None, str | None]:
    """The default plan counts requests; any other plan shows its balance or the exemption."""
    left = view.available if view.charged_to == "default" else None
    if left is not None:
        return int(max(left, 0)), None
    return None, footer(view)


def _unaffordable(view: PlanView) -> str | None:
    """Why the note form cannot be sent on this plan, None when it can."""
    if view.affordable(price_bounds(view.spec.options[0].price)):
        return None
    return out_of_credits_text(view.charged_to)


def _pick(view: PlanView, data: Mapping[str, object]) -> SupportOption | str:
    """The option a menu sent, or why not: OPTIONS_CHANGED ends the form, the rest re-ask."""
    if view.catalog is None or str(data.get("revision")) != str(view.revision):
        return OPTIONS_CHANGED
    key = str(data.get(OPTION_INPUT) or "")
    if not key:
        return CHOOSE_OPTION
    option = next((o for o in view.spec.options if o.key == key), None)
    if option is None:
        return OPTIONS_CHANGED
    if not view.affordable(cheapest_bounds(option.price)):
        return NOT_ENOUGH_CREDITS
    return option


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
    token: str, view: PlanView, *, sealed: bool, error: str | None = None
) -> AdaptiveCard:
    """A plan's menu in the chat; Next replaces it with the picked option's form."""
    pick = button(VERB, NEXT, "pick", ask=token, revision=view.revision)
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
    revision: int,
    *,
    sealed: bool,
    error: str | None = None,
    values: Mapping[str, object] | None = None,
) -> AdaptiveCard:
    send = button(VERB, SEND, "send", ask=token, option=option.key, revision=revision)
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
    message_id: str, view: PlanView, *, sealed: bool, error: str | None = None
) -> AdaptiveCard:
    """The menu as a dialog's first card; Next answers with the option's (`continue`)."""
    data = {"message": message_id, "revision": view.revision, "stage": "pick"}
    pick = SubmitAction(title=NEXT, data=SubmitData(ASK_HUMAN_DIALOG, data))
    body = menu_body(view, sealed=sealed, error=error)
    return AdaptiveCard(body=body, actions=[pick], fallback_text=TITLE)


def _option_dialog(
    message_id: str,
    option: SupportOption,
    revision: int,
    *,
    sealed: bool,
    error: str | None = None,
    values: Mapping[str, object] | None = None,
) -> AdaptiveCard:
    data = {"message": message_id, "revision": revision, "option": option.key, "stage": "send"}
    send = SubmitAction(title=SEND, data=SubmitData(ASK_HUMAN_DIALOG, data))
    body = [*option_body(option, error=error, values=values), *_hints(None, sealed)]
    return AdaptiveCard(body=body, actions=[send], fallback_text=TITLE)


def _facts(row: SupportEscalationRow, view: PlanView) -> list[str]:
    """A plan request's lines for the post; the default plan's post is unchanged."""
    if row.charged_to == "default":
        return []
    balance = balance_text(view.balance, view.plan) if row.charged_to == "plan" else None
    return request_facts(row, menu=view.catalog is not None, balance=balance)


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
        """Whether `actor` may ask here and on which plan; a str is the refusal.

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
            view = await resolve_plan(
                session,
                tenant_id=actor.tenant_id,
                place=origin.support_place,
                platform_user_id=actor.user_id,
                default_allowance=self._runtime.settings.support.credits_per_user,
                exempt=actor.is_platform_admin,
            )
        if not view.active:
            return ended_text(view, now=datetime.now(UTC))
        return _Decided(origin, view, sealed)

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
        if view.catalog is not None:
            await context.send_card(menu_card(self._remember(asked), view, sealed=asked.sealed))
            return
        if (refused := _unaffordable(view)) is not None:
            await context.send(notice_card(refused))
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
        if asked is None or asked.user_id != actor.user_id:
            chat = actor.conversation_id
            asked = _Asked(actor.user_id, activity.from_.name, chat, chat, activity.id, _DM_LINK)
        if op == "pick" or (op == "send" and data.get(OPTION_INPUT)):
            return await self._act_plan(actor, token, asked, data, send=op == "send")
        note = str(data.get(NOTE_INPUT) or "").strip()[:MAX_NOTE_CHARS]
        if not note:
            return toast(USAGE)
        return await self._send(actor, token, asked, RequestChoice.default(note))

    async def _act_plan(
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
        picked = _pick(view, data)
        if picked == OPTIONS_CHANGED:
            return replace_card(text_card(TITLE, OPTIONS_CHANGED))
        if isinstance(picked, str):
            return replace_card(menu_card(token, view, sealed=sealed, error=picked))
        if not send:
            return replace_card(option_card(token, picked, view.revision, sealed=sealed))
        try:
            choice = read_choice(data, picked, view.revision)
        except SupportPlanError as err:
            card = option_card(
                token, picked, view.revision, sealed=sealed, error=str(err), values=data
            )
            return replace_card(card)
        return await self._send(actor, token, asked, choice)

    async def _send(
        self, actor: Actor, token: str, asked: _Asked, choice: RequestChoice
    ) -> AdaptiveCardInvokeResponse:
        if asked is self._asked.get(token):
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
        if view.catalog is not None:
            return dialog(TITLE, _menu_dialog(place.message_id, view, sealed=sealed))
        if (refused := _unaffordable(view)) is not None:
            return dialog_notice(TITLE, refused)
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
            return await self._ask_plan(actor, asked, data, send=stage == "send")
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

    async def _ask_plan(
        self, actor: Actor, asked: _Asked, data: Mapping[str, object], *, send: bool
    ) -> TaskModuleResponse:
        """The dialog's menu Next (answered with the option's card), or its Send."""
        decided = await self._decide(actor, asked, refuse=_dialog_refusal)
        if isinstance(decided, str):
            return dialog_message(decided)
        view, sealed, message_id = decided.view, decided.sealed, asked.message_id
        picked = _pick(view, data)
        if picked == OPTIONS_CHANGED:
            return dialog_message(OPTIONS_CHANGED)
        if isinstance(picked, str):
            return dialog(TITLE, _menu_dialog(message_id, view, sealed=sealed, error=picked))
        if not send:
            return dialog(TITLE, _option_dialog(message_id, picked, view.revision, sealed=sealed))
        try:
            choice = read_choice(data, picked, view.revision)
        except SupportPlanError as err:
            form = _option_dialog(
                message_id, picked, view.revision, sealed=sealed, error=str(err), values=data
            )
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
        if not await self._offered(origin):
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
        except SupportPlanError as err:
            # A missing step or a bad answer: nothing was recorded.
            return str(err)
        if outcome.status == "refused":
            log.info("support.refused", tenant_id=str(tenant_id))
            return refusal
        row, view = outcome.row, outcome.view
        if outcome.status != "recorded" or row is None or view is None:
            _log_not_recorded(outcome, tenant_id)
            return plan_refusal(outcome, now=datetime.now(UTC))
        log.info("support.escalation_recorded", escalation_id=str(row.id))
        asked = replace(asked, sealed=sealed)
        self._spawn(self._deliver(tenant_id, asked, row, view), name="teams.support.deliver")
        return received_text(status=row.status, charged_to=row.charged_to)

    async def _deliver(
        self, tenant_id: uuid.UUID, asked: _Asked, row: SupportEscalationRow, view: PlanView
    ) -> None:
        """Best effort: the row is committed, and stays undelivered unless a post lands.

        A database or Graph failure while picking admins still leaves the
        escalation channel to try, as on Slack. A request still to be charged
        is posted there whatever the chats did: its post is where staff set it.
        """
        facts = _facts(row, view)
        body = asked.body(row.note, facts)
        delivered = False
        try:
            delivered = await self._chat_admins(tenant_id, asked, body)
        except (SQLAlchemyError, *TEAMS_SEND_ERRORS) as exc:
            # OSError (in TEAMS_SEND_ERRORS): asyncpg raises a refused connection raw.
            log.warning(
                "support.admin_routing_failed",
                escalation_id=str(row.id),
                err_type=type(exc).__name__,
            )
        post_id = None
        if row.status == "pending":
            card = settle_card(row, asked.lines(facts))
            post_id = await post_card_to_support_channel(self._runtime, self._direct, card)
        elif not delivered:
            delivered = await post_to_support_channel(self._runtime, self._direct, body)
        delivered = delivered or post_id is not None
        if delivered:
            channel = self._runtime.settings.support.teams_escalation_channel_id
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
        log.info("support.escalation_delivered", escalation_id=str(row.id), delivered=delivered)

    async def _chat_admins(self, tenant_id: uuid.UUID, asked: _Asked, body: str) -> bool:
        """Message the channel's admins, else the deployment's. True once a tier got it.

        False at once for the 1:1 chat or a channel with no admins of its own,
        so it keeps the escalation channel. Each recipient is held to the
        tenant's DM policy and must be on the channel's team roster.
        """
        direct = self._direct
        if asked.place is None or direct is None:
            return False
        channel_id = asked.place.channel_id
        tiers = await support_recipient_tiers(
            self._runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="teams",
            channel_id=channel_id,
            requester_id=asked.user_id,
            members=team_owner_lookup(self._runtime),
            server_admins=live_admin_ids(self._runtime),
        )
        policies = self._runtime.settings.direct_message_policies
        policy = policies.get(tenant_id, DirectMessagePolicy())
        for tier in tiers:
            landed = 0
            for user_id in (uid for uid in tier if policy.allows(uid)):
                try:
                    member = await direct.member(channel_id, user_id)
                    if member is not None:
                        await direct.post(await direct.open_chat(member), body)
                        landed += 1
                except TEAMS_SEND_ERRORS as exc:
                    log.info("support.admin_dm_undelivered", err_type=type(exc).__name__)
            if landed:
                log.info("support.sent_to_admins", recipients=landed)
                return True
        return False


async def post_to_support_channel(
    runtime: TeamsRuntime, direct: DirectChats | None, body: str
) -> bool:
    """Post `body` to the Teams escalation channel; False when it did not land."""

    async def send(chats: DirectChats, channel: str) -> str:
        return await chats.post(channel, body)

    return await _post_in_support_channel(runtime, direct, send) is not None


async def post_card_to_support_channel(
    runtime: TeamsRuntime, direct: DirectChats | None, card: AdaptiveCard
) -> str | None:
    """Post `card` to the Teams escalation channel; its activity id, None when it did not land."""

    async def send(chats: DirectChats, channel: str) -> str:
        return await chats.post_card(channel, MessageActivityInput().add_card(card))

    return await _post_in_support_channel(runtime, direct, send)


async def _post_in_support_channel(
    runtime: TeamsRuntime,
    direct: DirectChats | None,
    send: Callable[[DirectChats, str], Awaitable[str]],
) -> str | None:
    """`send` to the escalation channel when this bot may post there.

    The channel is asked `authorize(POST)` with no agent, on a policy read
    just before the send, as Slack does, so a channel protected meanwhile is
    refused. No lock is held across the send.
    """
    channel = runtime.settings.support.teams_escalation_channel_id
    try:
        if not channel or direct is None or not await _may_post_to(runtime, channel):
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

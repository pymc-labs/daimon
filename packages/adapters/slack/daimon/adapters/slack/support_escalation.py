"""Slack "Ask a human" — the human-support path, at parity with Discord.

Discord seeds a third reaction on every answer and bridges it to a button in a
DM (`daimon.adapters.discord.support_escalation`). Slack needs no bridge: the
answer message already carries the feedback buttons, and a button click is a
``block_actions`` payload whose ``trigger_id`` opens a modal directly. So the
affordance is one more button in the same actions block, rendered only when
`DAIMON_SUPPORT__SLACK_ESCALATION_CHANNEL_ID` is set and credits are on.

The ledger, the credit gate and the person-facing copy are the core ones
(`daimon.core.support_escalation`, `daimon.core.stores.support_escalation`):
one allowance per person per tenant, spent from the same `support_escalations`
rows Discord writes. There is no Slack-only ledger.

Clicking spends nothing; only sending the note does. The click and the submit
both decide access the way a turn does — the clicker must be someone the
tenant lets start a turn in that place (`authorize(START_TURN)`), and an
external Slack Connect member is refused — and the submit decides again at the
moment of action: under the per-person ledger lock and then the tenant policy
lock, in the transaction that spends the credit (`record_escalation_once`), so
a policy edit committed first refuses with nothing spent and one arriving
later waits. The post into the escalation channel is a plain adapter post, so
it asks `authorize(POST)` with no agent, the same check a routine delivery
asks, from a fresh policy read right before the send: a protected destination
is refused.

Every request is posted in the escalation channel: `daimon.core.support_routing`
has no DM tier, so no one is messaged directly.

What reaches the escalation channel: the requester (mention, name, workspace),
a permalink to the answer, and the note the person typed. Nothing from the
conversation itself, which is what Discord posts too. A permalink opens only for
someone who can already read that channel, so a sealed origin is not widened by
it; the post marks a sealed origin so whoever picks it up knows to answer
there, and the modal tells the person that the note leaves the channel.

Idempotency: `record_escalation_once` records at most one request per person
per answer message (keyed on the Slack message ``ts``), under a per-person
ledger lock in the same transaction as the credit count, so a double click,
two open modals or a Slack retry spend one credit and post once.

A support plan with a menu shows it in the same modal: picking an option
(`handle_option_pick`) re-renders the modal with its fields, and Send records
it (`submit_request`, at the answer's channel, thread and agent). A pending
request's post carries Set charge and Decline (`support_settle`).

Hygiene: the note is the person's own words to the support team. It goes to
the database row and the escalation post, never into a log line.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final, cast

import structlog
from daimon.adapters.slack.admin import resolve_is_platform_admin
from daimon.adapters.slack.click_replies import (
    CLICK_REPLY_ERRORS,
    error_name,
    notice_modal,
    open_modal,
    post_ephemeral,
    update_modal,
)
from daimon.adapters.slack.gating import is_external_interactive
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.modal_limits import MAX_PLAIN_TEXT_INPUT_CHARS
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.place_access import (
    check_place_access,
    may_start_turn_at,
    stored_clicker,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.support_plan_form import (
    OFFER_PROMPT,
    build_plan_menu,
    form_meta,
    read_plan_form,
)
from daimon.adapters.slack.support_post import (
    NOT_NOTIFIED_LINE,
    SupportPost,
    pending_post_blocks,
    request_facts,
)
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.config import SupportSettings
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.permissions import readers_limited_at
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.support_escalation import (
    SubmitOutcome,
    find_escalation_for_message,
    mark_delivered,
    resolve_plan,
    submit_request,
)
from daimon.core.stores.support_rows import PlanView, SupportEscalationRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support_escalation import (
    ALREADY_REQUESTED,
    ASK_THE_TEAM,
    ESCALATE,
    RECORDED_UNDELIVERED,
    SEALED_NOTE_HINT,
    SEALED_POST_MARKER,
    UNAVAILABLE,
    is_enabled,
    offer_text,
)
from daimon.core.support_plans import (
    EXEMPT_OFFER,
    OPTIONS_CHANGED,
    RequestChoice,
    SupportPlace,
    SupportPlanError,
    balance_text,
    cheapest_bounds,
    ended_text,
    out_of_credits_text,
    price_bounds,
    received_text,
    refusal_text,
)
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ASK_HUMAN_ACTION_ID",
    "SUPPORT_CALLBACK_ID",
    "AnswerOrigin",
    "SupportSubmission",
    "answer_origin",
    "build_ask_human_button",
    "build_support_modal",
    "evaluate_plan_submission",
    "evaluate_support_submission",
    "handle_ask_human_click",
    "handle_option_pick",
    "post_to_support_channel",
    "run_support_submission",
    "slack_support_enabled",
]

log = structlog.get_logger()

# Disjoint from `feedback_vote:` and every other action_id app.py routes.
ASK_HUMAN_ACTION_ID: Final = "support_escalate"
SUPPORT_CALLBACK_ID: Final = "support_escalation"

_NOTE_BLOCK_ID: Final = "support_note_block"
_NOTE_INPUT_ID: Final = "support_note_input"

NOT_ALLOWED: Final = "Asking the team isn't available to you here."
POLICY_UNREADABLE: Final = (
    "This workspace's access settings could not be read, so nothing was sent. "
    "Ask an admin to check them."
)
CHECK_FAILED: Final = "That didn't work. Try again."
FORM_DID_NOT_OPEN: Final = "That didn't work. Try again."


def slack_support_enabled(support: SupportSettings) -> bool:
    """Whether Slack answers offer Ask a human (its own channel set, credits on).

    Anything but a configured string channel and an integer allowance reads
    as off, so a half-built settings object fails closed.
    """
    channel = cast(object, support.slack_escalation_channel_id)
    allowance = cast(object, support.credits_per_user)
    if not isinstance(channel, str) or not isinstance(allowance, int):
        return False
    return is_enabled(channel_id=channel, allowance=allowance)


def build_ask_human_button() -> dict[str, Any]:
    """The Ask the team button, appended to the answer's feedback actions block.

    Unstyled and unchanging after a click, for the reason the vote buttons
    are: the answer message is shared, so a per-click state would tell the
    channel who asked for help. The 🙋 sits beside the words, as the vote
    buttons carry theirs.
    """
    return {
        "type": "button",
        "action_id": ASK_HUMAN_ACTION_ID,
        "text": {
            "type": "plain_text",
            "text": f"{ESCALATE} {ASK_THE_TEAM}",
            "emoji": True,
        },
    }


def build_support_modal(
    *,
    channel_id: str,
    message_ts: str,
    thread_ts: str,
    remaining: int | None,
    sealed: bool,
    footer: str | None = None,
) -> dict[str, Any]:
    """The note form. ``private_metadata`` carries routing handles only, never identity.

    `remaining` counts the default plan's requests left; with None (an admin,
    or a plan without a menu) `footer` says what the request uses instead.
    The submit derives tenant and person from the verified payload, not from
    here, so a forged metadata blob can only point the CLICKER's own request
    (counted against their own allowance) at a different message.
    """
    if remaining is not None:
        prompt, count = offer_text(remaining=remaining).split("\n", 1)
        hints = [count]
    else:
        prompt, hints = OFFER_PROMPT, [footer] if footer else []
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": prompt}},
        *({"type": "context", "elements": [{"type": "mrkdwn", "text": h}]} for h in hints),
    ]
    if sealed:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": SEALED_NOTE_HINT}]}
        )
    blocks.append(
        {
            "type": "input",
            "block_id": _NOTE_BLOCK_ID,
            "label": {
                "type": "plain_text",
                "text": "What do you need help with?",
            },
            "element": {
                "type": "plain_text_input",
                "action_id": _NOTE_INPUT_ID,
                "multiline": True,
                "max_length": MAX_PLAIN_TEXT_INPUT_CHARS,
            },
        }
    )
    return {
        "type": "modal",
        "callback_id": SUPPORT_CALLBACK_ID,
        "private_metadata": json.dumps(
            {"channel_id": channel_id, "message_ts": message_ts, "thread_ts": thread_ts},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": ASK_THE_TEAM},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


@dataclasses.dataclass(frozen=True)
class AnswerOrigin:
    """The answer a request is about, and the place its plan and feature are read at."""

    tenant_id: uuid.UUID
    channel_id: str
    thread_ts: str
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
            self.tenant_id, "slack", self.channel_id, self.thread_ts, agent_id=self.agent_id
        )


async def answer_origin(
    runtime: SlackRuntime, *, team_id: str, channel_id: str, thread_ts: str
) -> AnswerOrigin:
    """Read where the answer sits now: its thread's agent and session (decision 9)."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session:
        row = await get_latest_thread_session(
            session, tenant_id=tenant_id, platform="slack", thread_id=thread_ts
        )
    return AnswerOrigin(
        tenant_id=tenant_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_id=None if row is None else row.ma_agent_id,
        ma_session_id=None if row is None else row.ma_session_id,
        budget_channel_id=None if row is None else row.budget_channel_id,
    )


@dataclasses.dataclass(frozen=True)
class SupportSubmission:
    """Outcome of the pure pre-ack evaluation of a note or menu form submission.

    ``response_payload`` is the ack body: field errors, or None to close the
    modal. ``note`` and ``choice`` live in memory only — never logged.
    """

    proceed: bool
    response_payload: dict[str, Any] | None
    team_id: str
    user_id: str
    user_name: str
    channel_id: str
    message_ts: str
    thread_ts: str
    note: str
    choice: RequestChoice | None = None


def _base_submission(
    payload: dict[str, Any], *, channel_id: str, message_ts: str, thread_ts: str
) -> SupportSubmission:
    team: dict[str, Any] = payload.get("team") or {}
    user: dict[str, Any] = payload.get("user") or {}
    return SupportSubmission(
        proceed=False,
        response_payload=None,
        team_id=str(team.get("id") or ""),
        user_id=str(user.get("id") or ""),
        user_name=str(user.get("username") or user.get("name") or ""),
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts or message_ts,
        note="",
    )


def evaluate_support_submission(payload: dict[str, Any]) -> SupportSubmission:
    """Pure (no I/O) evaluation of the note form, before the single ack.

    An external Slack Connect member's submission closes the modal and goes
    no further, as their click would have; so does one whose routing handles
    are missing.
    """
    view: dict[str, Any] = payload.get("view") or {}
    meta = form_meta(view)
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    block: dict[str, Any] = values.get(_NOTE_BLOCK_ID) or {}
    element: dict[str, Any] = block.get(_NOTE_INPUT_ID) or {}
    note = str(element.get("value") or "")
    base = _base_submission(
        payload,
        channel_id=str(meta.get("channel_id") or ""),
        message_ts=str(meta.get("message_ts") or ""),
        thread_ts=str(meta.get("thread_ts") or ""),
    )
    if is_external_interactive(payload):
        log.info("support.external_submission_rejected")
        return base
    if not (base.team_id and base.user_id and base.channel_id and base.message_ts):
        return base
    if not note.strip():
        return dataclasses.replace(
            base,
            response_payload={
                "response_action": "errors",
                "errors": {_NOTE_BLOCK_ID: "Please describe what you need help with."},
            },
        )
    return dataclasses.replace(base, proceed=True, note=note)


def evaluate_plan_submission(payload: dict[str, Any]) -> SupportSubmission:
    """Pure evaluation of a menu form, before the single ack; field errors keep it open."""
    view: dict[str, Any] = payload.get("view") or {}
    form = read_plan_form(view)
    base = _base_submission(
        payload, channel_id=form.channel_id, message_ts=form.message_ts, thread_ts=form.thread_ts
    )
    if is_external_interactive(payload):
        log.info("support.external_submission_rejected")
        return base
    if not (base.team_id and base.user_id and base.channel_id and base.message_ts):
        return base
    if form.errors:
        return dataclasses.replace(
            base, response_payload={"response_action": "errors", "errors": form.errors}
        )
    return dataclasses.replace(base, proceed=True, choice=form.choice)


async def _decide_click(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    origin: AnswerOrigin,
    message_ts: str,
) -> dict[str, Any] | str:
    """The note form or the menu for this click, or the text saying why there is none."""
    support = runtime.settings.support
    tenant_id, channel_id, thread_ts = origin.tenant_id, origin.channel_id, origin.thread_ts
    async with runtime.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.archived_at is not None:
        log.info("support.tenant_missing", tenant_id=str(tenant_id))
        return UNAVAILABLE
    access = await check_place_access(
        runtime,
        client,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    if access.decision == "unreadable" or access.policy is None:
        return POLICY_UNREADABLE
    if access.decision == "refused":
        log.info("support.refused", tenant_id=str(tenant_id))
        return NOT_ALLOWED
    # Decision 5: a workspace admin's requests use no credits.
    exempt = await resolve_is_platform_admin(client, user_id=user_id, host_team_id=team_id)
    async with runtime.sessionmaker() as session:
        view = await resolve_plan(
            session,
            tenant_id=tenant_id,
            place=origin.support_place,
            platform_user_id=user_id,
            default_allowance=support.credits_per_user,
            exempt=exempt,
        )
        already = await find_escalation_for_message(
            session,
            tenant_id=tenant_id,
            platform="slack",
            platform_user_id=user_id,
            channel_id=channel_id,
            message_id=message_ts,
        )

    if not view.active:
        return ended_text(view, now=datetime.now(UTC))
    sealed = readers_limited_at(access.policy, channel_id=channel_id, thread_id=thread_ts)
    if view.catalog is not None:
        # A menu asks once per option: `submit_request` dedups, so another
        # option on the same answer stays open.
        return build_plan_menu(
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=thread_ts,
            view=view,
            sealed=sealed,
        )
    if already is not None:
        return ALREADY_REQUESTED
    if not view.affordable(price_bounds(view.spec.options[0].price)):
        return out_of_credits_text(view.charged_to)
    left = view.available if view.charged_to == "default" else None
    return build_support_modal(
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts,
        remaining=None if left is None else int(max(left, 0)),
        sealed=sealed,
        footer=EXEMPT_OFFER if view.exempt else balance_text(view.balance, view.plan),
    )


async def _escalation_offered(runtime: SlackRuntime, origin: AnswerOrigin) -> bool:
    """Whether `support_escalation` is on at the answer's channel, thread and agent."""
    placed = await place_feature(
        runtime.sessionmaker, runtime.settings, origin.feature_place, "support_escalation"
    )
    return placed.on


async def handle_ask_human_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Open the note form, or say why not. Spends nothing.

    The modal opens first, as "Checking…", and is then replaced by the form
    or by the reason there is none: the checks below include a live Slack
    user-group lookup, and running them before ``views.open`` could outlive
    the click's 3-second ``trigger_id`` and leave the button looking dead.
    When the modal could not open at all, the answer comes as an ephemeral.

    The credit read here is UX only — someone can open the form, spend their
    last credit elsewhere, and submit — the write transaction decides.
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    user_info: dict[str, Any] = payload.get("user") or {}
    channel_info: dict[str, Any] = payload.get("channel") or {}
    container: dict[str, Any] = payload.get("container") or {}
    message: dict[str, Any] = payload.get("message") or {}
    team_id = str(team_info.get("id") or "")
    user_id = str(user_info.get("id") or "")
    channel_id = str(channel_info.get("id") or container.get("channel_id") or "")
    message_ts = str(container.get("message_ts") or "")
    thread_ts = str(message.get("thread_ts") or container.get("thread_ts") or "") or message_ts
    trigger_id = str(payload.get("trigger_id") or "")
    if is_external_interactive(payload):
        log.info("support.external_click_rejected")
        return
    if not (team_id and user_id and channel_id and message_ts and trigger_id):
        return

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return

    if not slack_support_enabled(runtime.settings.support):
        log.info("support.disabled", platform="slack")
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            text=UNAVAILABLE,
            error=True,
        )
        return

    view_id = await open_modal(
        client,
        trigger_id=trigger_id,
        view=notice_modal(title=ASK_THE_TEAM, text="Checking\N{HORIZONTAL ELLIPSIS}"),
    )

    async def reply(text: str) -> None:
        if view_id is not None and await update_modal(
            client, view_id=view_id, view=notice_modal(title=ASK_THE_TEAM, text=text)
        ):
            return
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            text=text,
            error=True,
        )

    try:
        origin = await answer_origin(
            runtime, team_id=team_id, channel_id=channel_id, thread_ts=thread_ts
        )
        if not await _escalation_offered(runtime, origin):
            log.info("support.disabled", platform="slack")
            await reply(UNAVAILABLE)
            return
        decided = await _decide_click(
            runtime,
            client,
            team_id=team_id,
            user_id=user_id,
            origin=origin,
            message_ts=message_ts,
        )
    except (SQLAlchemyError, OSError, *CLICK_REPLY_ERRORS) as err:
        # OSError: asyncpg raises a refused connection raw, outside SQLAlchemy's.
        # Never leave the Checking… notice standing: say so, then let it be seen.
        log.warning("support.click_check_failed", error=error_name(err))
        await reply(CHECK_FAILED)
        return
    if isinstance(decided, str):
        await reply(decided)
        return
    form = decided
    if view_id is not None and await update_modal(client, view_id=view_id, view=form):
        return
    await post_ephemeral(
        client,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
        message_ts=message_ts,
        text=FORM_DID_NOT_OPEN,
        error=True,
    )


async def _picked_menu(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    meta: dict[str, Any],
    picked: str,
) -> dict[str, Any]:
    """The menu with `picked`'s form, read from the plan now (decision 9)."""
    message_ts = str(meta.get("message_ts") or "")
    thread_ts = str(meta.get("thread_ts") or "") or message_ts
    channel_id = str(meta.get("channel_id") or "")
    origin = await answer_origin(
        runtime, team_id=team_id, channel_id=channel_id, thread_ts=thread_ts
    )
    exempt = await resolve_is_platform_admin(client, user_id=user_id, host_team_id=team_id)
    async with runtime.sessionmaker() as session:
        view = await resolve_plan(
            session,
            tenant_id=origin.tenant_id,
            place=origin.support_place,
            platform_user_id=user_id,
            default_allowance=runtime.settings.support.credits_per_user,
            exempt=exempt,
        )
        try:
            policy = await load_access_policy(session, tenant_id=origin.tenant_id)
        except AccessPolicyUnreadable:
            policy = None
    option = view.spec.option(picked)
    if option is None or view.catalog is None or view.revision != meta.get("revision"):
        return notice_modal(title=ASK_THE_TEAM, text=OPTIONS_CHANGED)
    if not view.active:
        return notice_modal(title=ASK_THE_TEAM, text=ended_text(view, now=datetime.now(UTC)))
    # An unreadable policy counts as sealed: the hint only asks for care.
    sealed = policy is None or readers_limited_at(
        policy, channel_id=channel_id, thread_id=thread_ts
    )
    affordable = view.affordable(cheapest_bounds(option.price))
    return build_plan_menu(
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts,
        view=view,
        sealed=sealed,
        picked=option.key if affordable else None,
        unaffordable=None if affordable else option.key,
    )


async def handle_option_pick(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """A menu pick: re-render the modal with that option's form, or say why not.

    Spends nothing; Send decides again (`run_support_submission`).
    """
    if is_external_interactive(payload):
        log.info("support.external_click_rejected")
        return
    team_info: dict[str, Any] = payload.get("team") or {}
    user_info: dict[str, Any] = payload.get("user") or {}
    view: dict[str, Any] = payload.get("view") or {}
    actions: list[dict[str, Any]] = payload.get("actions") or []
    action = actions[0] if actions else {}
    selected: dict[str, Any] = action.get("selected_option") or {}
    team_id = str(team_info.get("id") or "")
    user_id = str(user_info.get("id") or "")
    view_id = str(view.get("id") or "")
    picked = str(selected.get("value") or "")
    meta = form_meta(view)
    if not (team_id and user_id and view_id and picked and meta.get("message_ts")):
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    try:
        menu = await _picked_menu(
            runtime, client, team_id=team_id, user_id=user_id, meta=meta, picked=picked
        )
    except (SQLAlchemyError, OSError, *CLICK_REPLY_ERRORS) as err:
        # As at the click: say so rather than leave the old form standing.
        log.warning("support.option_pick_failed", error=error_name(err))
        menu = notice_modal(title=ASK_THE_TEAM, text=CHECK_FAILED)
    await update_modal(client, view_id=view_id, view=menu)


# Named for the races tests, which observe the recorder's connection by this name.
record_escalation_once = submit_request


def _log_not_recorded(outcome: SubmitOutcome, tenant_id: uuid.UUID) -> None:
    if outcome.status == "duplicate":
        log.info("support.duplicate_request", tenant_id=str(tenant_id))
    elif outcome.status == "out_of_credits":
        charged_to = None if outcome.view is None else outcome.view.charged_to
        log.info("support.out_of_credits", tenant_id=str(tenant_id), charged_to=charged_to)
    else:
        log.info("support.request_not_recorded", tenant_id=str(tenant_id), status=outcome.status)


async def run_support_submission(runtime: SlackRuntime, submission: SupportSubmission) -> None:
    """Decide again, record the request once, then post it to the escalation channel.

    Same ordering as Discord: the row is committed BEFORE the post is
    attempted and `delivered_at` is stamped only once it lands, so a request
    the channel never received survives as an undelivered row.
    """
    s = submission
    client = await resolve_web_client(runtime, team_id=s.team_id)
    if client is None:
        return

    async def reply(text: str, *, error: bool = True) -> None:
        await post_ephemeral(
            client,
            channel_id=s.channel_id,
            user_id=s.user_id,
            thread_ts=s.thread_ts,
            message_ts=s.message_ts,
            text=text,
            error=error,
        )

    support = runtime.settings.support
    dest_channel = support.slack_escalation_channel_id
    if dest_channel is None or not slack_support_enabled(support):
        await reply(UNAVAILABLE)
        return
    origin = await answer_origin(
        runtime, team_id=s.team_id, channel_id=s.channel_id, thread_ts=s.thread_ts
    )
    if not await _escalation_offered(runtime, origin):
        # Disabled between the click and the submit. Nothing is spent.
        await reply(UNAVAILABLE)
        return

    tenant_id = origin.tenant_id
    # What the authoritative source check saw, for the reply and the post.
    decided: dict[str, Any] = {}
    async with runtime.sessionmaker() as session:
        stored, account_id = await stored_clicker(session, tenant_id=tenant_id, user_id=s.user_id)
    # Looked up with no session open: a slow Slack must not hold a connection.
    subject = stored.subject()
    exempt = await resolve_is_platform_admin(client, user_id=s.user_id, host_team_id=s.team_id)
    choice = s.choice or RequestChoice.default(s.note)
    try:
        async with runtime.sessionmaker() as session, session.begin():
            tenant = await get_tenant(session, tenant_id)
            if tenant is None or tenant.archived_at is not None:
                log.info("support.tenant_missing", tenant_id=str(tenant_id))
                return

            async def source_allowed(locked: AsyncSession) -> bool:
                # Runs under the ledger and policy locks (`record_escalation_once`),
                # so this policy read is the one the credit is spent against.
                try:
                    policy = await load_access_policy(locked, tenant_id=tenant_id)
                except AccessPolicyUnreadable:
                    decided["refusal"] = POLICY_UNREADABLE
                    return False
                decided["sealed"] = readers_limited_at(
                    policy, channel_id=s.channel_id, thread_id=s.thread_ts
                )
                if not may_start_turn_at(
                    policy, subject, channel_id=s.channel_id, thread_ts=s.thread_ts
                ):
                    decided["refusal"] = NOT_ALLOWED
                    return False
                return True

            outcome = await record_escalation_once(
                session,
                tenant_id=tenant_id,
                account_id=account_id,
                platform="slack",
                platform_user_id=s.user_id,
                channel_id=s.channel_id,
                message_id=s.message_ts,
                ma_session_id=origin.ma_session_id,
                place=origin.support_place,
                choice=choice,
                default_allowance=support.credits_per_user,
                exempt=exempt,
                source_allowed=source_allowed,
            )
    except SupportPlanError as err:
        # A missing step or a bad answer: nothing was recorded.
        await reply(str(err))
        return
    sealed = bool(decided.get("sealed", False))

    if outcome.status == "refused":
        log.info("support.refused", tenant_id=str(tenant_id))
        await reply(str(decided.get("refusal") or NOT_ALLOWED))
        return
    row, view = outcome.row, outcome.view
    if outcome.status != "recorded" or row is None or view is None:
        _log_not_recorded(outcome, tenant_id)
        text = refusal_text(outcome, now=datetime.now(UTC))
        await reply(text, error=outcome.status != "duplicate")
        return

    # The row is committed and the credit spent; everything below is
    # best-effort delivery, and the person hears the outcome whatever fails.
    post: SupportPost | None = None
    try:
        post = await _post_to_escalation_channel(
            runtime,
            source_client=client,
            submission=s,
            dest_channel=dest_channel,
            sealed=sealed,
            row=row,
            view=view,
        )
        if post is not None:
            async with runtime.sessionmaker() as session, session.begin():
                await mark_delivered(
                    session,
                    escalation_id=row.id,
                    support_channel_id=post.channel_id,
                    support_post_id=post.ts,
                )
    except (SQLAlchemyError, OSError, *CLICK_REPLY_ERRORS) as err:
        # Delivered but not stamped still reads as received: it landed.
        log.warning(
            "support.delivery_failed",
            escalation_id=str(row.id),
            delivered=post is not None,
            error=error_name(err),
        )
    log.info(
        "support.escalation_recorded",
        escalation_id=str(row.id),
        tenant_id=str(tenant_id),
        delivered=post is not None,
    )
    await reply(
        received_text(row, view) if post is not None else RECORDED_UNDELIVERED,
        error=False,
    )


async def _permalink(client: AsyncWebClient, *, channel_id: str, message_ts: str) -> str | None:
    try:
        resp = await client.chat_getPermalink(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id, message_ts=message_ts
        )
    except CLICK_REPLY_ERRORS:
        return None
    link: Any = resp.get("permalink")  # pyright: ignore[reportUnknownMemberType]
    return link if isinstance(link, str) and link else None


def _post_parts(
    *, submission: SupportSubmission, link: str | None, sealed: bool
) -> tuple[str, list[str]]:
    """The post's head (who asked), then where and the sealed marker."""
    s = submission
    who = f"<@{s.user_id}>"
    if s.user_name:
        who += f" ({escape_mrkdwn(s.user_name)}, {s.user_id} in {s.team_id})"
    else:
        who += f" ({s.user_id} in {s.team_id})"
    where = link if link is not None else f"message {s.message_ts} in channel {s.channel_id}"
    rest = [where, f"_{SEALED_POST_MARKER}_"] if sealed else [where]
    return f"*Human support requested* by {who}", rest


def render_escalation_post(
    *,
    submission: SupportSubmission,
    link: str | None,
    sealed: bool,
    facts: str | None = None,
    note: str | None = None,
) -> str:
    """The escalation channel's message: who, a link, and their note. Nothing else.

    No answer text and no channel name: the same fields Discord posts. The
    requester's name and workspace are spelled out because the escalation
    channel may sit in another workspace, where the mention would not resolve.
    A plan request adds its `facts` (terms, status, balance) after the head.
    """
    head, rest = _post_parts(submission=submission, link=link, sealed=sealed)
    body = escape_mrkdwn(submission.note if note is None else note)
    return "\n\n".join([head, *([facts] if facts else []), *rest, *([body] if body else [])])


async def _post_to_escalation_channel(
    runtime: SlackRuntime,
    *,
    source_client: AsyncWebClient,
    submission: SupportSubmission,
    dest_channel: str,
    sealed: bool,
    row: SupportEscalationRow,
    view: PlanView,
) -> SupportPost | None:
    """Post the request; a pending one carries Set charge and Decline. None unless it landed."""
    s = submission
    # The default form's note as typed, as before; a menu's answers as recorded.
    note = s.note if s.choice is None else row.note
    facts: str | None = None
    if row.charged_to != "default":
        balance = balance_text(view.balance, view.plan) if row.charged_to == "plan" else None
        facts = request_facts(row, menu=view.catalog is not None, balance=balance)
    blocks: Callable[[str | None], list[dict[str, Any]]] | None = None
    text_facts = facts
    # `support_settle` accepts a settle in the named escalation workspace, or from
    # the request's own workspace when none is named.
    if facts is not None and row.status == "pending":
        pending_facts = facts
        text_facts = f"{facts}\n{NOT_NOTIFIED_LINE}"

        def render_blocks(link: str | None) -> list[dict[str, Any]]:
            head, rest = _post_parts(submission=s, link=link, sealed=sealed)
            return pending_post_blocks(
                head=head,
                facts=pending_facts,
                rest=rest,
                note=escape_mrkdwn(note),
                escalation_id=row.id,
            )

        blocks = render_blocks

    return await post_to_support_channel(
        runtime,
        source_client=source_client,
        source_team_id=s.team_id,
        channel_id=s.channel_id,
        message_ts=s.message_ts,
        dest_channel=dest_channel,
        render=lambda link: render_escalation_post(
            submission=s, link=link, sealed=sealed, facts=text_facts, note=note
        ),
        render_blocks=blocks,
    )


async def post_to_support_channel(
    runtime: SlackRuntime,
    *,
    source_client: AsyncWebClient,
    source_team_id: str,
    channel_id: str,
    message_ts: str,
    dest_channel: str,
    render: Callable[[str | None], str],
    render_blocks: Callable[[str | None], list[dict[str, Any]]] | None = None,
) -> SupportPost | None:
    """Post ``render(permalink to the answer)`` to the support channel; None unless it landed.

    Shared by Ask a human and the routed 👎 form (`daimon.adapters.slack.feedback`).
    `render_blocks` adds blocks (the settle buttons) beside the text.
    Posts with the escalation workspace's token when
    `slack_escalation_team_id` names one, else the source workspace's own. The
    destination tenant's policy is read fresh and asked `authorize(POST)` with
    no agent immediately before the send (after the permalink round trip), so
    a channel protected meanwhile is refused.
    No policy lock is held across the send: a protection committed in the
    instant between that read and Slack accepting the post is not ordered
    against it.
    """
    support = runtime.settings.support
    dest_team = support.slack_escalation_team_id or source_team_id
    dest_client = (
        source_client
        if dest_team == source_team_id
        else await resolve_web_client(runtime, team_id=dest_team)
    )
    if dest_client is None:
        log.warning("support.destination_workspace_unavailable")
        return None
    # Network I/O first, so the destination decision below is the last thing
    # before the send and nothing awaits between them but the policy read.
    link = await _permalink(source_client, channel_id=channel_id, message_ts=message_ts)
    text = render(link)
    extra: dict[str, Any] = {} if render_blocks is None else {"blocks": render_blocks(link)}
    dest_tenant = derive_tenant_uuid(platform="slack", workspace_id=dest_team)
    async with runtime.sessionmaker() as session:
        try:
            dest_policy = await load_access_policy(session, tenant_id=dest_tenant)
        except AccessPolicyUnreadable:
            log.warning("support.destination_policy_unreadable")
            return None
    if not authorize(
        dest_policy, subject=Subject(), action=Action.POST, place=Place(channel_id=dest_channel)
    ):
        log.warning("support.destination_protected", channel_id=dest_channel)
        return None
    try:
        resp = await dest_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=dest_channel, text=text, unfurl_links=False, unfurl_media=False, **extra
        )
    except CLICK_REPLY_ERRORS as err:
        # As with the DMs: an ambiguous timeout stays undelivered; daimon does not re-post.
        log.warning("support.channel_undeliverable", channel_id=dest_channel, error=error_name(err))
        return None
    channel: Any = resp.get("channel")  # pyright: ignore[reportUnknownMemberType]
    ts: Any = resp.get("ts")  # pyright: ignore[reportUnknownMemberType]
    return SupportPost(
        channel_id=channel if isinstance(channel, str) and channel else dest_channel,
        ts=ts if isinstance(ts, str) and ts else None,
    )

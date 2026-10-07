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

A channel with its own admins sends the request to them by DM first, then to
the server admins, and to the escalation channel only when no DM landed
(`daimon.core.support_routing`); the DM carries what the post would.

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

Hygiene: the note is the person's own words to the support team. It goes to
the database row and the escalation post, never into a log line.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any, Final, cast

import structlog
from daimon.adapters.slack.channel_admin_groups import user_group_members
from daimon.adapters.slack.click_replies import (
    notice_modal,
    open_modal,
    post_ephemeral,
    update_modal,
)
from daimon.adapters.slack.gating import is_external_interactive
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.place_access import (
    check_place_access,
    may_start_turn_at,
    stored_clicker,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_admins import confirm_stored_subject
from daimon.core.config import DirectMessagePolicy, SupportSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.permissions import readers_limited_at
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.support_escalation import (
    count_escalations_for_user,
    find_escalation_for_message,
    mark_delivered,
    record_escalation_once,
)
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support_escalation import (
    ALREADY_REQUESTED,
    OUT_OF_CREDITS,
    RECORDED_UNDELIVERED,
    UNAVAILABLE,
    has_credit,
    is_enabled,
    offer_text,
    received_text,
    remaining_credits,
)
from daimon.core.support_routing import support_recipient_tiers
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ASK_HUMAN_ACTION_ID",
    "SUPPORT_CALLBACK_ID",
    "SupportSubmission",
    "build_ask_human_button",
    "build_support_modal",
    "evaluate_support_submission",
    "handle_ask_human_click",
    "run_support_submission",
    "slack_support_enabled",
]

log = structlog.get_logger()

# Disjoint from `feedback_vote:` and every other action_id app.py routes.
ASK_HUMAN_ACTION_ID: Final = "support_escalate"
SUPPORT_CALLBACK_ID: Final = "support_escalation"

_NOTE_BLOCK_ID: Final = "support_note_block"
_NOTE_INPUT_ID: Final = "support_note_input"

NOT_ALLOWED: Final = "Human support isn't available to you here."
POLICY_UNREADABLE: Final = (
    "This workspace's access policy could not be read, so nothing was sent. "
    "Ask an admin to check it."
)
FORM_DID_NOT_OPEN: Final = (
    "Slack didn't open the form in time. Click *Ask a human* again; nothing was spent."
)
SEALED_NOTE_HINT: Final = (
    "Only turns inside this channel read it. Your note goes to the support team outside it, so "
    "don't paste anything that has to stay here. They get a link to this answer, "
    "not its content."
)


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
    """The Ask a human button, appended to the answer's feedback actions block.

    Unstyled and unchanging after a click, for the reason the vote buttons
    are: the answer message is shared, so a per-click state would tell the
    channel who asked for help.
    """
    return {
        "type": "button",
        "action_id": ASK_HUMAN_ACTION_ID,
        "text": {"type": "plain_text", "text": "Ask a human"},
    }


def build_support_modal(
    *, channel_id: str, message_ts: str, thread_ts: str, remaining: int, sealed: bool
) -> dict[str, Any]:
    """The note form. ``private_metadata`` carries routing handles only, never identity.

    The submit derives tenant and person from the verified payload, not from
    here, so a forged metadata blob can only point the CLICKER's own request
    (counted against their own allowance) at a different message.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": offer_text(remaining=remaining)}},
    ]
    if sealed:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": SEALED_NOTE_HINT}]}
        )
    blocks.append(
        {
            "type": "input",
            "block_id": _NOTE_BLOCK_ID,
            "label": {"type": "plain_text", "text": "What do you need help with?"},
            "element": {
                "type": "plain_text_input",
                "action_id": _NOTE_INPUT_ID,
                "multiline": True,
                "max_length": 4000,
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
        "title": {"type": "plain_text", "text": "Ask a human"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


@dataclasses.dataclass(frozen=True)
class SupportSubmission:
    """Outcome of the pure pre-ack evaluation of a support_escalation submission.

    ``response_payload`` is the ack body: field errors for an empty note, or
    None to close the modal. ``note`` lives in memory only — never logged.
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


def evaluate_support_submission(payload: dict[str, Any]) -> SupportSubmission:
    """Pure (no I/O) evaluation of the note form, before the single ack.

    An external Slack Connect member's submission closes the modal and goes
    no further, as their click would have; so does one whose routing handles
    are missing.
    """
    team: dict[str, Any] = payload.get("team") or {}
    user: dict[str, Any] = payload.get("user") or {}
    view: dict[str, Any] = payload.get("view") or {}
    try:
        meta_raw: Any = json.loads(str(view.get("private_metadata") or "") or "{}")
    except json.JSONDecodeError:
        meta_raw = {}
    meta: dict[str, Any] = cast("dict[str, Any]", meta_raw) if isinstance(meta_raw, dict) else {}
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    block: dict[str, Any] = values.get(_NOTE_BLOCK_ID) or {}
    element: dict[str, Any] = block.get(_NOTE_INPUT_ID) or {}
    note = str(element.get("value") or "")
    channel_id = str(meta.get("channel_id") or "")
    message_ts = str(meta.get("message_ts") or "")
    base = SupportSubmission(
        proceed=False,
        response_payload=None,
        team_id=str(team.get("id") or ""),
        user_id=str(user.get("id") or ""),
        user_name=str(user.get("username") or user.get("name") or ""),
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=str(meta.get("thread_ts") or "") or message_ts,
        note="",
    )
    if is_external_interactive(payload):
        log.info("support.external_submission_rejected")
        return base
    if not (base.team_id and base.user_id and channel_id and message_ts):
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

    support = runtime.settings.support
    if not slack_support_enabled(support):
        log.info("support.disabled", platform="slack")
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            text=UNAVAILABLE,
        )
        return

    view_id = await open_modal(
        client,
        trigger_id=trigger_id,
        view=notice_modal(title="Ask a human", text="Checking\N{HORIZONTAL ELLIPSIS}"),
    )

    async def reply(text: str) -> None:
        if view_id is not None and await update_modal(
            client, view_id=view_id, view=notice_modal(title="Ask a human", text=text)
        ):
            return
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            text=text,
        )

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.archived_at is not None:
        log.info("support.tenant_missing", tenant_id=str(tenant_id))
        await reply(UNAVAILABLE)
        return
    access = await check_place_access(
        runtime,
        client,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    if access.decision == "unreadable" or access.policy is None:
        await reply(POLICY_UNREADABLE)
        return
    if access.decision == "refused":
        log.info("support.refused", tenant_id=str(tenant_id))
        await reply(NOT_ALLOWED)
        return
    async with runtime.sessionmaker() as session:
        already = await find_escalation_for_message(
            session,
            tenant_id=tenant_id,
            platform="slack",
            platform_user_id=user_id,
            channel_id=channel_id,
            message_id=message_ts,
        )
        used = await count_escalations_for_user(
            session, tenant_id=tenant_id, platform_user_id=user_id
        )

    if already is not None:
        await reply(ALREADY_REQUESTED)
        return
    allowance = support.credits_per_user
    if not has_credit(allowance=allowance, used=used):
        await reply(OUT_OF_CREDITS)
        return
    form = build_support_modal(
        channel_id=channel_id,
        message_ts=message_ts,
        thread_ts=thread_ts,
        remaining=remaining_credits(allowance=allowance, used=used),
        sealed=readers_limited_at(access.policy, channel_id=channel_id, thread_id=thread_ts),
    )
    if view_id is not None and await update_modal(client, view_id=view_id, view=form):
        return
    await post_ephemeral(
        client,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
        message_ts=message_ts,
        text=FORM_DID_NOT_OPEN,
    )


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

    async def reply(text: str) -> None:
        await post_ephemeral(
            client,
            channel_id=s.channel_id,
            user_id=s.user_id,
            thread_ts=s.thread_ts,
            message_ts=s.message_ts,
            text=text,
        )

    support = runtime.settings.support
    dest_channel = support.slack_escalation_channel_id
    if dest_channel is None or not slack_support_enabled(support):
        # Disabled between the click and the submit. Nothing is spent.
        await reply(UNAVAILABLE)
        return

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=s.team_id)
    # What the authoritative source check saw, for the reply and the post.
    decided: dict[str, Any] = {}
    async with runtime.sessionmaker() as session:
        stored, account_id = await stored_clicker(session, tenant_id=tenant_id, user_id=s.user_id)
    # Looked up with no session open: a slow Slack must not hold a connection.
    subject = await confirm_stored_subject(
        stored, user_group_members(runtime, client, tenant_id=tenant_id)
    )
    async with runtime.sessionmaker() as session, session.begin():
        tenant = await get_tenant(session, tenant_id)
        if tenant is None or tenant.archived_at is not None:
            log.info("support.tenant_missing", tenant_id=str(tenant_id))
            return
        thread_row = await get_latest_thread_session(
            session, tenant_id=tenant_id, platform="slack", thread_id=s.thread_ts
        )

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
            ma_session_id=thread_row.ma_session_id if thread_row is not None else None,
            note=s.note,
            allowance=support.credits_per_user,
            source_allowed=source_allowed,
        )
    sealed = bool(decided.get("sealed", False))

    if outcome.status == "refused":
        log.info("support.refused", tenant_id=str(tenant_id))
        await reply(str(decided.get("refusal") or NOT_ALLOWED))
        return
    if outcome.status == "duplicate":
        log.info("support.duplicate_request", tenant_id=str(tenant_id))
        await reply(ALREADY_REQUESTED)
        return
    if outcome.row is None:
        log.info("support.out_of_credits", tenant_id=str(tenant_id))
        await reply(OUT_OF_CREDITS)
        return

    # The row is committed; everything below is best-effort delivery.
    delivered = await _dm_channel_admins(
        runtime, client, submission=s, tenant_id=tenant_id, sealed=sealed
    ) or await _post_to_escalation_channel(
        runtime,
        source_client=client,
        submission=s,
        dest_channel=dest_channel,
        sealed=sealed,
    )
    if delivered:
        async with runtime.sessionmaker() as session, session.begin():
            await mark_delivered(session, escalation_id=outcome.row.id)
    log.info(
        "support.escalation_recorded",
        escalation_id=str(outcome.row.id),
        tenant_id=str(tenant_id),
        delivered=delivered,
    )
    await reply(received_text(remaining=outcome.remaining) if delivered else RECORDED_UNDELIVERED)


async def _permalink(client: AsyncWebClient, *, channel_id: str, message_ts: str) -> str | None:
    try:
        resp = await client.chat_getPermalink(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id, message_ts=message_ts
        )
    except SlackApiError:
        return None
    link: Any = resp.get("permalink")  # pyright: ignore[reportUnknownMemberType]
    return link if isinstance(link, str) and link else None


def render_escalation_post(*, submission: SupportSubmission, link: str | None, sealed: bool) -> str:
    """The escalation channel's message: who, a link, and their note. Nothing else.

    No answer text and no channel name: the same fields Discord posts. The
    requester's name and workspace are spelled out because the escalation
    channel may sit in another workspace, where the mention would not resolve.
    """
    s = submission
    who = f"<@{s.user_id}>"
    if s.user_name:
        who += f" ({escape_mrkdwn(s.user_name)}, {s.user_id} in {s.team_id})"
    else:
        who += f" ({s.user_id} in {s.team_id})"
    where = link if link is not None else f"message {s.message_ts} in channel {s.channel_id}"
    lines = [f"*Human support requested* by {who}", where]
    if sealed:
        lines.append(
            "_From a channel read only from inside: answer there, the conversation stays in it._"
        )
    return "\n".join(lines) + "\n\n" + escape_mrkdwn(s.note)


async def _dm_channel_admins(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    submission: SupportSubmission,
    tenant_id: uuid.UUID,
    sealed: bool,
) -> bool:
    """DM the origin channel's admins, else the server admins. True once a tier got it.

    False at once for a channel with no admins of its own, so it keeps the
    escalation channel. Each DM is held to the tenant's DM policy.
    """
    s = submission
    tiers = await support_recipient_tiers(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        channel_id=s.channel_id,
        requester_id=s.user_id,
        members=user_group_members(runtime, client, tenant_id=tenant_id),
    )
    if not tiers:
        return False
    policy = runtime.settings.direct_message_policies.get(tenant_id, DirectMessagePolicy())
    link = await _permalink(client, channel_id=s.channel_id, message_ts=s.message_ts)
    text = render_escalation_post(submission=s, link=link, sealed=sealed)
    for tier in tiers:
        landed = 0
        for user_id in (uid for uid in tier if policy.allows(uid)):
            try:
                opened = await client.conversations_open(users=user_id)  # pyright: ignore[reportUnknownMemberType]
                channel = cast("dict[str, str]", opened["channel"])["id"]
                await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel, text=text, unfurl_links=False, unfurl_media=False
                )
                landed += 1
            except SlackApiError as err:
                log.info(
                    "support.admin_dm_undelivered",
                    error=str(err.response.get("error", "slack_api_error")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
                )
        if landed:
            log.info("support.sent_to_admins", recipients=landed)
            return True
    return False


async def _post_to_escalation_channel(
    runtime: SlackRuntime,
    *,
    source_client: AsyncWebClient,
    submission: SupportSubmission,
    dest_channel: str,
    sealed: bool,
) -> bool:
    """Post the request. True only if it landed.

    Posts with the escalation workspace's token when
    `slack_escalation_team_id` names one, else the requester's own. The
    destination tenant's policy is read fresh and asked `authorize(POST)` with
    no agent immediately before the send (after the permalink round trip), so
    a channel protected meanwhile is refused and the row stays undelivered.
    No policy lock is held across the send: a protection committed in the
    instant between that read and Slack accepting the post is not ordered
    against it.
    """
    support = runtime.settings.support
    dest_team = support.slack_escalation_team_id or submission.team_id
    dest_client = (
        source_client
        if dest_team == submission.team_id
        else await resolve_web_client(runtime, team_id=dest_team)
    )
    if dest_client is None:
        log.warning("support.destination_workspace_unavailable")
        return False
    # Network I/O first, so the destination decision below is the last thing
    # before the send and nothing awaits between them but the policy read.
    link = await _permalink(
        source_client, channel_id=submission.channel_id, message_ts=submission.message_ts
    )
    text = render_escalation_post(submission=submission, link=link, sealed=sealed)
    dest_tenant = derive_tenant_uuid(platform="slack", workspace_id=dest_team)
    async with runtime.sessionmaker() as session:
        try:
            dest_policy = await load_access_policy(session, tenant_id=dest_tenant)
        except AccessPolicyUnreadable:
            log.warning("support.destination_policy_unreadable")
            return False
    if not authorize(
        dest_policy, subject=Subject(), action=Action.POST, place=Place(channel_id=dest_channel)
    ):
        log.warning("support.destination_protected", channel_id=dest_channel)
        return False
    try:
        await dest_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=dest_channel, text=text, unfurl_links=False, unfurl_media=False
        )
    except SlackApiError as err:
        log.warning(
            "support.channel_undeliverable",
            channel_id=dest_channel,
            error=str(err.response.get("error", "slack_api_error")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        )
        return False
    return True

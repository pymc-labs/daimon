"""Set charge and Decline on a pending support post, at parity with Discord.

Anyone acting in the configured support channel may settle (decision 4);
`settle_from_post` audits it. The requester is never messaged (decision 8):
the post, the form and the settler's confirmation say so. The buttons carry
the request id, so they work after a restart. Settling edits the post: the
final status and settler in place of the pending line, no buttons.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from decimal import Decimal
from typing import Any, Final, cast

import structlog
from daimon.adapters.slack.click_replies import (
    CLICK_REPLY_ERRORS,
    error_name,
    open_modal,
    post_ephemeral,
)
from daimon.adapters.slack.gating import is_external_interactive
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.support_post import (
    SETTLE_DECLINE_ACTION_ID,
    closed_post,
    status_line,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support import get_support_channel
from daimon.core.stores.support_escalation import SettleOutcome, get_request
from daimon.core.stores.support_rows import SupportEscalationRow
from daimon.core.support import (
    CHARGE_FIELD,
    CLIENT_NOT_NOTIFIED,
    LIMIT_GONE,
    REQUEST_GONE,
    SET_CHARGE,
    SETTLE_ELSEWHERE,
    SupportError,
    already_settled_text,
    format_credits,
    option_text,
    parse_amount,
    settle_range_text,
    settled_text,
)
from daimon.core.support_changes import settle_from_post
from daimon.core.support_routing import settle_channel_ok
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

SETTLE_CALLBACK_ID: Final = "support_settle_charge"
_CHARGE_BLOCK_ID: Final = "support_charge_block"
_CHARGE_INPUT_ID: Final = "support_charge_input"
_FAILED: Final = "That didn't work. Try again."
_SLACK_USER = re.compile(r"[UW][A-Z0-9]+")


@dataclasses.dataclass(frozen=True)
class _Post:
    """The support post a settle acts on, and who is settling."""

    team_id: str
    user_id: str
    channel_id: str
    ts: str


def _settler(by: str | None) -> str | None:
    """A Slack settler as a mention; any other surface's name as stored."""
    return f"<@{by}>" if by and _SLACK_USER.fullmatch(by) else by


async def _settles_here(
    runtime: SlackRuntime, *, team_id: str, channel_id: str, row: SupportEscalationRow
) -> bool:
    """The click is in the channel `row` was posted to, in a workspace that may hold it.

    The channel stamped at posting, so open requests stay settleable there
    after a channel change. The workspace is the request's own, the
    deployment's escalation one, or the one its own channel sits in.
    """
    support = runtime.settings.support
    async with runtime.sessionmaker() as session:
        own = await get_support_channel(session, tenant_id=row.tenant_id)
        if not await settle_channel_ok(
            session,
            row=row,
            channel_id=channel_id,
            parent_id=None,
            default_channel_id=support.slack_escalation_channel_id,
        ):
            return False
    if derive_tenant_uuid(platform="slack", workspace_id=team_id) == row.tenant_id:
        return True
    if support.slack_escalation_team_id == team_id:
        return True
    return own is not None and own.platform == "slack" and own.slack_team_id == team_id


async def _notice(client: AsyncWebClient, post: _Post, text: str, *, error: bool) -> None:
    await post_ephemeral(
        client,
        channel_id=post.channel_id,
        user_id=post.user_id,
        thread_ts=post.ts,
        message_ts=post.ts,
        text=text,
        error=error,
    )


async def _fetch_post(client: AsyncWebClient, post: _Post) -> dict[str, Any] | None:
    resp = await client.conversations_history(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk kwargs
        channel=post.channel_id, latest=post.ts, inclusive=True, limit=1
    )
    messages = cast("list[Any]", resp.get("messages") or [])  # pyright: ignore[reportUnknownMemberType]
    first: object = messages[0] if messages else None
    if not isinstance(first, dict):
        return None
    message = cast("dict[str, Any]", first)
    return message if message.get("ts") == post.ts else None


async def _close_post(
    client: AsyncWebClient,
    post: _Post,
    *,
    before: SupportEscalationRow,
    after: SupportEscalationRow,
    message: dict[str, Any] | None,
) -> None:
    """Edit the post: the final status and settler, no buttons. Best-effort."""
    by = _settler(after.settled_by)
    final = status_line(after)
    try:
        message = message or await _fetch_post(client, post)
        if message is None:
            log.warning("support.post_edit_failed", escalation_id=str(after.id), error="missing")
            return
        text, blocks = closed_post(
            message,
            pending_line=status_line(before),
            final_line=f"{final} by {by}" if by else final,
        )
        await client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk kwargs
            channel=post.channel_id, ts=post.ts, text=text, blocks=blocks
        )
    except CLICK_REPLY_ERRORS as err:
        # The settle is committed; a stale post only shows old buttons, which
        # answer "Already charged" on the next click.
        log.warning("support.post_edit_failed", escalation_id=str(after.id), error=error_name(err))


async def _finish(
    client: AsyncWebClient,
    post: _Post,
    *,
    before: SupportEscalationRow,
    outcome: SettleOutcome,
    message: dict[str, Any] | None = None,
) -> None:
    after = outcome.row
    if after is None:
        await _notice(client, post, REQUEST_GONE, error=True)
        return
    if outcome.status == "limit_gone":
        await _notice(client, post, LIMIT_GONE, error=True)
        return
    if outcome.status == "not_here":
        await _notice(client, post, SETTLE_ELSEWHERE, error=True)
        return
    if outcome.status in ("invalid", "over_balance"):
        text = settle_range_text(after.bounds, counted=after.counted, available=outcome.available)
        await _notice(client, post, text, error=True)
        return
    if after.status != "pending":
        await _close_post(client, post, before=before, after=after, message=message)
    if outcome.status == "settled":
        text = settled_text(charge=after.charge)
    elif outcome.status == "declined":
        text = settled_text(charge=None)
    else:
        by = _settler(after.settled_by)
        text = already_settled_text(status=after.status, charge=after.charge, by=by)
    await _notice(client, post, text, error=False)


async def _settle(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    post: _Post,
    *,
    row: SupportEscalationRow,
    charge: Decimal | None,
    message: dict[str, Any] | None = None,
) -> None:
    """Settle at `charge` (None declines) and say how it went."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=post.team_id)
    async with runtime.sessionmaker() as session:
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=post.user_id
        )
    outcome = await settle_from_post(
        runtime.sessionmaker,
        escalation_id=row.id,
        charge=charge,
        settled_by=post.user_id,
        settled_by_account_id=None if principal is None else principal.account_id,
        default_allowance=runtime.settings.support.credits_per_user,
        channel_id=post.channel_id,
        parent_id=None,
        default_channel_id=runtime.settings.support.slack_escalation_channel_id,
    )
    log.info("support.request_settled", escalation_id=str(row.id), status=outcome.status)
    await _finish(client, post, before=row, outcome=outcome, message=message)


def _escalation_id(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(hex=str(value or ""))
    except ValueError:
        return None


def settle_modal(row: SupportEscalationRow, *, channel_id: str, message_ts: str) -> dict[str, Any]:
    """The charge, prefilled with the hold; no note to the requester (decision 8)."""
    terms = [escape_mrkdwn(option_text(row.option))] if row.option is not None else []
    meta = {"escalation_id": row.id.hex, "channel_id": channel_id, "message_ts": message_ts}
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join([*terms, status_line(row)])},
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": CLIENT_NOT_NOTIFIED}]},
        {
            "type": "input",
            "block_id": _CHARGE_BLOCK_ID,
            "label": {"type": "plain_text", "text": CHARGE_FIELD},
            "hint": {
                "type": "plain_text",
                "text": settle_range_text(row.bounds, counted=row.counted, available=None),
            },
            "element": {
                "type": "plain_text_input",
                "action_id": _CHARGE_INPUT_ID,
                "initial_value": format_credits(row.hold),
                "max_length": 12,
            },
        },
    ]
    return finish_modal(
        title=SET_CHARGE,
        blocks=blocks,
        private_metadata=json.dumps(meta, separators=(",", ":")),
        callback_id=SETTLE_CALLBACK_ID,
        close="Cancel",
        submit=SET_CHARGE,
    )


async def handle_settle_click(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Set charge opens the form; Decline settles. Checks the request now, then its channel."""
    if is_external_interactive(payload):
        log.info("support.external_click_rejected")
        return
    team: dict[str, Any] = payload.get("team") or {}
    user: dict[str, Any] = payload.get("user") or {}
    channel: dict[str, Any] = payload.get("channel") or {}
    container: dict[str, Any] = payload.get("container") or {}
    message: dict[str, Any] = payload.get("message") or {}
    actions: list[dict[str, Any]] = payload.get("actions") or []
    action = actions[0] if actions else {}
    post = _Post(
        team_id=str(team.get("id") or ""),
        user_id=str(user.get("id") or ""),
        channel_id=str(channel.get("id") or container.get("channel_id") or ""),
        ts=str(container.get("message_ts") or message.get("ts") or ""),
    )
    trigger_id = str(payload.get("trigger_id") or "")
    if not (post.team_id and post.user_id and post.channel_id and post.ts):
        return
    client = await resolve_web_client(runtime, team_id=post.team_id)
    if client is None:
        return
    try:
        escalation_id = _escalation_id(action.get("value"))
        row = None
        if escalation_id is not None:
            async with runtime.sessionmaker() as session:
                row = await get_request(session, escalation_id=escalation_id)
        if row is None:
            await _notice(client, post, REQUEST_GONE, error=True)
        elif not await _settles_here(
            runtime, team_id=post.team_id, channel_id=post.channel_id, row=row
        ):
            await _notice(client, post, SETTLE_ELSEWHERE, error=True)
        elif row.status != "pending":
            outcome = SettleOutcome(status="already_settled", row=row)
            await _finish(client, post, before=row, outcome=outcome, message=message)
        elif action.get("action_id") == SETTLE_DECLINE_ACTION_ID:
            await _settle(runtime, client, post, row=row, charge=None, message=message)
        else:
            view = settle_modal(row, channel_id=post.channel_id, message_ts=post.ts)
            if await open_modal(client, trigger_id=trigger_id, view=view) is None:
                await _notice(client, post, _FAILED, error=True)
    except (SQLAlchemyError, OSError, *CLICK_REPLY_ERRORS) as err:
        # OSError: asyncpg raises a refused connection raw, outside SQLAlchemy's.
        log.warning("support.settle_failed", error=error_name(err))
        await _notice(client, post, _FAILED, error=True)


@dataclasses.dataclass(frozen=True)
class SettleSubmission:
    """The pure pre-ack read of the Set charge form; field errors keep it open."""

    proceed: bool
    response_payload: dict[str, Any] | None
    team_id: str
    user_id: str
    channel_id: str
    message_ts: str
    escalation_id: uuid.UUID | None
    charge: Decimal | None


def evaluate_settle_submission(payload: dict[str, Any]) -> SettleSubmission:
    """Pure (no I/O): the routing handles and the parsed charge, or the field error."""
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
    block: dict[str, Any] = values.get(_CHARGE_BLOCK_ID) or {}
    element: dict[str, Any] = block.get(_CHARGE_INPUT_ID) or {}
    base = SettleSubmission(
        proceed=False,
        response_payload=None,
        team_id=str(team.get("id") or ""),
        user_id=str(user.get("id") or ""),
        channel_id=str(meta.get("channel_id") or ""),
        message_ts=str(meta.get("message_ts") or ""),
        escalation_id=_escalation_id(meta.get("escalation_id")),
        charge=None,
    )
    if is_external_interactive(payload):
        log.info("support.external_submission_rejected")
        return base
    if not (base.team_id and base.user_id and base.channel_id and base.message_ts):
        return base
    if base.escalation_id is None:
        return base
    try:
        charge = parse_amount(
            str(element.get("value") or ""), name=CHARGE_FIELD.lower(), allow_zero=True
        )
    except SupportError as err:
        errors = {_CHARGE_BLOCK_ID: str(err)}
        return dataclasses.replace(
            base, response_payload={"response_action": "errors", "errors": errors}
        )
    return dataclasses.replace(base, proceed=True, charge=charge)


async def run_settle_submission(runtime: SlackRuntime, submission: SettleSubmission) -> None:
    """Settle at the typed charge, then edit the post and confirm to the settler."""
    s = submission
    if s.escalation_id is None or s.charge is None:
        return
    client = await resolve_web_client(runtime, team_id=s.team_id)
    if client is None:
        return
    post = _Post(team_id=s.team_id, user_id=s.user_id, channel_id=s.channel_id, ts=s.message_ts)
    try:
        async with runtime.sessionmaker() as session:
            row = await get_request(session, escalation_id=s.escalation_id)
        if row is None:
            await _notice(client, post, REQUEST_GONE, error=True)
            return
        if not await _settles_here(runtime, team_id=s.team_id, channel_id=s.channel_id, row=row):
            await _notice(client, post, SETTLE_ELSEWHERE, error=True)
            return
        await _settle(runtime, client, post, row=row, charge=s.charge)
    except (SQLAlchemyError, OSError, *CLICK_REPLY_ERRORS) as err:
        # OSError: asyncpg raises a refused connection raw, outside SQLAlchemy's.
        log.warning("support.settle_failed", error=error_name(err))
        await _notice(client, post, _FAILED, error=True)


__all__ = [
    "SETTLE_CALLBACK_ID",
    "SettleSubmission",
    "evaluate_settle_submission",
    "handle_settle_click",
    "run_settle_submission",
    "settle_modal",
]

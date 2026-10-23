"""Redeem a promo code from the Slack /billing modal.

``billing_redeem_open`` pushes a one-field modal over the panel. Its
``view_submission`` is evaluated purely before the ack (an empty code is a field
error; anything else acks with a "Redeeming…" update), then the background
side reads admin rights live and redeems through ``daimon.core.promo_credit``:
all codes require a platform admin; a channel code credits the channel
/billing ran in (never a DM or group DM, read from Slack's own
description of the conversation). It either shows the result and
refreshes the panel underneath, or reopens the form with the refusal so the
person can retry.
"""

from __future__ import annotations

import dataclasses
import functools
import json
from datetime import UTC, datetime
from typing import Any, cast

import aiohttp
import structlog
from daimon.adapters.slack.admin import resolve_is_platform_admin
from daimon.adapters.slack.billing_panel.names import name_shown_spenders
from daimon.adapters.slack.billing_panel.views import build_billing_view, slack_time
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.billing_panel import load_billing_snapshot, month_start, redeemed_text
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.panel_audit import record_panel_write
from daimon.core.promo_codes import describe_refusal
from daimon.core.promo_credit import (
    PromoChannel,
    PromoRedeemRefused,
    PromoRedeemResult,
    redeem_promo_code,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

REDEEM_CALLBACK_ID = "billing_redeem"
_CODE_BLOCK_ID = "billing_redeem_code"
_CODE_INPUT_ID = "code"
_TITLE = {"type": "plain_text", "text": "Redeem a promo code"}


def _text_view(text: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "title": _TITLE,
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


def _metadata(view: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(str(view.get("private_metadata") or "") or "{}")
    except json.JSONDecodeError:
        return {}
    return cast("dict[str, Any]", meta) if isinstance(meta, dict) else {}


def build_redeem_modal(
    *, root_view_id: str, channel_id: str = "", error: str | None = None
) -> dict[str, Any]:
    """The code form. ``error`` is a refusal from the last attempt; ``channel_id``
    is the channel /billing ran in, for the panel refresh."""
    blocks: list[dict[str, Any]] = []
    if error is not None:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ {error}"}})
    blocks.append(
        {
            "type": "input",
            "block_id": _CODE_BLOCK_ID,
            "label": {"type": "plain_text", "text": "Promo code"},
            "element": {
                "type": "plain_text_input",
                "action_id": _CODE_INPUT_ID,
                "placeholder": {"type": "plain_text", "text": "XXXXX-XXXXX-XXXXX-XXXXX"},
                "max_length": 100,
            },
        }
    )
    return {
        "type": "modal",
        "callback_id": REDEEM_CALLBACK_ID,
        "title": _TITLE,
        "submit": {"type": "plain_text", "text": "Redeem"},
        "close": {"type": "plain_text", "text": "Back"},
        "private_metadata": json.dumps({"root_view_id": root_view_id, "channel_id": channel_id}),
        "blocks": blocks,
    }


def redeem_result_text(result: PromoRedeemResult) -> str:
    """What a redemption did, in Slack mrkdwn. Pure."""
    if isinstance(result, PromoRedeemRefused):
        return describe_refusal(result.reason)
    return redeemed_text(
        result,
        bold=lambda text: f"*{text}*",
        when=slack_time,
        channel=lambda channel_id: f"<#{channel_id}>",
        owner="the workspace's",
    )


@dataclasses.dataclass(frozen=True)
class RedeemDecision:
    """Pure pre-ack evaluation of a ``billing_redeem`` submission."""

    proceed: bool
    response_payload: dict[str, Any]
    code: str
    view_id: str
    root_view_id: str
    channel_id: str = ""


def evaluate_redeem_submission(payload: dict[str, Any]) -> RedeemDecision:
    """No I/O: read the code and pick the ack. An empty code stays on the form."""
    view: dict[str, Any] = payload.get("view") or {}
    meta = _metadata(view)
    channel_id = str(meta.get("channel_id") or "")
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    block: dict[str, Any] = values.get(_CODE_BLOCK_ID) or {}
    element: dict[str, Any] = block.get(_CODE_INPUT_ID) or {}
    code = str(element.get("value") or "")
    view_id = str(view.get("id") or "")
    root_view_id = str(meta.get("root_view_id") or "")
    if not code.strip():
        return RedeemDecision(
            proceed=False,
            response_payload={
                "response_action": "errors",
                "errors": {_CODE_BLOCK_ID: "Enter a promo code."},
            },
            code="",
            view_id=view_id,
            root_view_id=root_view_id,
            channel_id=channel_id,
        )
    return RedeemDecision(
        proceed=True,
        response_payload={"response_action": "update", "view": _text_view("⏳ Redeeming…")},
        code=code,
        view_id=view_id,
        root_view_id=root_view_id,
        channel_id=channel_id,
    )


async def promo_channel(client: AsyncWebClient, channel_id: str) -> PromoChannel | None:
    """The channel a channel code credits, or None for a DM, a group DM or no channel.

    The conversation's type comes from Slack (``conversations.info``), not its
    id: a group DM can carry a ``G`` id like a private channel. Anything Slack
    won't describe as a public or private channel is refused (fail closed),
    which never affects a server-wide code.
    """
    if not channel_id or channel_id.startswith("D"):
        return None
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
    except (SlackApiError, aiohttp.ClientError, TimeoutError) as exc:
        # Unknown is not a channel; a server-wide code never needs one.
        log.info("slack.billing_redeem.channel_unreadable", channel_id=channel_id, exc_info=exc)
        return None
    channel = cast("dict[str, Any]", info.get("channel") or {})
    if channel.get("is_im") or channel.get("is_mpim"):
        return None
    if not (channel.get("is_channel") or channel.get("is_group")):
        return None
    return PromoChannel(platform="slack", channel_id=channel_id)


async def handle_redeem_open(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """``billing_redeem_open`` click: push the code form; the submit decides what a code may do."""
    team: dict[str, Any] = payload.get("team") or {}
    view: dict[str, Any] = payload.get("view") or {}
    team_id = str(team.get("id") or "")
    trigger_id = str(payload.get("trigger_id") or "")
    root_view_id = str(view.get("id") or "")
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.billing_redeem.no_token", team_id=team_id)
        return
    try:
        # views.push, not views.open: the button lives in the open /billing modal.
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs
            trigger_id=trigger_id,
            view=build_redeem_modal(
                root_view_id=root_view_id, channel_id=str(_metadata(view).get("channel_id") or "")
            ),
        )
    except SlackApiError as exc:
        log.error("slack.billing_redeem_open_failed", team_id=team_id, exc_info=exc)
        capture_exception_with_scope(exc)


async def _refresh_panel(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    root_view_id: str,
    channel_id: str,
    now: datetime,
    is_admin: bool,
) -> None:
    """Redraw the /billing panel under the form. A failure leaves the success reply alone."""
    since = month_start(now)
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    try:
        async with runtime.sessionmaker() as session:
            state = await load_billing_snapshot(
                session,
                tenant_id=tenant_id,
                platform_user_id=user_id,
                is_admin=is_admin,
                since=since,
                now=now,
                platform="slack",
                channel_id=channel_id or None,
            )
        state = await name_shown_spenders(
            client, state, sessionmaker=runtime.sessionmaker, tenant_id=tenant_id
        )
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=root_view_id,
            view=build_billing_view(state, now=now, since=since, channel_id=channel_id),
        )
    except (DaimonError, SlackApiError, SQLAlchemyError) as exc:
        log.warning("slack.billing_redeem_refresh_failed", team_id=team_id, exc_info=exc)
        capture_exception_with_scope(exc)


async def run_redeem_submission(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    decision: RedeemDecision,
) -> None:
    """Background side of a ``billing_redeem`` submission (after the ack)."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    audit = functools.partial(
        record_panel_write,
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id=user_id,
        op="promo_redeem",
        target_channel_id=decision.channel_id or None,
    )
    audited = False
    try:
        is_admin = await resolve_is_platform_admin(client, user_id=user_id)
        async with runtime.sessionmaker() as session, session.begin():
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant_id, platform="slack", external_id=user_id
            )
        now = datetime.now(UTC)
        result = await redeem_promo_code(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            account_id=principal.account_id,
            code=decision.code,
            now=now,
            channel=await promo_channel(client, decision.channel_id),
            is_admin=is_admin,
        )
        if isinstance(result, PromoRedeemRefused):
            audited = True
            await audit(outcome="denied", reason=f"promo:{result.reason}")
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=decision.view_id,
                view=build_redeem_modal(
                    root_view_id=decision.root_view_id,
                    channel_id=decision.channel_id,
                    error=redeem_result_text(result),
                ),
            )
            return
        audited = True
        await audit(outcome="allowed", reason="completed")
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=decision.view_id, view=_text_view(redeem_result_text(result))
        )
        if decision.root_view_id:
            await _refresh_panel(
                runtime,
                client,
                team_id=team_id,
                user_id=user_id,
                root_view_id=decision.root_view_id,
                channel_id=decision.channel_id,
                now=now,
                is_admin=is_admin,
            )
    except (DaimonError, SlackApiError, SQLAlchemyError) as exc:
        request_id = generate_request_id()
        log.error(
            "slack.billing_redeem_failed",
            team_id=team_id,
            user_id=user_id,
            request_id=request_id,
            exc_info=exc,
        )
        capture_exception_with_scope(exc)
        if not audited:  # a failed redeem, not a failed reply after one
            await audit(outcome="error", reason="failed")
        await surface_command_error(
            client,
            exc,
            request_id=request_id,
            title="Redeem a promo code",
            view_id=decision.view_id,
            channel_id="",
            user_id=user_id,
        )

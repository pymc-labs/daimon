"""Redeem a promo code from the Slack /billing modal. Admin only.

``billing_redeem_open`` pushes a one-field modal over the panel. Its
``view_submission`` is evaluated purely before the ack (an empty code is a field
error; anything else acks with a "Redeeming…" update), then the background
side re-checks admin, redeems through ``daimon.core.promo_credit`` and either
shows the result and refreshes the panel underneath, or reopens the form with
the refusal so the admin can retry.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.billing_panel.read import load_billing_snapshot
from daimon.adapters.slack.billing_panel.views import build_billing_view, slack_time
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.promo_codes import describe_refusal
from daimon.core.promo_credit import PromoRedeemRefused, PromoRedeemResult, redeem_promo_code
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

REDEEM_CALLBACK_ID = "billing_redeem"
_CODE_BLOCK_ID = "billing_redeem_code"
_CODE_INPUT_ID = "code"
_TITLE = {"type": "plain_text", "text": "Redeem a promo code"}
_ADMIN_ONLY = "Only workspace admins can redeem promo codes."


def _text_view(text: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "title": _TITLE,
        "close": {"type": "plain_text", "text": "Back"},
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }


def build_redeem_modal(*, root_view_id: str, error: str | None = None) -> dict[str, Any]:
    """The code form. ``error`` is a refusal from the last attempt."""
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
        "private_metadata": json.dumps({"root_view_id": root_view_id}),
        "blocks": blocks,
    }


def redeem_result_text(result: PromoRedeemResult) -> str:
    """What a redemption did, in Slack mrkdwn. Pure."""
    if isinstance(result, PromoRedeemRefused):
        return describe_refusal(result.reason)
    amount = f"*${result.amount_usd:,.2f}*"
    if result.credit_ends_at is None:
        return f"🎟️ Redeemed {amount} of credit. Balance: *${result.balance_usd:,.2f}*."
    window = f"until {slack_time(result.credit_ends_at)}"
    if not result.granted and result.credit_starts_at is not None:
        window = f"from {slack_time(result.credit_starts_at)} {window}"
    return (
        f"🎟️ Redeemed {amount} of timed credit, usable {window}. "
        "It is spent before other credit, and what is left then expires."
    )


@dataclasses.dataclass(frozen=True)
class RedeemDecision:
    """Pure pre-ack evaluation of a ``billing_redeem`` submission."""

    proceed: bool
    response_payload: dict[str, Any]
    code: str
    view_id: str
    root_view_id: str


def evaluate_redeem_submission(payload: dict[str, Any]) -> RedeemDecision:
    """No I/O: read the code and pick the ack. An empty code stays on the form."""
    view: dict[str, Any] = payload.get("view") or {}
    try:
        meta: dict[str, Any] = json.loads(str(view.get("private_metadata") or "") or "{}")
    except json.JSONDecodeError:
        meta = {}
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
        )
    return RedeemDecision(
        proceed=True,
        response_payload={"response_action": "update", "view": _text_view("⏳ Redeeming…")},
        code=code,
        view_id=view_id,
        root_view_id=root_view_id,
    )


async def handle_redeem_open(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """``billing_redeem_open`` click: push the code form for a live admin."""
    team: dict[str, Any] = payload.get("team") or {}
    user: dict[str, Any] = payload.get("user") or {}
    view: dict[str, Any] = payload.get("view") or {}
    team_id = str(team.get("id") or "")
    user_id = str(user.get("id") or "")
    trigger_id = str(payload.get("trigger_id") or "")
    root_view_id = str(view.get("id") or "")
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.billing_redeem.no_token", team_id=team_id)
        return
    try:
        is_admin = await resolve_is_admin(client, user_id=user_id)
        # views.push, not views.open: the button lives in the open /billing modal.
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs
            trigger_id=trigger_id,
            view=build_redeem_modal(root_view_id=root_view_id)
            if is_admin
            else _text_view(_ADMIN_ONLY),
        )
    except SlackApiError as exc:
        log.error("slack.billing_redeem_open_failed", team_id=team_id, exc_info=exc)
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
    try:
        if not await resolve_is_admin(client, user_id=user_id):
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=decision.view_id, view=_text_view(_ADMIN_ONLY)
            )
            return
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
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
        )
        if isinstance(result, PromoRedeemRefused):
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=decision.view_id,
                view=build_redeem_modal(
                    root_view_id=decision.root_view_id, error=redeem_result_text(result)
                ),
            )
            return
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=decision.view_id, view=_text_view(redeem_result_text(result))
        )
        if decision.root_view_id:
            since = datetime(now.year, now.month, 1, tzinfo=UTC)
            async with runtime.sessionmaker() as session:
                state = await load_billing_snapshot(
                    session,
                    team_id=team_id,
                    platform_user_id=user_id,
                    is_admin=True,
                    since=since,
                    now=now,
                )
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=decision.root_view_id,
                view=build_billing_view(state, now=now, since=since),
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
        await surface_command_error(
            client,
            exc,
            request_id=request_id,
            title="Redeem a promo code",
            view_id=decision.view_id,
            channel_id="",
            user_id=user_id,
        )

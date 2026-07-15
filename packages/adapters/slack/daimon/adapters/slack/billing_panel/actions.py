"""Slack /billing slash command handler + billing_topup block_actions handler.

Follows ack-first Socket Mode discipline (S1) — callers ACK before spawning
these functions as background tasks. These are the background side of the ack.

Pattern sequence for slash command:
  1. resolve_web_client → get per-event AsyncWebClient
  2. views.open(loading) → capture view_id
  3. resolve_is_admin → bool (D-02 fail-closed)
  4. load_billing_snapshot → BillingPanelState
  5. views.update(view_id, billing modal)

Pattern sequence for billing_topup block_action:
  1. resolve_web_client → client
  2. Re-verify is_admin (D-02 — re-check at click time, not cached)
  3. Validate amount ∈ preset set (T-82-10)
  4. get_or_create_platform_principal → account_id
  5. create_checkout(http_client, …) → url
  6. chat_postEphemeral with "<url|Complete payment>" link (D-01)

Error boundary (S3): catches DaimonError | httpx.HTTPStatusError | SlackApiError
at the handler level; logs + captures to Sentry. Never stripe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.billing_panel.checkout import create_checkout
from daimon.adapters.slack.billing_panel.read import load_billing_snapshot
from daimon.adapters.slack.billing_panel.views import build_billing_container, build_loading_view
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.errors import SlackApiError

log = structlog.get_logger()

# Preset top-up amounts; any amount not in this set is rejected (T-82-10)
_PRESET_AMOUNTS: frozenset[int] = frozenset({10, 25, 50, 100})


async def handle_billing_command(
    runtime: SlackRuntime,
    payload: dict[str, Any],
) -> None:
    """Handle the /billing slash command.

    Opens a loading modal (D-06), resolves is_admin in the background (D-03),
    reads usage/ledger/cap aggregates, then updates the modal with the billing view.

    Args:
        runtime: Injected SlackRuntime (settings, sessionmaker).
        payload: Verified slash_commands payload dict from Socket Mode.
    """
    team_id: str = payload.get("team_id") or payload.get("team", {}).get("id") or ""
    user_id: str = payload.get("user_id") or payload.get("user", {}).get("id") or ""
    trigger_id: str = payload.get("trigger_id") or ""

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.billing_command.no_token", team_id=team_id)
        return

    try:
        # Open loading modal immediately (D-06)
        open_resp = await client.views_open(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs
            trigger_id=trigger_id,
            view=build_loading_view(),
        )
        # SlackResponse subscript is untyped — extract the view dict explicitly
        open_view: dict[str, str] = open_resp["view"]  # pyright: ignore[reportUnknownVariableType, reportAssignmentType, reportUnknownMemberType]  # SlackResponse untyped
        view_id: str = open_view.get("id") or ""

        # Resolve admin status (D-02 fail-closed)
        is_admin = await resolve_is_admin(client, user_id=user_id)

        # Load billing snapshot from DB
        now = datetime.now(UTC)
        since = datetime(now.year, now.month, 1, tzinfo=UTC)
        async with runtime.sessionmaker() as session:
            state = await load_billing_snapshot(
                session,
                team_id=team_id,
                platform_user_id=user_id,
                is_admin=is_admin,
                since=since,
            )

        blocks = build_billing_container(state, now=now, since=since)
        billing_view: dict[str, Any] = {
            "type": "modal",
            "title": {"type": "plain_text", "text": "Billing"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": blocks,
        }
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,
            view=billing_view,
        )

    except (DaimonError, SlackApiError) as exc:
        log.error(
            "slack.billing_command_failed",
            team_id=team_id,
            user_id=user_id,
            exc_info=exc,
        )
        capture_exception_with_scope(exc)


async def handle_topup_select(
    runtime: SlackRuntime,
    payload: dict[str, Any],
    *,
    _http_client: httpx.AsyncClient | None = None,
) -> None:
    """Handle the billing_topup static_select block_action.

    Re-verifies admin status at click time (D-02), validates the selected amount
    against the preset set (T-82-10), mints an internal token, POSTs to
    /billing/checkout, and replies with an ephemeral "<url|Complete payment>" link.

    Args:
        runtime:      Injected SlackRuntime.
        payload:      block_actions payload dict.
        _http_client: Optional injected AsyncClient for testing (None = create one).
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    team_id: str = team_info.get("id") or ""
    user_info: dict[str, Any] = payload.get("user") or {}
    user_id: str = user_info.get("id") or ""
    # channel comes from block_actions container (may be absent for modal actions)
    container: dict[str, Any] = payload.get("container") or {}
    channel: str = container.get("channel_id") or ""

    # Extract selected amount from the first action's selected_option value
    actions: list[dict[str, Any]] = payload.get("actions") or []
    selected_option: dict[str, Any] = (actions[0].get("selected_option") or {}) if actions else {}
    raw_value: str = selected_option.get("value") or ""

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.billing_topup.no_token", team_id=team_id)
        return

    try:
        # D-02: Re-verify admin at click time (not cached)
        is_admin = await resolve_is_admin(client, user_id=user_id)
        if not is_admin:
            log.warning(
                "slack.billing_topup.non_admin_refused",
                team_id=team_id,
                user_id=user_id,
            )
            return

        # T-82-10: Validate amount against preset set
        try:
            amount = int(raw_value)
        except (ValueError, TypeError):
            log.warning(
                "slack.billing_topup.invalid_amount",
                team_id=team_id,
                raw_value=raw_value,
            )
            return
        if amount not in _PRESET_AMOUNTS:
            log.warning(
                "slack.billing_topup.amount_not_in_preset",
                team_id=team_id,
                amount=amount,
            )
            return

        # Resolve Slack principal's account_id for this workspace
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        async with runtime.sessionmaker() as session, session.begin():
            principal = await get_or_create_platform_principal(
                session,
                tenant_id=tenant_id,
                platform="slack",
                external_id=user_id,
            )

        # Mint token + POST /billing/checkout
        async with _http_client or httpx.AsyncClient() as http_client:
            url = await create_checkout(
                http_client,
                settings=runtime.settings.mcp,
                account_id=principal.account_id,
                amount=amount,
            )

        # D-01: Ephemeral mrkdwn link (not a url_button — those can't be ephemeral)
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel,
            user=user_id,
            text=f"<{url}|Complete payment>",
        )

    except (DaimonError, httpx.HTTPStatusError, SlackApiError) as exc:
        log.error(
            "slack.billing_topup_failed",
            team_id=team_id,
            user_id=user_id,
            exc_info=exc,
        )
        capture_exception_with_scope(exc)

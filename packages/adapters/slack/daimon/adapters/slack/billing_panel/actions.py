"""Slack /billing slash command handler + billing_topup block_actions handler.

Follows ack-first Socket Mode discipline (S1) — callers ACK before spawning
these functions as background tasks. These are the background side of the ack.

Pattern sequence for slash command:
  1. resolve_web_client → get per-event AsyncWebClient
  2. views.open(loading) → capture view_id
  3. resolve_is_admin → bool (fail-closed)
  4. load_billing_snapshot → BillingPanelState
  5. views.update(view_id, billing modal)

Pattern sequence for billing_topup block_action:
  1. resolve_web_client → client
  2. Re-verify is_admin (re-check at click time, not cached)
  3. Validate amount ∈ preset set (T-82-10)
  4. get_or_create_platform_principal → account_id
  5. create_checkout(http_client, …) → url
  6. chat_postEphemeral with "<url|Complete payment>" link
  7. On failure, views.update the open modal with a static "not configured" message
     instead of leaving the dropdown silently dead

Error boundary (S3): catches DaimonError | httpx.HTTPStatusError | SlackApiError
at the handler level; logs + captures to Sentry, then answers the open modal
via views.update. Never stripe.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.billing_panel.names import lookup_name, name_shown_spenders
from daimon.adapters.slack.billing_panel.views import (
    EXPIRY_OPEN_ACTION_ID,
    LOOKUP_ACTION_ID,
    Lookup,
    build_billing_view,
    build_expiry_view,
    build_loading_view,
)
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.billing_panel import (
    LOOK_UP,
    TOPUP_AMOUNTS,
    create_checkout,
    load_billing_snapshot,
    lookup_line,
    month_start,
    stored_name_labels,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.promo_credit import get_active_timed_credit
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.usage_events import (
    cost_for_user_in_tenant_since,
    turn_count_for_user_in_tenant_since,
)
from slack_sdk.errors import SlackApiError
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()


async def handle_billing_command(
    runtime: SlackRuntime,
    payload: dict[str, Any],
) -> None:
    """Handle the /billing slash command.

    Opens a loading modal, resolves is_admin in the background,
    reads usage/ledger/cap aggregates, then updates the modal with the billing view.

    Args:
        runtime: Injected SlackRuntime (settings, sessionmaker).
        payload: Verified slash_commands payload dict from Socket Mode.
    """
    team_id: str = payload.get("team_id") or payload.get("team", {}).get("id") or ""
    user_id: str = payload.get("user_id") or payload.get("user", {}).get("id") or ""
    trigger_id: str = payload.get("trigger_id") or ""
    channel_id: str = payload.get("channel_id") or ""

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.billing_command.no_token", team_id=team_id)
        return

    view_id: str = ""
    try:
        # Open loading modal immediately
        open_resp = await client.views_open(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs
            trigger_id=trigger_id,
            view=build_loading_view(),
        )
        # SlackResponse subscript is untyped — extract the view dict explicitly
        open_view: dict[str, str] = open_resp["view"]  # pyright: ignore[reportUnknownVariableType, reportAssignmentType, reportUnknownMemberType]  # SlackResponse untyped
        view_id = open_view.get("id") or ""

        # Resolve admin status (fail-closed)
        is_admin = await resolve_is_admin(client, user_id=user_id)

        # Load billing snapshot from DB
        now = datetime.now(UTC)
        since = month_start(now)
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        async with runtime.sessionmaker() as session:
            state = await load_billing_snapshot(
                session,
                tenant_id=tenant_id,
                platform_user_id=user_id,
                is_admin=is_admin,
                since=since,
                platform="slack",
                channel_id=channel_id or None,
                now=now,
            )
        state = await name_shown_spenders(
            client, state, sessionmaker=runtime.sessionmaker, tenant_id=tenant_id
        )

        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,
            view=build_billing_view(state, now=now, since=since, channel_id=channel_id),
        )

    except (DaimonError, SlackApiError, InvalidToken, SQLAlchemyError) as exc:
        request_id = generate_request_id()
        log.error(
            "slack.billing_command_failed",
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
            title="Billing",
            view_id=view_id,
            channel_id=channel_id,
            user_id=user_id,
        )


async def handle_topup_select(
    runtime: SlackRuntime,
    payload: dict[str, Any],
    *,
    _http_client: httpx.AsyncClient | None = None,
) -> None:
    """Handle the billing_topup static_select block_action.

    Re-verifies admin status at click time, validates the selected amount
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
    view_info: dict[str, Any] = payload.get("view") or {}
    view_id: str = str(view_info.get("id") or "")

    # Extract selected amount from the first action's selected_option value
    actions: list[dict[str, Any]] = payload.get("actions") or []
    selected_option: dict[str, Any] = (actions[0].get("selected_option") or {}) if actions else {}
    raw_value: str = selected_option.get("value") or ""

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.billing_topup.no_token", team_id=team_id)
        return

    try:
        # Re-verify admin at click time (not cached)
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
        if amount not in TOPUP_AMOUNTS:
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

        # Ephemeral mrkdwn link (not a url_button — those can't be ephemeral)
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
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Billing"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Payments aren't configured for this workspace. "
                                "Ask an operator about a manual credit top-up."
                            ),
                        },
                    }
                ],
            },
        )


LOOKUP_ADMIN_ONLY = "Only workspace admins can look up a person's spend."


async def handle_panel_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """The /billing modal's "Expiry dates" button and "Look up a person" picker.

    "Expiry dates" pushes the timed credit's end dates for anyone. A pick in
    "Look up a person" re-checks admin at pick time, reads fresh and redraws the
    panel with that person's spend under the picker.
    """
    team: dict[str, Any] = payload.get("team") or {}
    user: dict[str, Any] = payload.get("user") or {}
    open_view: dict[str, Any] = payload.get("view") or {}
    team_id = str(team.get("id") or "")
    user_id = str(user.get("id") or "")
    trigger_id = str(payload.get("trigger_id") or "")
    view_id = str(open_view.get("id") or "")
    actions: list[dict[str, Any]] = payload.get("actions") or []
    action: dict[str, Any] = actions[0] if actions else {}
    action_id = str(action.get("action_id") or "")
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.billing_panel_action.no_token", team_id=team_id)
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    now = datetime.now(UTC)
    since = month_start(now)
    try:
        if action_id == EXPIRY_OPEN_ACTION_ID:
            async with runtime.sessionmaker() as session:
                credits = await get_active_timed_credit(session, tenant_id=tenant_id, now=now)
            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=trigger_id, view=build_expiry_view(credits)
            )
            return
        if action_id != LOOKUP_ACTION_ID:
            return
        if not await resolve_is_admin(client, user_id=user_id):
            await client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=trigger_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": LOOK_UP},
                    "close": {"type": "plain_text", "text": "Back"},
                    "blocks": [
                        {"type": "section", "text": {"type": "mrkdwn", "text": LOOKUP_ADMIN_ONLY}}
                    ],
                },
            )
            return
        channel_id = _channel_of(open_view)
        picked = str(action.get("selected_user") or "")
        async with runtime.sessionmaker() as session:
            state = await load_billing_snapshot(
                session,
                tenant_id=tenant_id,
                platform_user_id=user_id,
                is_admin=True,
                since=since,
                now=now,
                platform="slack",
                channel_id=channel_id or None,
            )
            spend = await cost_for_user_in_tenant_since(
                session, tenant_id=tenant_id, platform_user_id=picked, since=since
            )
            turns = await turn_count_for_user_in_tenant_since(
                session, tenant_id=tenant_id, platform_user_id=picked, since=since
            )
            stored = await stored_name_labels(
                session, tenant_id=tenant_id, platform="slack", user_ids=[picked]
            )
        # Both under the same short timeout, side by side.
        state, name = await asyncio.gather(
            name_shown_spenders(
                client, state, sessionmaker=runtime.sessionmaker, tenant_id=tenant_id
            ),
            lookup_name(
                client,
                picked,
                sessionmaker=runtime.sessionmaker,
                tenant_id=tenant_id,
                stored=stored.get(picked),
            ),
        )
        view = build_billing_view(
            state,
            now=now,
            since=since,
            channel_id=channel_id,
            lookup=Lookup(user_id=picked, name=name, line=lookup_line(spend, turns)),
        )
        # The hash makes a slower, earlier lookup lose to a newer one instead of
        # overwriting it.
        try:
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id, hash=open_view.get("hash"), view=view
            )
        except SlackApiError as exc:
            error = str(cast("dict[str, object]", exc.response.data).get("error", ""))  # pyright: ignore[reportUnknownMemberType]
            if error != "hash_conflict":
                raise
            log.info("slack.billing_panel_lookup_superseded", team_id=team_id)
    except (DaimonError, SlackApiError, SQLAlchemyError) as exc:
        log.error(
            "slack.billing_panel_action_failed",
            team_id=team_id,
            action_id=action_id,
            exc_info=exc,
        )
        capture_exception_with_scope(exc)


def _channel_of(view: dict[str, Any]) -> str:
    """The channel /billing ran in, kept in the panel's private metadata."""
    try:
        meta: object = json.loads(str(view.get("private_metadata") or "") or "{}")
    except json.JSONDecodeError:
        return ""
    return (
        str(cast("dict[str, Any]", meta).get("channel_id") or "") if isinstance(meta, dict) else ""
    )

"""Privacy panel slash-command and block-action controller.

`handle_privacy_command` — ack → views.open(loading) → background: resolve account +
load preview → views.update(main view). Read-only; never creates a principal.

`handle_privacy_block_action` — routes:
  - action_id "privacy_delete_open" → resolve account + preview → views.open(delete modal)
  - action_id "privacy_export"      → chat.postEphemeral with held-data summary

I/O shell: catches DaimonError | anthropic.APIError | SlackApiError | SQLAlchemyError
at the boundary (S3). Slow work runs after the Socket Mode ack (S1).

Discord analogs:
  /privacy handler  → PrivacyPanelView.__init__ + _on_delete (privacy_panel/panel.py)
  block actions     → _on_export (panel.py:125-133) + _on_delete (panel.py:134-153)
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import aiohttp
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.privacy_panel.read import load_purge_preview, resolve_privacy_account
from daimon.adapters.slack.privacy_panel.views import (
    build_delete_modal,
    build_disconnect_result_view,
    build_export_result_view,
    build_loading_view,
    build_privacy_main_container,
    summary_line,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.slack_oauth import build_slack_connect_url
from daimon.core.stores.slack_user_tokens import delete_slack_user_token, get_slack_user_token
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

# No-data-on-file view shown when the invoker has no account.
_NO_DATA_BLOCKS: list[dict[str, Any]] = [
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                "*🔒 Privacy*\n"
                "You have no data on file with daimon.\n"
                "Run any other slash command to start fresh."
            ),
        },
    },
]


def _mint_connect_url(runtime: SlackRuntime, *, team_id: str, slack_user_id: str) -> str | None:
    """Signed per-user connect URL, or None when the deploy can't mint one
    (no slack config / no public MCP url). State TTL is 10 minutes — fine for
    a button in a just-opened modal."""
    slack_settings = runtime.settings.slack
    app_root_url = runtime.settings.mcp.app_root_url
    if slack_settings is None or app_root_url is None:
        return None
    return build_slack_connect_url(
        app_root_url=app_root_url,
        signing_secret=slack_settings.signing_secret.get_secret_value(),
        team_id=team_id,
        slack_user_id=slack_user_id,
        now=time.time(),
    )


async def handle_privacy_command(
    runtime: SlackRuntime,
    payload: dict[str, Any],
) -> None:
    """Handle the /privacy slash command (D-06 loading-modal → views.update pattern).

    1. Resolve the per-event web_client (S2).
    2. Immediately views.open a loading modal to capture the trigger_id window.
    3. Resolve account and load purge preview (I/O after ack).
    4. views.update the modal to the full privacy main view.

    Called from app.on_request as a _spawn background task AFTER the Socket Mode ack.
    Catches DaimonError | SlackApiError | SQLAlchemyError at the boundary (S3).
    """
    team_id: str = str(payload.get("team_id") or "")
    user_id: str = str(payload.get("user_id") or "")
    trigger_id: str = str(payload.get("trigger_id") or "")

    try:
        web_client = await resolve_web_client(runtime, team_id=team_id)
        if web_client is None:
            log.warning("privacy.command.dropped.no_token", team_id=team_id)
            return

        # D-06: open loading modal before any slow I/O to stay within trigger_id TTL.
        resp = await web_client.views_open(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id,
            view=build_loading_view(),
        )
        opened_view: dict[str, Any] = resp.get("view") or {}  # pyright: ignore[reportUnknownMemberType]  # slack_sdk SlackResponse.data is a union type
        view_id: str = str(opened_view.get("id") or "")

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

        async with runtime.sessionmaker() as s:
            account_id = await resolve_privacy_account(
                s, tenant_id=tenant_id, platform_user_id=user_id
            )

        if account_id is None:
            await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Privacy"},
                    "blocks": _NO_DATA_BLOCKS,
                },
            )
            return

        preview = await load_purge_preview(sm=runtime.sessionmaker, account_id=account_id)
        async with runtime.sessionmaker() as s:
            token_row = await get_slack_user_token(s, team_id=team_id, slack_user_id=user_id)
        is_slack_connected = token_row is not None
        connect_url = (
            None
            if is_slack_connected
            else _mint_connect_url(runtime, team_id=team_id, slack_user_id=user_id)
        )
        await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,
            view=build_privacy_main_container(
                preview,
                is_slack_connected=is_slack_connected,
                slack_connect_url=connect_url,
                policy_url=str(runtime.settings.privacy_policy_url),
            ),
        )
    except (SlackApiError, SQLAlchemyError) as exc:
        log.error("privacy.command.failed", team_id=team_id, exc_info=exc)


async def handle_privacy_block_action(
    runtime: SlackRuntime,
    payload: dict[str, Any],
) -> None:
    """Handle privacy-related block_actions interactive payloads.

    Routes:
      "privacy_delete_open"     — resolve account + preview, push the delete confirm modal
      "privacy_export"          — push a modal with the held-data summary
      "privacy_slack_disconnect" — delete the slack_user_tokens row, then
                                    best-effort revoke (D-05: delete first)

    Catches DaimonError | SlackApiError | SQLAlchemyError at the boundary (S3).
    """
    team_info: dict[str, Any] = payload.get("team") or {}
    team_id: str = str(team_info.get("id") or "")
    user_info: dict[str, Any] = payload.get("user") or {}
    user_id: str = str(user_info.get("id") or "")
    user_name: str = str(user_info.get("username") or user_info.get("name") or "")
    trigger_id: str = str(payload.get("trigger_id") or "")
    actions: list[dict[str, Any]] = payload.get("actions") or []
    action_id: str = str(actions[0].get("action_id") if actions else "") or ""
    view_block: dict[str, Any] = payload.get("view") or {}
    current_view_id: str = str(view_block.get("id") or "")

    try:
        web_client = await resolve_web_client(runtime, team_id=team_id)
        if web_client is None:
            log.warning("privacy.block_action.dropped.no_token", team_id=team_id)
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

        if action_id == "privacy_delete_open":
            async with runtime.sessionmaker() as s:
                account_id = await resolve_privacy_account(
                    s, tenant_id=tenant_id, platform_user_id=user_id
                )
            if account_id is None:
                log.warning("privacy.delete_open.no_account", user_id=user_id)
                return
            preview = await load_purge_preview(sm=runtime.sessionmaker, account_id=account_id)
            # views.push, not views.open: the button lives in the open privacy
            # modal, and Slack answers ok to views.open from a modal trigger
            # while rendering nothing.
            await web_client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=trigger_id,
                view=build_delete_modal(
                    preview,
                    account_id=account_id,
                    user_name=user_name,
                    view_id=current_view_id,
                ),
            )
        elif action_id == "privacy_export":
            # The panel is a modal — its block_actions payloads carry no
            # channel, so the summary is pushed as a stacked modal rather than
            # posted as an ephemeral channel message.
            async with runtime.sessionmaker() as s:
                account_id = await resolve_privacy_account(
                    s, tenant_id=tenant_id, platform_user_id=user_id
                )
            if account_id is None:
                await web_client.views_push(  # pyright: ignore[reportUnknownMemberType]
                    trigger_id=trigger_id,
                    view=build_export_result_view(summary=None),
                )
                return
            preview = await load_purge_preview(sm=runtime.sessionmaker, account_id=account_id)
            await web_client.views_push(  # pyright: ignore[reportUnknownMemberType]
                trigger_id=trigger_id,
                view=build_export_result_view(summary=summary_line(preview)),
            )
        elif action_id == "privacy_slack_disconnect":
            async with runtime.sessionmaker() as s:
                token_row = await get_slack_user_token(s, team_id=team_id, slack_user_id=user_id)
                if token_row is not None:
                    await delete_slack_user_token(s, team_id=team_id, slack_user_id=user_id)
                await s.commit()
            if token_row is not None:
                # Best-effort revoke — the row is already gone either way.
                # build_multifernet is inside the suppress too: it raises
                # ValueError on an empty/misconfigured key set, and that must
                # not escape and skip the views_update below (the delete
                # already happened; the user still needs to see the result).
                with contextlib.suppress(
                    SlackApiError,
                    InvalidToken,
                    ValueError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                ):
                    fernet = build_multifernet(
                        tuple(k.get_secret_value() for k in runtime.settings.crypto.keys)
                    )
                    user_token = decrypt_token(fernet, token_row.encrypted_token)
                    await AsyncWebClient(token=user_token).auth_revoke()  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                view_id=current_view_id,
                view=build_disconnect_result_view(
                    was_connected=token_row is not None,
                    reconnect_url=_mint_connect_url(
                        runtime, team_id=team_id, slack_user_id=user_id
                    ),
                ),
            )
    except (SlackApiError, SQLAlchemyError) as exc:
        log.error(
            "privacy.block_action.failed",
            team_id=team_id,
            action_id=action_id,
            exc_info=exc,
        )

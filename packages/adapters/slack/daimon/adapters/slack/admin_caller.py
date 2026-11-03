"""Build a caller from Slack's live workspace admin check."""

from __future__ import annotations

from daimon.adapters.slack.admin import resolve_is_platform_admin
from daimon.core.native_admins import AdminCaller
from slack_sdk.web.async_client import AsyncWebClient


async def admin_caller(
    client: AsyncWebClient,
    *,
    user_id: str,
) -> AdminCaller:
    # A rendered/effective admin bit must never become native authority.
    native = await resolve_is_platform_admin(client, user_id=user_id)
    return AdminCaller(platform_user_id=user_id, is_server_admin=native)

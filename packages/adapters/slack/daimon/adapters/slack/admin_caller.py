"""Build a caller from Slack's live workspace admin check."""

from __future__ import annotations

import uuid

from daimon.adapters.slack.admin import resolve_is_platform_admin
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.native_admins import AdminCaller
from slack_sdk.web.async_client import AsyncWebClient


async def admin_caller(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    is_admin: bool,
    fresh: bool = False,
) -> AdminCaller:
    # A rendered/effective admin bit must never become native authority.
    native = await resolve_is_platform_admin(client, user_id=user_id)
    return AdminCaller(platform_user_id=user_id, is_server_admin=native)

"""Build a caller from the native Teams admin-team owner check."""

from __future__ import annotations

import uuid

from daimon.adapters.teams.deployment_admins import admin_team_owners, is_platform_admin
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.native_admins import AdminCaller


async def admin_caller(
    runtime: TeamsRuntime,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    is_admin: bool,
    fresh: bool = False,
) -> AdminCaller:
    settings = runtime.settings.teams
    native = settings is not None and await is_platform_admin(
        settings, admin_team_owners(runtime, fresh=True), user_id=user_id
    )
    return AdminCaller(platform_user_id=user_id, is_server_admin=native)

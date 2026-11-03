"""Build a caller from the native Teams admin-team owner check."""

from __future__ import annotations

from daimon.adapters.teams.deployment_admins import admin_team_owners, is_platform_admin
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.native_admins import AdminCaller


async def admin_caller(
    runtime: TeamsRuntime,
    *,
    user_id: str,
) -> AdminCaller:
    settings = runtime.settings.teams
    native = settings is not None and await is_platform_admin(
        settings, admin_team_owners(runtime, fresh=True), user_id=user_id
    )
    return AdminCaller(platform_user_id=user_id, is_server_admin=native)

"""Microsoft Teams adapter for daimon: channel @mentions and 1:1 chats.

The Teams SDK's `App` owns `/api/messages` and its JWT checks; this package
owns the FastAPI app, health routes and process lifecycle. `parse_inbound`
verifies each message, `TeamsApp` runs it through core's turn pipeline with
one status card edited in place, and the boot sweep marks turns a restart
cut off as interrupted. See docs/teams.md.
"""

from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.http_service import (
    MAX_TEAMS_HTTP_BODY_BYTES,
    TeamsHttpService,
    create_teams_http_service,
)
from daimon.adapters.teams.identity import TeamsInbound, live_tenant_id, parse_inbound
from daimon.adapters.teams.lifecycle import TeamsSender, TeamsTurnLifecycle
from daimon.adapters.teams.runtime import TeamsRuntime, build_runtime

__all__ = [
    "MAX_TEAMS_HTTP_BODY_BYTES",
    "TeamsApp",
    "TeamsHttpService",
    "TeamsInbound",
    "TeamsRuntime",
    "TeamsSender",
    "TeamsTurnLifecycle",
    "build_runtime",
    "create_teams_http_service",
    "live_tenant_id",
    "parse_inbound",
]

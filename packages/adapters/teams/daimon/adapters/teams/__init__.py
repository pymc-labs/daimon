"""Microsoft Teams adapter for daimon (personal-chat turns).

Ingress: the Microsoft Teams Apps SDK's ``App`` + ``FastAPIAdapter`` owns
``/api/messages`` with JWT-authenticated dispatch; this package owns the
FastAPI app it mounts on, the health routes, and the process lifecycle.

Turn path: ``VerifiedTeamsTurnResolver`` verifies tenant/identity and
produces an ``AuthorizedTeamsActivity``; ``DirectCoreTurnDispatcher`` runs
it in-process against core as tracked ``asyncio`` tasks — one progress
message, updated in place, closed with the terminal card — exactly the
orchestration shape the Slack adapter uses.

Restart semantics: a redeploy kills an in-flight render loop; the turn may
still complete remotely. On next boot, ``retire_orphaned_turns`` edits the
frozen progress message to interrupted and clears the marker. No late
answer is delivered. Durable delivery/recovery is follow-up work.
"""

from daimon.adapters.teams.app import (
    AuthorizedTeamsActivity,
    DirectCoreTurnDispatcher,
)
from daimon.adapters.teams.boot_sweep import TeamsMessageSender, retire_orphaned_turns
from daimon.adapters.teams.http_service import (
    MAX_TEAMS_HTTP_BODY_BYTES,
    TeamsHttpService,
    create_teams_http_service,
)
from daimon.adapters.teams.identity import VerifiedTeamsTurnResolver
from daimon.adapters.teams.lifecycle import (
    FAILURE_MESSAGE,
    INTERRUPTED_MESSAGE,
    MAX_TEAMS_CARD_TEXT_BYTES,
    NO_ANSWER_MESSAGE,
    TRUNCATION_MARKER,
    WORKING_MESSAGE,
    bounded_text,
    terminal_card,
)
from daimon.adapters.teams.runtime import TeamsRuntime, build_runtime
from daimon.adapters.teams.turn_lifecycle import TeamsTurnLifecycle

__all__ = [
    "FAILURE_MESSAGE",
    "INTERRUPTED_MESSAGE",
    "MAX_TEAMS_CARD_TEXT_BYTES",
    "MAX_TEAMS_HTTP_BODY_BYTES",
    "NO_ANSWER_MESSAGE",
    "TRUNCATION_MARKER",
    "WORKING_MESSAGE",
    "AuthorizedTeamsActivity",
    "DirectCoreTurnDispatcher",
    "TeamsHttpService",
    "TeamsMessageSender",
    "TeamsRuntime",
    "TeamsTurnLifecycle",
    "VerifiedTeamsTurnResolver",
    "bounded_text",
    "build_runtime",
    "create_teams_http_service",
    "retire_orphaned_turns",
    "terminal_card",
]

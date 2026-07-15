"""Leak policy for user-token (xoxp) Slack reads.

Channel and group-DM (mpim) content produced from a user token follows the
asking user's own visibility (their xoxp token is the authority) and may be
answered wherever they asked. 1:1 direct-message content (im) is the
exception: it may only be produced in a DM with daimon. The destination is
resolved from slack_turn_contexts rows the Slack adapter maintains around
run_turn; zero or ambiguous rows fail closed (DM content is withheld).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.stores.slack_turn_contexts import get_slack_turn_channels

TURN_CONTEXT_TTL = timedelta(minutes=60)

DM_REDIRECT_MSG = "that DM's content is only shareable in a DM with me — ask me there instead"


def resolve_destination(channels: frozenset[str]) -> str | None:
    """Exactly one live turn channel → that's the destination; else fail closed."""
    if len(channels) == 1:
        return next(iter(channels))
    return None


def is_dm_destination(destination: str | None) -> bool:
    """Slack im (1:1 DM) channel ids start with 'D' — audience is the user alone."""
    return destination is not None and destination.startswith("D")


async def get_destination(runtime: McpRuntime, auth: AuthIdentity, *, now: datetime) -> str | None:
    """Resolve the caller's current conversation channel, or None (fail closed).

    IMPORTANT — this resolution is account-scoped, not turn-scoped: it reads
    whatever slack_turn_contexts row(s) are live for (tenant_id, account_id)
    right now, without regard to which turn is calling. Only interactive
    Slack-adapter turns write those rows (around ``run_turn`` in
    ``SlackApp._run_thread_turn``); a scheduler routine running as the same
    account does NOT write one. So a routine executing concurrently with the
    creator chatting in a DM would inherit that DM's destination here and pass
    the DM gate — i.e. non-context-writing callers alias whatever destination
    an interactive turn happens to be using at the time.

    This is safe today only because Slack has no ``send_message`` /
    broadcast-capable tool — a routine can read but never emit anywhere.
    Before shipping Slack ``send_message`` or any other broadcast-capable
    tool, this MUST be fixed: either resolve per-turn (thread a turn/request
    id through instead of keying purely on account) or have the scheduler
    write its own sentinel turn-context row so routines get a distinct,
    correctly-scoped destination instead of aliasing a concurrent interactive
    turn's.
    """
    async with runtime.session_factory() as session:
        channels = await get_slack_turn_channels(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            cutoff=now - TURN_CONTEXT_TTL,
        )
    return resolve_destination(channels)

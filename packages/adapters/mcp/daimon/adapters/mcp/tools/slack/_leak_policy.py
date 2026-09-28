"""Leak policy for user-token (xoxp) Slack reads.

Channel and group-DM (mpim) content produced from a user token follows the
asking user's own visibility (their xoxp token is the authority) and may be
answered wherever they asked. 1:1 direct-message content (im) is the
exception: it may only be produced in a DM with daimon. The destination is
resolved only from the slack_turn_contexts row named by a verified JWT claim.
Account activity is never authority; missing or expired grants fail closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.stores.slack_turn_contexts import get_slack_turn_destination

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
    """Resolve the signed execution's destination; never borrow account activity.

    Ordinary account, routine/headless and MCP agent-chat/hub credentials have
    no execution grant. Missing, expired, cleaned-up or foreign rows fail closed.
    The private-session vault is isolated, so no shared credential is restamped.
    """
    if auth.slack_turn_context_id is None:
        return None
    async with runtime.session_factory() as session:
        return await get_slack_turn_destination(
            session,
            id=auth.slack_turn_context_id,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            cutoff=now - TURN_CONTEXT_TTL,
        )

"""Shared session ownership gate for account, agent-chat and hub tools."""

from __future__ import annotations

from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_PRIVATE_DM


def session_belongs_to_caller(session: BetaManagedAgentsSession, auth: AuthIdentity) -> bool:
    """Private transcripts require the exact verified execution grant, not just an account.

    The stamp also protects session mutation: another caller must not drive the
    private session while it holds an active read grant. Missing or malformed
    grants fail closed; admins have no exception. Discord private scopes carry
    no execution credential, so all MCP session introspection is denied there.
    """
    metadata = session.metadata or {}
    if metadata.get(MA_METADATA_KEY_ACCOUNT) != str(auth.account_id):
        return False
    if MA_METADATA_KEY_PRIVATE_DM not in metadata:
        return True
    return (
        auth.slack_turn_context_id is not None
        and auth.agent_id is None
        and metadata[MA_METADATA_KEY_PRIVATE_DM] == str(auth.slack_turn_context_id)
    )

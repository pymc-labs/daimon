"""The MCP side of the shared `daimon.core.authz` builders.

Every MCP gate describes its caller with `mcp_subject`, so a field added to
`Subject` (and populated in `daimon.core.authz.build_subject`) reaches every
MCP decision -- admission, configuration, sends, seals, fork -- at once.
`mcp_place` is where an MCP turn runs.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.core.authz import Place, Subject, build_subject, build_turn_place


def mcp_subject(auth: AuthIdentity, *, is_admin: bool = False) -> Subject:
    """The caller as `authorize` sees it.

    ``is_admin`` is the gate's own trusted admin signal (the stored role, the
    hub exemption, the configuration guard's `_trusted_credential`); an
    agent-scoped key is marked so `authorize` never exempts it as an admin.
    The channel admin grants are the verifier's read of the stored ones.
    """
    return build_subject(
        is_admin=is_admin,
        platform_user_id=auth.platform_user_id,
        via_agent_key=auth.agent_id is not None,
        administered_channel_ids=auth.administered_channel_ids,
    )


def mcp_place(auth: AuthIdentity) -> Place:
    """Where an MCP turn runs: no channel, or a channel-bound agent key's channel.

    Only the key's own channel: an unbound key, a chat turn's credential and a
    hub login stay outside every channel (`token_channel_id`).
    """
    channel_id = token_channel_id(auth)
    if channel_id is None:
        return Place()
    return build_turn_place(channel_id=channel_id, thread_id=None)

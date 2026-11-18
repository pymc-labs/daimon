"""The MCP side of the shared `daimon.core.authz` builders.

Every MCP gate describes its caller with `mcp_subject`, so a field added to
`Subject` (and populated in `daimon.core.authz.build_subject`) reaches every
MCP decision -- admission, configuration, sends, seals, fork -- at once.
`mcp_place` is where an MCP turn runs.
"""

from __future__ import annotations

from contextvars import ContextVar

from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Place, Subject, build_subject, build_turn_place
from daimon.core.channel_changes import ChannelActor
from daimon.core.stores.domain import TurnOriginRow

#: Where the running tool call verifiably started: its turn's channel and thread, set once
#: per call by `CallOriginMiddleware`. Empty for a call with no live turn (a hub or web
#: login, a routine) and for an agent key, which keeps an old origin.
CALL_ORIGIN: ContextVar[frozenset[str]] = ContextVar("daimon_call_origin", default=frozenset())


def origin_proves_presence(auth: AuthIdentity, origin: TurnOriginRow) -> bool:
    """Whether a turn origin proves someone is in that channel now.

    Unattended or resumed work proves nobody is there. A held credential stays in its
    hold. Middleware and every explicit origin use this one check.
    """
    if not origin.attended:
        return False
    return auth.hold is None or auth.hold.contains(
        origin.thread_id or origin.parent_channel_id, origin.parent_channel_id
    )


def mcp_subject(auth: AuthIdentity, *, is_admin: bool = False) -> Subject:
    """The caller as `authorize` sees it.

    ``is_admin`` is the gate's own trusted admin signal (the stored role, the
    hub exemption, the configuration guard's `_trusted_credential`); an
    agent-scoped key is marked so `authorize` never exempts it as an admin. A
    held credential's requester from another organisation is marked a client, which
    forces ``is_admin`` off. Pass the credential's own hold (``auth.hold``) to every
    `authorize` call that can run for it.
    """
    return build_subject(
        is_admin=is_admin,
        platform_user_id=auth.platform_user_id,
        via_agent_key=auth.is_external_agent,
        client=auth.client,
        origin_channel_ids=() if auth.is_external_agent else CALL_ORIGIN.get(),
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


def chat_actor(auth: AuthIdentity, action: DelegatedAction | None = None) -> ChannelActor:
    """The caller as a channel change from chat sees it (`daimon.core.channel_changes`).

    An admin for `action` only; without one, only a platform admin."""
    return ChannelActor(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        subject=mcp_subject(auth, is_admin=auth.admin_for(action)),
        surface="chat",
    )

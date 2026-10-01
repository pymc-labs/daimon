"""Whether a member may change what a pinned agent reaches, from where they are.

One rule for every path that adds to or edits an agent's configuration (keys,
connector tokens, MCP servers, prompt, tools, skills): an admin always may; on
an agent the operator pinned to channels, anyone else may only act from a
conversation inside those channels; an unpinned agent is unaffected. The
request tools, the direct configuration tools and the private forms' submit
paths all decide it with `daimon.core.authz.authorize(CONFIGURE)`, so the rule
and its names can't drift.
"""

from __future__ import annotations

from collections.abc import Mapping

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_READER_SOURCE,
)
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import CredentialRequestRow, Role
from sqlalchemy.ext.asyncio import AsyncSession

PIN_WRITE_REFUSAL = (
    "An operator pinned this agent to its own channels, so its keys, connections and "
    "configuration can only be changed from a conversation inside them, or by a server "
    "or workspace admin. Nothing was changed."
)
POLICY_UNREADABLE_REFUSAL = (
    "This workspace's access policy could not be read, so nothing was changed. "
    "Ask an admin to check it."
)


_READER_SUFFIX = "-reader"


def agent_pin_names(name: str | None, metadata: Mapping[str, str]) -> tuple[str | None, ...]:
    """Every name a pin on an agent may be keyed by: its MA name and its config name.

    A published report's reader variant answers as its source agent, so it
    also carries the source's names (stamped as `daimon_reader_source`; a
    reader written before that stamp falls back to its name without the
    ``-reader`` suffix): a pin on the source holds for its reader. Admission
    checks the same names, so a pin on any of them holds everywhere.
    """
    config_name = metadata.get(MA_METADATA_KEY_NAME)
    names: list[str | None] = [name, config_name]
    if MA_METADATA_KEY_READER_OF in metadata:
        stamped = metadata.get(MA_METADATA_KEY_READER_SOURCE)
        if stamped:
            names.extend(stamped.split("\n"))
        else:
            names.extend(
                candidate[: -len(_READER_SUFFIX)]
                for candidate in (name, config_name)
                if candidate and candidate.endswith(_READER_SUFFIX)
            )
    return tuple(names)


async def request_pin_refusal(
    session: AsyncSession, *, row: CredentialRequestRow, agent: BetaManagedAgentsAgent | None
) -> str | None:
    """Refusal copy when a private form may not be applied to its pinned target, else None.

    Called by every submit path just before the form is consumed, with the
    target resolved afresh by its stable id (``row.agent_id``), so a pin
    added since the card was posted, a rename, or a pin keyed by the config
    name all hold. An unreadable policy refuses. Admin is the requester's
    stored role, the signal the MCP gate reads too.
    """
    try:
        policy = await load_access_policy(session, tenant_id=row.tenant_id)
    except AccessPolicyUnreadable:
        return POLICY_UNREADABLE_REFUSAL
    if not policy.agent_channel_pins:
        return None
    account = await get_account(session, row.account_id)
    decision = authorize(
        policy,
        subject=Subject(is_admin=account is not None and account.role is Role.ADMIN),
        action=Action.CONFIGURE,
        surface=Surface.CONFIG,
        agent=(
            AgentRef.unresolved()
            if agent is None
            else AgentRef.of(*agent_pin_names(agent.name, agent.metadata))
        ),
        place=Place.from_origin(
            parent_channel_id=row.parent_channel_id, thread_id=row.origin_thread_id
        ),
    )
    return None if decision else PIN_WRITE_REFUSAL

"""The lines a provider switch says, and the answer to an older switch card's press.

A switch applies at once and posts no card. Cards posted before that expired
within a day, so a press on one only closes it as expired.
"""

from __future__ import annotations

import datetime
import re
import uuid
from typing import TYPE_CHECKING, Final

from daimon.core.host_calls import EXPIRED_TEXT
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.posted_controls import PostedCard

#: An older switch card's buttons: ``bsw:<card id>:confirm`` or ``…:cancel``.
CUSTOM_ID_PATTERN: Final[str] = r"bsw:(?P<card>[0-9a-f-]{36}):(?P<action>confirm|cancel)"
_CUSTOM_ID = re.compile(CUSTOM_ID_PATTERN)


def expired_switch_card(custom_id: str) -> PostedCard | None:
    """The closed card for a press on an older switch card; None for any other button."""
    from daimon.core.posted_controls import PostedCard

    if _CUSTOM_ID.fullmatch(custom_id) is None:
        return None
    return PostedCard(kind="switch", state="expired", headline=f"⌛ {EXPIRED_TEXT}")


async def channel_pin_line(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
) -> str | None:
    """Say so when this channel pins a provider: the agent's switch won't apply there."""
    from daimon.core.channel_backend import channel_ref, current_backend
    from daimon.core.stores import mux_state
    from daimon.core.turn.notices import provider_name

    channel = channel_ref(tenant_id, platform, channel_id)
    async with sessionmaker() as session:
        pinned = await current_backend(session, channel)
        if (
            pinned is None
            or await mux_state.config_origin(session, channel, pinned.local) != "channel"
        ):
            return None
    model = f" ({pinned.model})" if pinned.model else ""
    return (
        f"This channel pins {provider_name(pinned.backend)}{model}, so this agent keeps "
        'answering on that here until an admin clears it: say "clear this channel\'s pin".'
    )


async def move_this_thread(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    responder_name: str | None,
    agent_name: str,
    target: Provider,
    pinned_here: bool,
) -> str:
    """After a confirmed switch, move the requester's own slot of this thread too.

    Only when the switched agent is the one answering here, and never a shared
    thread (it stays pinned for everyone in it). Says plainly what happens.
    """
    from daimon.core.stores import mux_state, thread_agent_bindings
    from daimon.core.thread_release import caller_slot, request_thread_release
    from daimon.core.turn.notices import provider_name

    provider = provider_name(target)
    if pinned_here:
        return "This channel pins its own provider, so this conversation stays on it."
    async with sessionmaker() as session:
        handed = await thread_agent_bindings.get_binding(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
        )
    # Who answers this conversation's next message: a hand-off's agent, else this
    # turn's responder. A setup conversation's configuration target never moves it.
    answering = (
        handed.responder_name
        if handed is not None and handed.kind in ("handoff", "owner")
        else responder_name
    )
    if (handed is not None and handed.kind == "setup") or agent_name != answering:
        return f"{agent_name} isn't the one answering here, so this conversation is unchanged."
    slot = caller_slot(
        tenant_id=tenant_id,
        platform=platform,
        channel_id=parent_channel_id,
        thread_id=thread_id,
        account_id=account_id,
    )
    async with sessionmaker.begin() as session:
        shared = slot.model_copy(update={"account_id": None})
        if await mux_state.get_binding(session, shared) is not None:
            return (
                "This conversation is shared, so it stays on its current model; "
                f"start a new thread to talk to {agent_name} on {provider}."
            )
        pinned = await mux_state.get_binding(session, slot, include_released=True)
        await request_thread_release(
            session,
            slot,
            generation=pinned.generation if pinned is not None else 0,
            now=datetime.datetime.now(datetime.UTC),
        )
    return (
        f"This conversation moves to {provider} from the next message "
        "and keeps its earlier messages."
    )


#: A self-posted card lives outside any turn, so a turn's ceiling doesn't cap
#: it; it never waits past a day (#1414's longest card), and says when it ends.
SWITCH_CARD_MAX_S: Final[int] = 86400

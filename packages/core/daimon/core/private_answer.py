"""The Private answer offer in a contained room (`audience_hold`, BUILD-PLAN C4).

A room that people outside the team can read gets an answer with internal sources
left out. An internal asker there is offered a Private answer: the same question, run
again as a new DM turn with no source and no room history. This module decides two
things on the stored facts and the asker's standing as they are now:

- `offer_private_answer`: whether the room shows the offer after a turn.
- `private_answer_refusal`: whether a press of the offer may start the DM turn.

Mode `off` reads nothing and offers nothing. Mode `shadow` logs `private_offer` where
mode `on` would show the offer, and shows nothing. A narrow room never offers: narrow
rooms are closed to DMs and private answers. An offer the press would certainly refuse
is not shown either (`audience.private_offer_skipped`): DMs are off on the server, or
the agent may not answer this person in a DM (a channel's own agent never does).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Literal

import structlog
from daimon.core.authz import Action, AgentRef, authorize, build_subject
from daimon.core.channel_audience import (
    AudienceMode,
    audience_mode,
    channel_has_outsiders,
    log_shadow,
    requester_standing,
)
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.membership import MembershipResolver
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

OfferSkip = Literal["dms_off", "agent_no_dms"]
"""Why an offer the press would refuse is not shown: DMs are off on the server, or the
agent may not answer this person in a DM."""

PrivateAnswerRefusal = Literal["unavailable", "not_internal", "narrow", "dms_off"]
"""Why a press cannot start a Private answer: the mode is not `on`, the presser holds
no internal role, the room is narrow, or DMs are off."""


async def offer_private_answer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    channel_id: str,
    thread_id: str | None,
    mode: AudienceMode,
    agent: str | None,
    agent_ref: AgentRef,
    event_role_ids: Collection[str] | None = None,
    resolver: MembershipResolver | None = None,
) -> bool:
    """Whether the room shows the Private answer offer to `user_id` (Discord only).

    True in mode `on` for a contained room that is not narrow, when the asker holds an
    internal role and the press would not certainly be refused (`offer_skip`, with the
    agent as its rules name it, `agent_ref`). Mode `shadow` logs `private_offer` where
    `on` would show it, and returns False. Mode `off` reads nothing.
    """
    if mode == "off":
        return False
    audience = await channel_has_outsiders(
        session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id=channel_id,
        thread_id=thread_id,
        mode=mode,
    )
    facts = audience if mode == "on" else audience.shadow
    if facts is None or not facts.contained or facts.narrow:
        return False
    standing = await requester_standing(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        event_role_ids=event_role_ids,
        # Shadow changes nothing: no member fetch for a log line.
        resolver=resolver if mode == "on" else None,
        mode="on",
    )
    if standing is not RequesterOrg.HOST:
        return False
    skip = await offer_skip(session, tenant_id=tenant_id, user_id=user_id, agent=agent_ref)
    if skip is not None:
        _log.info(
            "audience.private_offer_skipped",
            tenant_id=str(tenant_id),
            channel_id=channel_id,
            agent=agent,
            reason=skip,
        )
        return False
    if mode == "shadow":
        log_shadow(
            "private_offer",
            tenant_id=tenant_id,
            channel_id=channel_id,
            agent=agent,
            reason="room_contained",
        )
        return False
    return True


async def offer_skip(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: str, agent: AgentRef
) -> OfferSkip | None:
    """Why a press would certainly be refused as things stand; None when it may pass.

    The press's own static checks: DMs on for the server, then the agent's DM rule as
    the DM admission asks it (no admin standing). The press asks both again, since
    either can change in between.
    """
    if not await dm_enabled(session, tenant_id=tenant_id):
        return "dms_off"
    policy = await load_access_policy(session, tenant_id=tenant_id)
    subject = build_subject(is_admin=False, platform_user_id=user_id)
    if not authorize(
        policy, subject=subject, action=Action.DIRECT_MESSAGE, agent=agent, recipient_id=user_id
    ):
        return "agent_no_dms"
    return None


async def private_answer_refusal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    channel_id: str,
    thread_id: str | None,
    mode: AudienceMode,
    event_role_ids: Collection[str] | None = None,
    resolver: MembershipResolver | None = None,
) -> PrivateAnswerRefusal | None:
    """Why a press of the offer cannot start a Private answer now; None when it can.

    It reads again the mode, the asker's standing and the room. A narrow room is closed
    to private answers. The server must allow DMs. A room that is open again still
    gives the answer: it is a new DM with no source, which the asker could start anyway.
    """
    if mode != "on":
        return "unavailable"
    standing = await requester_standing(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        event_role_ids=event_role_ids,
        resolver=resolver,
        mode=mode,
    )
    if standing is not RequesterOrg.HOST:
        return "not_internal"
    audience = await channel_has_outsiders(
        session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id=channel_id,
        thread_id=thread_id,
        mode=mode,
    )
    if audience.narrow:
        return "narrow"
    if not await dm_enabled(session, tenant_id=tenant_id):
        return "dms_off"
    return None


async def read_mode(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel_id: str
) -> AudienceMode:
    """The server's `audience_hold` mode at a Discord room."""
    return await audience_mode(session, FeaturePlace(tenant_id, "discord", channel_id))


__all__ = [
    "OfferSkip",
    "PrivateAnswerRefusal",
    "offer_skip",
    "offer_private_answer",
    "private_answer_refusal",
    "read_mode",
]

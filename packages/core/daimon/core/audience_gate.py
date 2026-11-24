"""One output gate for rooms that outsiders may read (`audience_hold`).

`audience_refusal` is the single rule: a destination that is contained (outsiders may
read it, or nobody can say) takes a bot post only from an execution held to it
(`TurnHold.contains`). Three callers apply it and read the destination's fact through
`channel_has_outsiders`, never from the registry:

* Gate A, the output latch every turn installs (`turn.output.audience_output_check`): the
  first post and each streamed edit of an answer.
* Gate B, `authorize(POST, audience=...)`: adapter posts made with no turn running,
  such as routine delivery, a watch's report thread and a notice (`audience_fact`).
* Gate C, the MCP write guard (`require_channel_writable`).

The server's `audience_hold` mode decides what a refusal does: `off` reads nothing and
changes nothing, `shadow` logs `audience.shadow` and posts, `on` refuses. A failed mode
or fact read raises, so the post fails: it never falls back to a mode.

`POST_EXEMPTIONS` lists the output paths that do not call the gate, with the reason.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

import structlog
from daimon.core.audience_refusal import AUDIENCE_CONTAINED, AudienceRefusal, audience_refusal
from daimon.core.channel_audience import (
    AudienceMode,
    ChannelAudience,
    audience_mode,
    channel_has_outsiders,
)
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.turn_hold import TurnHold
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: The platforms whose `audience_hold` can be set. Others are never read: mode `off`.
GATED_PLATFORMS: Final = frozenset({"discord"})

#: Output paths that skip the gate, each with its reason. A test checks this list
#: against the code that makes each post (`test_audience_gate_exemptions.py`).
POST_EXEMPTIONS: Final[dict[str, str]] = {
    "reaction": "An emoji carries no content.",
    "turn_notice": (
        "Fixed text or an error card in the room where the conversation started. Never text the "
        "agent wrote: a confirmation card carries that and takes the gate."
    ),
    "hand_over_notice_edit": "An edit of the bot's own fixed notice in the same room.",
    "setup_thread": "An admin's setup action, not agent output.",
    "support_escalation": "The person's own Ask-a-human action; the agent writes none of it.",
    "private_reply": "A reply to the person alone, in a direct message or an ephemeral response.",
}


@dataclass(frozen=True)
class PostAudience:
    """Opt a post decision into the audience gate: where it runs and who it is held to."""

    platform: str
    hold: TurnHold | None = None
    agent: str | None = None
    #: The execution does not exist yet and will be held to the destination if it is
    #: contained, as admission holds every turn in a contained channel (a watch's report
    #: thread). Only an unreadable fact refuses such a post.
    held_if_contained: bool = False

    def hold_for(self, fact: ChannelAudience, channel_id: str) -> TurnHold | None:
        """The hold this post runs under, given the destination's fact."""
        if self.held_if_contained and fact.contained:
            return TurnHold(self.platform, channel_id, client=False)
        return self.hold


def _place(
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    parent_id: str | None,
    routine_id: uuid.UUID | None,
    workflow_id: uuid.UUID | None = None,
) -> FeaturePlace:
    return FeaturePlace.of(
        tenant_id,
        platform,
        parent_id or channel_id,
        channel_id if parent_id else None,
        routine_id=routine_id,
        workflow_id=workflow_id,
    )


async def read_audience_mode(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    parent_id: str | None = None,
    routine_id: uuid.UUID | None = None,
    session: AsyncSession | None = None,
    workflow_id: uuid.UUID | None = None,
) -> AudienceMode:
    """The mode for a destination: `off` for a platform that cannot set it, else read (raises).

    With `session` the read runs in the caller's open transaction and opens no other.
    """
    if platform not in GATED_PLATFORMS:
        return "off"
    place = _place(tenant_id, platform, channel_id, parent_id, routine_id, workflow_id)
    if session is not None:
        return await audience_mode(session, place)
    async with sessionmaker() as own:
        return await audience_mode(own, place)


def log_shadow_refusal(
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    agent: str | None,
    reason: str = AUDIENCE_CONTAINED,
    kind: Literal["post_refused", "dm_refused", "fact_unreadable"] = "post_refused",
) -> None:
    """The shadow record of a post `on` would refuse. Never carries message text."""
    log.info(
        "audience.shadow",
        kind=kind,
        tenant_id=str(tenant_id),
        channel_id=channel_id,
        agent=agent,
        reason=reason,
    )


async def _read_fact(
    sessionmaker: async_sessionmaker[AsyncSession],
    session: AsyncSession | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel: str,
) -> ChannelAudience:
    if session is not None:
        # A savepoint: a failed read leaves the caller's transaction usable.
        async with session.begin_nested():
            return await channel_has_outsiders(
                session, tenant_id=tenant_id, platform=platform, channel_id=channel
            )
    async with sessionmaker() as own:
        return await channel_has_outsiders(
            own, tenant_id=tenant_id, platform=platform, channel_id=channel
        )


async def audience_fact(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    parent_id: str | None,
    hold: TurnHold | None,
    agent: str | None = None,
    routine_id: uuid.UUID | None = None,
    mode: AudienceMode | None = None,
    held_if_contained: bool = False,
    session: AsyncSession | None = None,
    workflow_id: uuid.UUID | None = None,
) -> ChannelAudience | None:
    """The fact `authorize(POST, audience=...)` must enforce, or None when nothing is.

    None in mode `off` (nothing is read beyond the mode) and in `shadow` (a refusal is
    logged, the post goes ahead, also when the fact cannot be read). A failed read in
    `on`, and a failed mode read, raise. With `session` every read runs in the caller's
    open transaction and opens no other.
    """
    mode = mode or await read_audience_mode(
        sessionmaker,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        parent_id=parent_id,
        routine_id=routine_id,
        session=session,
        workflow_id=workflow_id,
    )
    if mode == "off":
        return None
    try:
        fact = await _read_fact(
            sessionmaker,
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel=parent_id or channel_id,
        )
    except Exception as exc:
        if mode != "shadow":
            raise
        log_shadow_refusal(
            tenant_id=tenant_id,
            channel_id=channel_id,
            agent=agent,
            reason=type(exc).__name__,
            kind="fact_unreadable",
        )
        return None
    if mode == "shadow":
        if held_if_contained and fact.contained:
            hold = TurnHold(platform, parent_id or channel_id, client=False)
        if audience_refusal(fact, hold, channel_id, parent_id) is not None:
            log_shadow_refusal(tenant_id=tenant_id, channel_id=channel_id, agent=agent)
        return None
    return fact


__all__ = [
    "AUDIENCE_CONTAINED",
    "GATED_PLATFORMS",
    "POST_EXEMPTIONS",
    "AudienceRefusal",
    "PostAudience",
    "audience_fact",
    "audience_refusal",
    "log_shadow_refusal",
    "read_audience_mode",
]

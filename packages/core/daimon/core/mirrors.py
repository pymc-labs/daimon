"""Mirrors: which events a mirror copies, whether it may, and what it posts. Pure.

A mirror copies one agent's agent-chat questions and answers into a channel
or thread of the same tenant. Content may go from A to B only if everyone who
can read B may read A. Daimon's rules decide the part it can see:

- nothing posts where the destination's writers are none;
- a session sealed to a channel is readable only inside it (`not_a_reader`);
- content of a channel kept to its own agents, or produced by one of them,
  stays in that channel, and nothing else enters it (`own_agents_only`). So a
  home's own agent may mirror into its home, and only there.

A person's own unbound session is readable by that person alone, so it is
copied only under recorded consent (`consent_basis`); a mirror without it
copies channel-bound sessions only. Who can view a channel on the platform is
the admin's call when they create the mirror.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.mirror_events import ConversationEventV1, SourceRef
from daimon.core.observability import redact_text
from daimon.core.permissions import (
    ChannelPermissions,
    agent_permissions,
    channel_permissions,
    crosses_home,
    session_homes,
    session_readable_from,
)
from daimon.core.stores.domain import MirrorRow

MirrorRefusal = Literal["writers_none", "not_a_reader", "own_agents_only"]

#: Discord's limit on a thread name.
_THREAD_NAME_MAX = 100


def matching_mirrors(
    mirrors: Iterable[MirrorRow], event: ConversationEventV1
) -> tuple[MirrorRow, ...]:
    """The active mirrors of the event's tenant and agent that cover it.

    A person's own session needs a mirror with recorded consent; without it
    the event is not stored for that mirror at all.
    """
    return tuple(
        mirror
        for mirror in mirrors
        if mirror.status == "active"
        and mirror.tenant_id == event.tenant_id
        and mirror.source_agent_id == event.agent_id
        and (mirror.consent_basis is not None or not event.person_scoped)
    )


def _place_ids(dest: ChannelPermissions) -> frozenset[str]:
    ids = {dest.channel_id, dest.parent_channel_id}
    if dest.channel_id is not None and dest.parent_channel_id is not None:
        ids.add(f"{dest.parent_channel_id}:{dest.channel_id}")
    return frozenset(each for each in ids if each is not None)


def mirror_refusal(
    policy: TenantAccessPolicy,
    *,
    agent_names: Iterable[str],
    dest: ChannelPermissions,
    source: SourceRef | None = None,
) -> MirrorRefusal | None:
    """Why the agent's content may not be copied to `dest`, or None.

    Without `source` (when the mirror is created) only what holds for every
    event is checked; with it, also the event's own channel and seals.
    """
    if dest.writers == "none":
        return "writers_none"
    if dest.home_unknown:
        return "own_agents_only"
    agent = agent_permissions(policy, agent_names)
    origin = None
    if source is not None:
        if not session_readable_from(
            policy,
            _place_ids(dest),
            channel=source.channel_id,
            thread=None,
            seal_ids=source.seal_ids,
            legacy_thread_id=None,
        ):
            return "not_a_reader"
        if source.channel_id is not None:
            origin = channel_permissions(policy, channel_id=source.channel_id)
        # Every home the session lay in when it ran, as `authz` reads a session.
        lies_in = session_homes(
            policy, channel=source.channel_id, thread=None, seal_ids=source.seal_ids
        )
        if any(home != dest.home or home != agent.home for home in lies_in):
            return "own_agents_only"
    if crosses_home(agent, dest, origin):
        return "own_agents_only"
    return None


@dataclass(frozen=True)
class MirrorPost:
    """What one event posts: a header that opens the session's thread (posted
    only for its first event), then the question and the answer."""

    thread_name: str
    header: str
    question: str
    answer: str


def _quoted(text: str, limit: int) -> str:
    """Redacted, clipped without leaving a code fence open, and blockquoted."""
    text = redact_text(text.strip())
    if len(text) > limit:
        cut = text[:limit].rstrip()
        fence = "\n```" if cut.count("```") % 2 else ""
        text = f"{cut}{fence}\n… ({len(text) - limit} more characters)"
    return "\n".join(f"> {line}" for line in text.splitlines()) or ">"


def render_qa(event: ConversationEventV1, *, agent_name: str, max_chars: int) -> MirrorPost:
    """The posts for one question and answer.

    Only the question and the final answer are copied, redacted and quoted:
    never tool calls or their output. The asker is named by display name,
    never by platform id, and marked when they are from another organisation.
    """
    session = event.source.session_key[-6:]
    asker = event.actor.display_name or "Someone"
    if event.actor.is_external:
        asker = f"{asker} (external)"
    return MirrorPost(
        thread_name=f"{agent_name} · {session}"[:_THREAD_NAME_MAX],
        header=f"Questions to **{agent_name}** · session `{session}`",
        question=f"**Q** · *{asker}*\n{_quoted(event.question, max_chars)}",
        answer=f"**A** · *{agent_name}*\n{_quoted(event.answer, max_chars)}",
    )


def answer_limit(mirror: MirrorRow, ceiling: int) -> int:
    """The mirror's own limit, never above the deployment's."""
    return min(mirror.max_answer_chars or ceiling, ceiling)


__all__ = [
    "MirrorPost",
    "MirrorRefusal",
    "answer_limit",
    "matching_mirrors",
    "mirror_refusal",
    "render_qa",
]

"""Who may act on an answer: the people who could have asked the agent there.

Feedback and Ask a human on an answer are open to exactly the people the
tenant lets start a turn at that answer's place (`authorize(START_TURN)`:
protection and the invoker allowlist), as on Slack (`place_access`). The
clicker is the verified one (`card_actor`), so someone from another
organisation never gets here, as Slack refuses a Slack Connect member. Their
admin role is the live one; the grant-named teams they own are looked up
before any session opens, because Graph may be slow. The same policy read says
whether the answer's place is sealed (`readers_limited_at`), which Ask a human
tells the person and the support team.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from daimon.adapters.teams.card_actions import Actor
from daimon.adapters.teams.channel_admin_groups import channel_admin_caller
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize, build_turn_place
from daimon.core.channel_admins import ChannelAdminCaller, load_live_subject
from daimon.core.permissions import readers_limited_at
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from microsoft_teams.api import InvokeActivity
from sqlalchemy.ext.asyncio import AsyncSession

NOT_ALLOWED = "You can't do that on this answer."
POLICY_UNREADABLE = (
    "This organisation's access policy could not be read, so nothing was recorded. "
    "Ask an admin to check it."
)
IN_DIRECT_CHAT = "(an answer in a 1:1 chat)"

Access = Literal["allowed", "refused", "unreadable"]


@dataclass(frozen=True)
class AnswerPlace:
    """The answer a click is about: its conversation (a channel thread's id, or a
    1:1 chat's) and its message id."""

    conversation_id: str
    message_id: str
    is_channel: bool

    @classmethod
    def of(cls, activity: InvokeActivity, message_id: str) -> AnswerPlace:
        conversation = activity.conversation
        return cls(conversation.id, message_id, conversation.conversation_type == "channel")

    @property
    def channel_id(self) -> str:
        return self.conversation_id.split(";", 1)[0]

    def link(self, entra_tenant_id: str) -> str:
        """A link to the answer; a 1:1 chat has none anyone else can open."""
        if not self.is_channel:
            return IN_DIRECT_CHAT
        _, _, root = self.conversation_id.partition(";messageid=")
        query = f"tenantId={entra_tenant_id}" + (f"&parentMessageId={root}" if root else "")
        channel = quote(self.channel_id)
        return f"https://teams.microsoft.com/l/message/{channel}/{self.message_id}?{query}"


@dataclass(frozen=True)
class AnswerAccess:
    """`check_answer_access`'s decision, and whether the place's readers are kept inside."""

    access: Access
    sealed: bool = False


def sealed_at(policy: TenantAccessPolicy, place: AnswerPlace) -> bool:
    """Whether only turns inside the answer's channel (or its thread) read it."""
    return place.is_channel and readers_limited_at(
        policy, channel_id=place.channel_id, thread_id=place.conversation_id
    )


async def clicker(runtime: TeamsRuntime, actor: Actor) -> ChannelAdminCaller:
    """The clicker with the grant-named teams they own. Network: call with no session open."""
    return await channel_admin_caller(
        runtime, tenant_id=actor.tenant_id, user_id=actor.user_id, is_admin=actor.is_admin
    )


async def may_start_turn_at(
    session: AsyncSession,
    policy: TenantAccessPolicy,
    actor: Actor,
    caller: ChannelAdminCaller,
    place: AnswerPlace,
) -> bool:
    """START_TURN at the answer's place, as admission decides it for a message there."""
    subject: Subject = await load_live_subject(
        session, tenant_id=actor.tenant_id, platform="teams", caller=caller
    )
    turn_place = build_turn_place(channel_id=place.channel_id, thread_id=place.conversation_id)
    return bool(authorize(policy, subject=subject, action=Action.START_TURN, place=turn_place))


async def check_answer_access(
    runtime: TeamsRuntime, actor: Actor, place: AnswerPlace
) -> AnswerAccess:
    """Decide, outside any write, whether `actor` may act on the answer at `place`."""
    caller = await clicker(runtime, actor)
    async with runtime.sessionmaker() as session:
        try:
            policy = await load_access_policy(session, tenant_id=actor.tenant_id)
        except AccessPolicyUnreadable:
            return AnswerAccess("unreadable")
        allowed = await may_start_turn_at(session, policy, actor, caller, place)
    return AnswerAccess("allowed" if allowed else "refused", sealed_at(policy, place))


def refusal_text(access: str) -> str:
    """What a refused clicker is told; `access` is the refusal `check_answer_access` gave."""
    return POLICY_UNREADABLE if access == "unreadable" else NOT_ALLOWED

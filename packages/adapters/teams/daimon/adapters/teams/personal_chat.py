"""Which agent a Teams personal chat reaches, by the DM grants, rules and picks Slack uses.

A chat is pinned by a hand-over binding on the chat itself, the one Switch to
agent writes. A chat with none is routed on its first message
(`daimon.core.dm_routing`): one agent starts at once, several get a card of
buttons, and a refusal says why. Each turn rechecks the pinned agent's grant
with the cached team rosters (`daimon.core.direct_messages.require_dm_turn_grant`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import structlog
from daimon.adapters.teams.card import notice_adaptive_card, notice_card
from daimon.adapters.teams.card_actions import (
    FAILED,
    button,
    card_actor,
    guarded,
    replace_card,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.identity import DENIED, TeamsInbound, display_bot_name
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.team_owners import team_member_lookup
from daimon.core.authz import build_subject
from daimon.core.direct_messages import DM_AGENT_REFUSED, dm_permission_text
from daimon.core.dm_access import DmAgent, decide_dm, list_dm_agents, lookup_dm_groups
from daimon.core.dm_routing import DmChoose, DmDecision, DmRefused, DmStart, DmUnchecked, Route
from daimon.core.native_admins import GroupLookupFailed, GroupMembers
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.dm_access import set_pick
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.thread_agent_bindings import get_binding, upsert_responder_binding
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext

if TYPE_CHECKING:
    from daimon.adapters.teams.thread_reader import ThreadReader

__all__ = ["UNCHECKED", "VERB", "TeamsDmPick", "chat_groups", "route_personal_chat"]

log = structlog.get_logger()

VERB: Final = "dm_pick_agent"
UNCHECKED: Final = "I couldn't check your access just now.\n\nTry again in a minute."
_CHOOSE: Final = "Several agents can answer you here.\n\nChoose one."
_MAX_CHOICES: Final = 6

Post = Callable[[MessageActivityInput], Awaitable[None]]


def _members(runtime: TeamsRuntime, *, fresh: bool) -> GroupMembers:
    lookup = team_member_lookup(runtime, fresh=fresh)
    if lookup is not None:
        return lookup

    async def unknown(group_id: str) -> frozenset[str]:
        raise GroupLookupFailed(f"no Graph access for team {group_id}")

    return unknown


async def chat_groups(
    runtime: TeamsRuntime, *, tenant_id: uuid.UUID, user_id: str, fresh: bool
) -> frozenset[str] | None:
    """The granted or ruled teams `user_id` is in; None when a roster could not be read."""
    return await lookup_dm_groups(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id.lower(),
        members=_members(runtime, fresh=fresh),
    )


async def _decide(
    runtime: TeamsRuntime, *, tenant_id: uuid.UUID, user_id: str
) -> tuple[DmDecision, list[DmAgent], uuid.UUID | None]:
    """The route for a chat with no agent yet, with the person's teams read afresh."""
    groups = await chat_groups(runtime, tenant_id=tenant_id, user_id=user_id, fresh=True)
    async with runtime.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        agents = await list_dm_agents(
            session,
            tenant_id=tenant_id,
            anthropic=runtime.turn_deps.anthropic,
            records=runtime.turn_deps.sessionmaker,
        )
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="teams", external_id=user_id
        )
        account_id = None if principal is None else principal.account_id
        decision = await decide_dm(
            session,
            runtime.settings,
            policy,
            tenant_id=tenant_id,
            platform="teams",
            agents=agents,
            subject=build_subject(is_admin=False, platform_user_id=user_id.lower()),
            account_id=account_id,
            groups=groups,
        )
    return decision, agents, account_id


async def _pin(
    runtime: TeamsRuntime,
    *,
    tenant_id: uuid.UUID,
    chat: str,
    agent: DmAgent,
    account_id: uuid.UUID | None,
    remember_pick: bool,
) -> None:
    """Bind the chat to `agent`, as Switch to agent does; a pick is saved for next time."""
    now = datetime.now(UTC)
    async with runtime.sessionmaker() as session, session.begin():
        await upsert_responder_binding(
            session,
            tenant_id=tenant_id,
            platform="teams",
            parent_channel_id=chat,
            thread_id=chat,
            responder_ma_agent_id=agent.ma_agent_id,
            responder_name=agent.name,
            created_by_account_id=account_id,
            now=now,
        )
        if remember_pick and account_id is not None:
            await set_pick(
                session, tenant_id=tenant_id, account_id=account_id, agent_name=agent.name, now=now
            )


def _refusal(route: Route) -> str:
    if isinstance(route, DmUnchecked):
        return UNCHECKED
    if isinstance(route, DmRefused) and route.reason == "no_agent":
        return dm_permission_text("teams")
    return DM_AGENT_REFUSED


async def route_personal_chat(
    runtime: TeamsRuntime, inbound: TeamsInbound, *, tenant_id: uuid.UUID, post: Post
) -> bool:
    """True to answer `inbound`; False once the person was told why not or asked to choose.

    Only a personal chat with no binding is routed; a bound one is rechecked after admission.
    """
    chat = inbound.conversation_id
    if inbound.kind != "dm" or inbound.setup_thread_id is not None:
        return True
    async with runtime.sessionmaker() as session:
        bound = await get_binding(
            session, tenant_id=tenant_id, platform="teams", parent_channel_id=chat, thread_id=chat
        )
    if bound is not None:
        return True
    decision, agents, account_id = await _decide(
        runtime, tenant_id=tenant_id, user_id=inbound.user_id
    )
    route = decision.route
    log.info("teams.dm.routed", route=type(route).__name__)
    if isinstance(route, DmStart):
        agent = next(a for a in agents if a.name == route.agent)
        await _pin(
            runtime,
            tenant_id=tenant_id,
            chat=chat,
            agent=agent,
            account_id=account_id,
            remember_pick=False,
        )
        return True
    if isinstance(route, DmChoose):
        choices = [button(VERB, name, "pick", agent=name) for name in route.agents[:_MAX_CHOICES]]
        await post(notice_card(_CHOOSE, actions=choices, error=False))
        return False
    await post(notice_card(_refusal(route)))
    return False


class TeamsDmPick:
    """Agent choice clicks in a personal chat: recheck, pin, then answer the waiting message."""

    def __init__(
        self,
        runtime: TeamsRuntime,
        *,
        dispatch: Callable[[TeamsInbound], None],
        reader: ThreadReader,
    ) -> None:
        self._runtime = runtime
        self._dispatch = dispatch
        self._reader = reader

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._on_action(ctx), toast(FAILED), "teams.dm.pick_failed")

    async def _on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        runtime, activity = self._runtime, ctx.activity
        actor = await card_actor(runtime, activity)
        if actor is None:
            return toast(DENIED)
        name = str(submitted_fields(activity.value.action.data).get("agent") or "")
        if activity.conversation.conversation_type != "personal" or not name:
            return toast(FAILED)
        chat = activity.conversation.id.split(";", 1)[0]
        decision, agents, account_id = await _decide(
            runtime, tenant_id=actor.tenant_id, user_id=actor.user_id
        )
        route = decision.route
        chosen = isinstance(route, DmStart) and route.agent == name
        if not (chosen or decision.may_switch_to(name)):
            return toast(UNCHECKED if isinstance(route, DmUnchecked) else DM_AGENT_REFUSED)
        agent = next(a for a in agents if a.name == name)
        await _pin(
            runtime,
            tenant_id=actor.tenant_id,
            chat=chat,
            agent=agent,
            account_id=account_id,
            remember_pick=True,
        )
        log.info("teams.dm.picked", agent=name)
        seed = await self._reader.latest_user_message(
            conversation_id=chat,
            channel_id=chat,
            user_id=actor.user_id,
            team_id=None,
            team_group_id=None,
            before=activity.reply_to_id,
            kind="personal",
        )
        if seed is None:
            return replace_card(
                notice_adaptive_card(f"You chose {name}.\n\nSend your message again.", error=False)
            )
        settings = runtime.settings.teams
        assert settings is not None
        self._dispatch(
            TeamsInbound(
                kind="dm",
                entra_tenant_id=settings.tenant_id,
                user_id=actor.user_id,
                conversation_id=chat,
                channel_id=chat,
                activity_id=activity.id,
                text=seed,
                service_url=activity.service_url,
                bot_name=display_bot_name(activity.recipient.name),
                user_name=activity.from_.name,
                timestamp=datetime.now(UTC).isoformat(),
                sender_id=activity.from_.id,
            )
        )
        return replace_card(notice_adaptive_card(f"You chose {name}.", error=False))

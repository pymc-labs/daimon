"""The data tools' seam: who a call answers for, from the caller's signed facts only.

`data_origin` turns the credential into a `DataOrigin`: a chat turn by its signed
execution grant (one live origin, `matching_turn`), a routine by its destination, and a
key, the hub or the CLI as a private place or the room the key was minted in. The model
passes nothing. `data_audience_for` applies the rule (`daimon.core.data_audience`) and
refuses when no data may be read; the tools (`daimon.adapters.mcp.tools.data`) call it first.
Readers are subjects in the form the store's identities take (`user_subject`).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, Final, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core.channel_audience import channel_has_outsiders
from daimon.core.data_audience import (
    CAP,
    DataAudience,
    DataOrigin,
    DataRefusal,
    Reader,
    RefusalReason,
    data_audience,
    user_subject,
)
from daimon.core.data_seal import reads_data
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.mcp_identity import matching_turn
from daimon.core.stores.agent_owners import owner_of
from daimon.core.stores.direct_messages import is_dm_thread
from daimon.core.stores.identity import accounts_of
from daimon.core.stores.room_viewers import read_viewers
from daimon.core.stores.routines import get_routine
from daimon.core.stores.turn_origins import get_active_origin, get_session_origin
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

_REFUSALS: Final[dict[RefusalReason, str]] = {
    "no_person": "Data sources answer only for a person in this conversation. Nothing was read.",
    "contained": (
        "People outside the team can read this conversation, so data sources stay closed "
        "here. Ask in a team channel or a direct message."
    ),
    "agent_request": "Only the agent's own conversations can read data sources. Nothing was read.",
}
#: Members a Slack room may page through before its viewers count as unknown.
_SLACK_PAGE_LIMIT: Final = 4 * CAP


async def data_audience_for(runtime: McpRuntime, auth: AuthIdentity) -> DataAudience:
    """Who a data call by `auth` answers for, or a refusal naming why none may run."""
    origin = await data_origin(runtime, auth, now=datetime.now(UTC))
    agent_id = auth.chat_agent_id or auth.agent_id
    async with runtime.session_factory() as session:
        owner = None if agent_id is None else await owner_of(session, auth.tenant_id, agent_id)
    found = data_audience(origin, agent_owner=owner)
    if isinstance(found, DataRefusal):
        raise ToolError(_REFUSALS[found.reason])
    return found


async def data_origin(runtime: McpRuntime, auth: AuthIdentity, *, now: datetime) -> DataOrigin:
    """Where a call by `auth` answers, from its signed facts only."""
    platform = auth.platform
    if auth.client:
        return DataOrigin("private", contained=True)
    if auth.chat_session_id is not None:
        return await _turn_origin(runtime, auth, now=now)
    if platform is None or auth.platform_user_id is None:
        return DataOrigin("none")
    asker = Reader(
        user_subject(platform, auth.external_id or "", auth.platform_user_id), auth.account_id
    )
    if auth.routine_id is not None:
        async with runtime.session_factory() as session:
            routine = await get_routine(session, auth.routine_id, tenant_id=auth.tenant_id)
        if routine is None:
            return DataOrigin("none")
        if routine.destination_kind in ("channel", "thread") and routine.destination_id:
            return await _room(runtime, auth, asker, routine.destination_id, None, now=now)
        return DataOrigin("private", asker)
    if auth.agent_id is not None and auth.bound_channel_id is not None:
        return await _room(runtime, auth, asker, auth.bound_channel_id, None, now=now)
    return DataOrigin("private", asker)


async def _turn_origin(runtime: McpRuntime, auth: AuthIdentity, *, now: datetime) -> DataOrigin:
    """A chat turn's origin by its execution grant: exactly one live turn of this agent."""
    grant = auth.chat_session_id
    if grant is None or auth.chat_agent_id is None or auth.platform is None:
        return DataOrigin("none")
    if auth.platform_user_id is None:
        return DataOrigin("none")
    async with runtime.session_factory() as session:
        held = await get_session_origin(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.requester_account_id or auth.account_id,
            platform=auth.platform,
            chat_session_id=grant,
            now=now,
        )
        turn = (
            None
            if held is None
            else matching_turn(
                [held],
                tenant_id=auth.tenant_id,
                agent_id=auth.chat_agent_id,
                thread_id=held.session_thread_id,
            )
        )
        parent = (
            None
            if turn is None or turn.helper_of is None
            else await get_active_origin(
                session,
                origin_id=turn.helper_of,
                tenant_id=auth.tenant_id,
                account_id=turn.account_id,
                platform=turn.platform,
                now=now,
            )
        )
    if turn is None or (turn.helper_of is not None and parent is None):
        return DataOrigin("none")
    asker = Reader(
        user_subject(turn.platform, auth.external_id or "", auth.platform_user_id),
        turn.account_id,
    )
    if turn.sender_foreign or turn.shared_channel:
        return DataOrigin("room", asker, contained=True)
    if is_dm_thread(turn.platform, turn.thread_id) or turn.platform == "web":
        origin = DataOrigin("private", asker)
    elif turn.platform not in ("discord", "slack"):
        return DataOrigin("none")  # a Teams room's readers are unknown
    else:
        origin = await _room(runtime, auth, asker, turn.parent_channel_id, turn.thread_id, now=now)
    if parent is None:
        return origin
    # A helper's turn: the asking agent must read data in the same room.
    place = FeaturePlace.of(
        auth.tenant_id,
        parent.platform,
        parent.parent_channel_id,
        parent.thread_id,
        agent_id=parent.responder_ma_agent_id,
    )
    caller_reads = await reads_data(runtime.session_factory, runtime.settings, place)
    viewers = origin.viewers if origin.kind == "room" else frozenset({asker})
    return DataOrigin(
        "agent_request", asker, viewers, origin.contained, caller_reads_data=caller_reads
    )


async def _room(
    runtime: McpRuntime,
    auth: AuthIdentity,
    asker: Reader,
    channel_id: str,
    thread_id: str | None,
    *,
    now: datetime,
) -> DataOrigin:
    """A room or thread: contained when outsiders may read it; its viewers, or None."""
    platform = cast(str, auth.platform)
    workspace = auth.external_id or ""
    async with runtime.session_factory() as session:
        audience = await channel_has_outsiders(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=thread_id,
            now=now,
        )
        if audience.contained:
            return DataOrigin("room", asker, contained=True)
        users = None
        if platform == "discord":
            stored = await read_viewers(
                session, tenant_id=auth.tenant_id, channel_id=thread_id or channel_id, now=now
            )
            # Stored as subjects of this tenant's server: back to its user ids.
            prefix = user_subject(platform, workspace, "")
            users = None if stored is None else frozenset(s.removeprefix(prefix) for s in stored)
    if platform == "slack" and auth.external_id is not None:
        users = await _slack_viewers(runtime, auth.external_id, channel_id)
    if users is None:
        return DataOrigin("room", asker, None)
    async with runtime.session_factory() as session:
        linked = await accounts_of(
            session, tenant_id=auth.tenant_id, platform=platform, external_ids=users
        )
    if platform == "slack" and auth.external_id is not None:
        users = await _slack_people(runtime, auth.external_id, users, linked)
        if users is None:
            return DataOrigin("room", asker, None)
    viewers = frozenset(
        Reader(user_subject(platform, workspace, user), linked.get(user)) for user in users
    )
    return DataOrigin("room", asker, viewers if len(viewers) <= CAP else None)


async def _slack_viewers(
    runtime: McpRuntime, team_id: str, channel_id: str
) -> frozenset[str] | None:
    """A Slack room's members, read now and never cached; None past the page limit or on
    any Slack error."""
    client = await slack_web_client(runtime, team_id=team_id)
    members: set[str] = set()
    cursor: str | None = None
    try:
        while len(members) <= _SLACK_PAGE_LIMIT:
            resp = await client.conversations_members(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel_id, limit=200, cursor=cursor
            )
            members.update(cast(list[str], resp["members"]))
            metadata = cast(dict[str, Any], resp.get("response_metadata") or {})
            cursor = str(metadata.get("next_cursor") or "") or None
            if cursor is None:
                return frozenset(members)
    except SlackApiError:
        return None
    return None


async def _slack_people(
    runtime: McpRuntime, team_id: str, users: Collection[str], linked: dict[str, uuid.UUID]
) -> frozenset[str] | None:
    """`users` without bots and deactivated users. A linked account is a person; any other
    member is looked up. None on any Slack error."""
    client: AsyncWebClient = await slack_web_client(runtime, team_id=team_id)
    people = {user for user in users if user in linked}
    try:
        for user in users:
            if user in linked:
                continue
            resp = await client.users_info(user=user)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            info = cast(dict[str, Any], resp["user"])
            if not (info.get("is_bot") or info.get("deleted") or user == "USLACKBOT"):
                people.add(user)
    except SlackApiError:
        return None
    return frozenset(people)

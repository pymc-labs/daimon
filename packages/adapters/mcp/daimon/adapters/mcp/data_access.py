"""The data tools' seam: who a call answers for, from the caller's signed facts only.

`data_origin` turns the credential into a `DataOrigin`: a chat turn by its signed
execution grant (one live origin, `matching_turn`), a routine by its destination, and a
key, the hub or the CLI as a private place or the room the key was minted in. The model
passes nothing. `data_audience_for` applies the rule (`daimon.core.data_audience`) and
refuses when no data may be read; the tools (`daimon.adapters.mcp.tools.data`) call it first.
Readers are subjects in the form the store's identities take (`user_subject`).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any, Final, cast

import psycopg
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.adapters.mcp.tools.teams._directory import graph_for, locate_channel, split_thread
from daimon.core.channel_audience import channel_has_outsiders
from daimon.core.config import Settings
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
from daimon.core.data_reading import reads_data
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.mcp_identity import matching_turn
from daimon.core.ports.gate import data_sources_on
from daimon.core.stores.agent_owners import owner_of
from daimon.core.stores.direct_messages import is_dm_thread
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.identity import accounts_of
from daimon.core.stores.room_viewers import read_viewers
from daimon.core.stores.routines import get_routine
from daimon.core.stores.turn_origins import get_active_origin, get_session_origin
from daimon.core.teams_graph import GraphUnavailable
from daimon.core.teams_threads import is_group_chat
from daimon.data import identity, pool, reads
from daimon.data.reading import open_read
from daimon.data.schema import Schemas
from daimon.data.sync.engine import Env
from fastmcp.exceptions import ToolError
from psycopg import Error
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

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
#: Slack lookups at once, and how long a member's bot or deactivated status is believed.
_SLACK_LOOKUPS: Final = 8
_SLACK_PERSON_TTL_S: Final = 300.0
_slack_person: dict[tuple[str, str], tuple[float, bool]] = {}


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


async def held_scopes(runtime: McpRuntime, auth: AuthIdentity) -> tuple[str, ...]:
    """The scopes the caller's answer may read, as the data tools resolve them; none when
    no data may be read here."""
    try:
        audience = await data_audience_for(runtime, auth)
        async with pool.connection(runtime.settings) as conn:
            schemas = Schemas.of(runtime.settings.data)
            salt = f"agent:{auth.chat_agent_id or auth.agent_id}"
            r = await open_read(conn, schemas, auth.tenant_id, audience, salt=salt)
    except (ToolError, Error):
        return ()
    return r.ctx.scopes


async def readable_by_audience(
    runtime: McpRuntime, auth: AuthIdentity, ref_sets: Sequence[Sequence[str]]
) -> list[bool]:
    """For each set of data items a memory recorded, whether everyone a call by `auth` answers
    for could read every one (`daimon.core.stores.data_memory`). Closed when unsure: data
    sources off or down, no audience here, or names hidden from someone."""
    if not ref_sets:
        return []
    settings = runtime.settings
    if not data_sources_on(settings):
        return [False] * len(ref_sets)
    schemas = Schemas.of(settings.data)
    try:
        audience = await data_audience_for(runtime, auth)
        async with pool.connection(settings) as conn:
            r = await open_read(conn, schemas, auth.tenant_id, audience, salt="memory")
            if r.entities is not None:  # what it saw may name what someone here may not see
                return [False] * len(ref_sets)
            env = Env.of(settings)
            return [await reads.readable(conn, schemas, r.ctx, refs, env) for refs in ref_sets]
    except (ToolError, psycopg.Error):
        log.info("data.memory_audience_unknown", tenant_id=str(auth.tenant_id))
        return [False] * len(ref_sets)


async def data_origin(runtime: McpRuntime, auth: AuthIdentity, *, now: datetime) -> DataOrigin:
    """Where a call by `auth` answers, from its signed facts only."""
    platform = auth.platform
    if auth.client:
        return DataOrigin("private", contained=True)
    if auth.chat_agent_id is not None and auth.chat_session_id is None:
        return DataOrigin("none")  # an agent's own credential reads only within a chat turn
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
    if auth.agent_id is not None:
        if not auth.coding_key:
            return DataOrigin("none")  # only a person's coding key reads, as its maker
        if auth.bound_channel_id is not None:
            # Minted in a limited channel: its rule follows the key.
            return await _room(runtime, auth, asker, auth.bound_channel_id, None, now=now)
    return DataOrigin("private", asker)


async def ledgers(runtime: McpRuntime, auth: AuthIdentity) -> list[str]:
    """The chat sessions a data read by `auth` lands on: its own, and for a helper's turn
    the asking turn's too, so what either saves after records it."""
    if auth.chat_session_id is None:
        return []
    found = await _turn_and_parent(runtime, auth, now=datetime.now(UTC))
    parent = None if found is None else found[1]
    asker = None if parent is None else parent.chat_session_id
    return [auth.chat_session_id, *([asker] if asker and asker != auth.chat_session_id else [])]


async def _turn_and_parent(
    runtime: McpRuntime, auth: AuthIdentity, *, now: datetime
) -> tuple[TurnOriginRow, TurnOriginRow | None] | None:
    """The live turn by its execution grant, and the asking turn when it is a helper's; None
    when either cannot be found."""
    grant = auth.chat_session_id
    if grant is None or auth.chat_agent_id is None or auth.platform is None:
        return None
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
        return None
    return turn, parent


async def _turn_origin(runtime: McpRuntime, auth: AuthIdentity, *, now: datetime) -> DataOrigin:
    """A chat turn's origin by its execution grant: exactly one live turn of this agent."""
    if auth.platform_user_id is None:
        return DataOrigin("none")
    found = await _turn_and_parent(runtime, auth, now=now)
    if found is None:
        return DataOrigin("none")
    turn, parent = found
    asker = Reader(
        user_subject(turn.platform, auth.external_id or "", auth.platform_user_id),
        turn.account_id,
    )
    if turn.sender_foreign or turn.shared_channel:
        return DataOrigin("room", asker, contained=True)
    if is_dm_thread(turn.platform, turn.thread_id) or turn.platform == "web":
        origin = DataOrigin("private", asker)
    elif turn.platform not in ("discord", "slack", "teams"):
        return DataOrigin("none")
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
    # From a DM (or web chat) the helper answers the asker alone, with their own access.
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
    if platform == "teams":
        users, outside = await _teams_viewers(runtime, auth, channel_id)
        if outside:
            return DataOrigin("room", asker, contained=True)
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


async def _teams_viewers(
    runtime: McpRuntime, auth: AuthIdentity, conversation_id: str
) -> tuple[frozenset[str] | None, bool]:
    """A Teams channel's or group chat's members by Entra id, read now, and whether anyone
    outside the organisation may read it: a guest, another tenant's member, a shared channel,
    or a channel that cannot be found. Members are None (public only) while the app lacks
    the consent to read them, or on any other failure."""
    client = runtime.teams_client
    if client is None:
        return None, False
    graph = graph_for(client)
    channel, _ = split_thread(conversation_id)
    try:
        if is_group_chat(channel):
            members = await graph.list_chat_members(channel)
        else:
            try:
                ref = await locate_channel(runtime, auth, client, channel)
            except ToolError:
                return None, True  # its sharing cannot be told
            if ref.channel_type == "shared":
                return None, True
            members = await graph.list_channel_members(ref.group_id, ref.channel_id)
    except GraphUnavailable as e:
        log.info("data.teams_viewers_unknown", reason=e.reason, status=e.status)
        return None, False
    home = (auth.external_id or "").lower()
    if any("guest" in m.roles or not home or (m.tenant_id or "").lower() != home for m in members):
        return None, True
    return frozenset(m.user_id.lower() for m in members if m.user_id), False


async def _slack_people(
    runtime: McpRuntime, team_id: str, users: Collection[str], linked: dict[str, uuid.UUID]
) -> frozenset[str] | None:
    """`users` without bots and deactivated users. A linked account is a person; any other
    member is looked up, a few at a time, and believed for a few minutes. None on any Slack
    error."""
    client: AsyncWebClient = await slack_web_client(runtime, team_id=team_id)
    gate = asyncio.Semaphore(_SLACK_LOOKUPS)

    async def is_person(user: str) -> bool:
        now = time.monotonic()
        known = _slack_person.get((team_id, user))
        if known is not None and known[0] > now:
            return known[1]
        async with gate:
            resp = await client.users_info(user=user)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        info = cast(dict[str, Any], resp["user"])
        person = not (info.get("is_bot") or info.get("deleted") or user == "USLACKBOT")
        if len(_slack_person) > 50 * CAP:
            _slack_person.clear()
        _slack_person[(team_id, user)] = (now + _SLACK_PERSON_TTL_S, person)
        return person

    unlinked = [user for user in users if user not in linked]
    lookups = [asyncio.ensure_future(is_person(user)) for user in unlinked]
    try:
        verdicts = await asyncio.gather(*lookups)
    except SlackApiError:
        return None
    finally:  # one failed lookup stops the rest
        for lookup in lookups:
            lookup.cancel()
    people = {user for user in users if user in linked}
    return frozenset(people | {u for u, ok in zip(unlinked, verdicts, strict=True) if ok})


async def link_github(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    account_id: uuid.UUID,
    github_user_id: int,
) -> None:
    """A personal GitHub link, as the person's GitHub identity in the data store where the
    workspace has data sources on (`identity.github_proof`). Never fails the link."""
    if not data_sources_on(settings) or not await pool.ready(settings):
        return
    try:
        async with sessionmaker() as session:
            place = FeaturePlace(tenant_id, platform)
            if not (await feature_in(session, settings, place, "data_sources")).on:
                return
        async with pool.connection(settings) as conn:
            await identity.link(
                conn, Schemas.of(settings.data), tenant_id=tenant_id, account_id=account_id,
                proof=identity.github_proof(github_user_id),
            )  # fmt: skip
    except Exception as e:
        log.warning("data.github_link_failed", why=type(e).__name__)

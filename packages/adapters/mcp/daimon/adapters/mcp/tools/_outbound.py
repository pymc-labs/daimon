"""Outbound etiquette at the tool boundary: every tool post is rendered, counted and gated here.

Rules are in `daimon.core.outbound`; the per-turn tally is a locked row
(`daimon.core.stores.reach_tallies`), since several MCP instances serve one turn.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import aiohttp
import httpx
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import list_running_origins
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.core.outbound.mentions import (
    Platform,
    RenderedPost,
    attribution_line,
    caption_length,
    mentioned_user_ids,
    ping_users,
    render_post,
)
from daimon.core.outbound.opt_out import opt_out_names, tag_opt_outs
from daimon.core.outbound.reach import (
    NO_CARD,
    NO_PLAN,
    SHORT_CAPTION,
    TOO_LONG,
    ReachPlan,
    ReachTally,
    check_reach,
    client_tally_key,
    needs_approval,
)
from daimon.core.security_audit import record_denial
from daimon.core.stores.direct_messages import DM_SCOPE_PREFIX
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.file_uploads import get_upload
from daimon.core.stores.reach_tallies import lock_tally, save_tally
from daimon.core.stores.routines import get_routine
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

log = structlog.get_logger(__name__)

_MEDIA_EXTENSIONS = frozenset(
    {"mp4", "mov", "webm", "mkv", "avi", "m4v", "mp3", "wav", "m4a", "ogg", "flac", "aac"}
)


@dataclass(frozen=True)
class OutgoingPost:
    """One post a tool is about to send, and where it lands."""

    text: str
    place: str
    new_thread: bool = False
    media: bool = False
    attribute: bool = True


TurnKind = Literal["live", "unattended", "outside"]


@dataclass(frozen=True)
class TurnScope:
    """What the gate knows about the calling turn.

    ``live``: a person started the turn and can answer a card. ``unattended``: the
    server itself started it (a routine, timer, watch or relay). ``outside``:
    everything else, an outside client with no card, so it fails closed.
    """

    key: str
    kind: TurnKind
    own_places: frozenset[str]
    requester_id: str | None
    routine_name: str | None = None
    origin: TurnOriginRow | None = None
    # Where a new thread stays in the conversation: the own places and their parent channel.
    own_channels: frozenset[str] = frozenset()

    @property
    def interactive(self) -> bool:
        return self.kind != "unattended"


def is_media(filename: str) -> bool:
    return filename.rsplit(".", 1)[-1].lower() in _MEDIA_EXTENSIONS if "." in filename else False


def _platform(auth: AuthIdentity) -> Platform:
    if auth.platform in ("slack", "teams"):
        return auth.platform
    return "discord"


def _own_origin(running: list[TurnOriginRow], session_id: str) -> TurnOriginRow | None:
    """The calling turn's origin, or None when it cannot be told apart.

    Native runtimes bind the grant to one origin's id. A session grant shared by
    concurrent turns is ambiguous, so it gets no card and no other turn's tally.
    """
    exact = [o for o in running if str(o.id) == session_id]
    if exact:
        return exact[0]
    return running[0] if len(running) == 1 else None


def canonical_place(place: str) -> str:
    """A place with Teams' ``;messageid=`` variants folded to ``<channel>;messageid=<root>``.

    Teams ids arrive with the parameter in any case, followed by more parameters, or
    empty; the thread is the channel plus that one id. Other platforms' ids pass through.
    """
    head, *params = place.strip().split(";")
    for param in params:
        key, _, value = param.partition("=")
        if key.lower() == "messageid" and value:
            return f"{head};messageid={value}"
    return head


def _own_places(origin: TurnOriginRow) -> frozenset[str]:
    """Every id a plain post into the turn's own thread or direct chat can carry.

    The parent channel is not one: a top-level post there reaches other people, as on
    Discord. Slack tools address a thread as ``channel:thread_ts``, which no origin field
    holds. A direct chat's platform id is its own; elsewhere ``platform_channel_id`` is the
    channel the message was posted in, own only when it is not the parent channel.
    """
    places = {origin.thread_id}
    if origin.platform == "slack" and origin.thread_id != origin.parent_channel_id:
        places.add(f"{origin.parent_channel_id}:{origin.thread_id}")
    chat = origin.platform_channel_id
    if chat and (origin.thread_id.startswith(DM_SCOPE_PREFIX) or chat != origin.parent_channel_id):
        places.add(chat)
    return frozenset(canonical_place(p) for p in places)


async def turn_scope(runtime: McpRuntime, auth: AuthIdentity) -> TurnScope:
    """Unattended only when the server proves it started the turn; anything not a
    live turn with a card is an outside client."""
    if auth.routine_id is not None:
        async with runtime.session_factory() as session:
            routine = await get_routine(session, auth.routine_id, tenant_id=auth.tenant_id)
        return TurnScope(
            key=f"routine:{auth.execution_id or auth.routine_id}",
            kind="unattended",
            own_places=frozenset(),
            requester_id=routine.created_by_user_id if routine else None,
            routine_name=(routine.name or routine.agent_name) if routine else None,
        )
    if auth.chat_session_id is not None:
        running = await list_running_origins(runtime, auth)
        origin = _own_origin(running, auth.chat_session_id)
        if origin is None and running:
            # Ambiguous: every running turn's conversation is the caller's own; elsewhere
            # still fails closed, with no card and no other turn's tally.
            places = frozenset[str]().union(*(_own_places(o) for o in running))
            return TurnScope(
                key=client_tally_key(str(auth.token_jti or auth.account_id), datetime.now(UTC)),
                kind="outside",
                own_places=places,
                requester_id=auth.platform_user_id,
                own_channels=places | {o.parent_channel_id for o in running},
            )
        if origin is not None:
            # Only the server records origins, and it marks the ones nobody attends.
            return TurnScope(
                key=f"turn:{origin.id}",
                kind="live" if origin.attended else "unattended",
                own_places=_own_places(origin),
                requester_id=auth.platform_user_id,
                origin=origin,
                own_channels=_own_places(origin) | {origin.parent_channel_id},
            )
    return TurnScope(
        key=client_tally_key(str(auth.token_jti or auth.account_id), datetime.now(UTC)),
        kind="outside",
        own_places=frozenset(),
        requester_id=auth.platform_user_id,
    )


OPT_OUT_UNCHECKED = "Not sent: could not check whether that person accepts messages; try later."

_NAME_TTL_SECONDS = 3600.0
_NAME_CACHE_MAX = 2048
_slack_names: dict[tuple[str, str], tuple[float, str]] = {}


async def _slack_user_name(runtime: McpRuntime, team_id: str, user_id: str) -> str | None:
    """A Slack person's name, looked up once an hour per person rather than once per post."""
    key = (team_id, user_id)
    hit = _slack_names.get(key)
    if hit is not None and time.monotonic() - hit[0] < _NAME_TTL_SECONDS:
        return hit[1]
    from daimon.adapters.mcp.tools.slack._client import slack_web_client
    from slack_sdk.errors import SlackApiError

    try:
        client = await slack_web_client(runtime, team_id=team_id)
        response = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]
    except (SlackApiError, ToolError, aiohttp.ClientError, TimeoutError):
        # No name means no attribution line, never a refused post; a failure is not cached.
        return None
    user = cast("dict[str, Any]", response.get("user") or {})  # pyright: ignore[reportUnknownMemberType]
    profile = cast("dict[str, Any]", user.get("profile") or {})
    name = profile.get("display_name") or user.get("real_name")
    if not name:
        return None
    if len(_slack_names) >= _NAME_CACHE_MAX:
        _slack_names.clear()
    _slack_names[key] = (time.monotonic(), str(name))
    return str(name)


async def _requester_name(
    runtime: McpRuntime, auth: AuthIdentity, place: str, user_id: str
) -> str | None:
    """A display name for Slack and Teams, which have no mention that does not ping.

    The live lookup first, then the name Daimon stored, then "someone": the line
    saying who asked is never dropped.
    """
    name: str | None = None
    if auth.platform == "slack" and auth.external_id:
        name = await _slack_user_name(runtime, auth.external_id, user_id)
    elif auth.platform == "teams" and runtime.teams_client is not None:
        try:
            member = await runtime.teams_client.get_member(place.split(";", 1)[0], user_id)
            name = member.name if member else None
        except (httpx.HTTPError, ValueError):
            name = None
    if name:
        return name
    try:
        stored = await opt_out_names(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=_platform(auth),
            user_ids={user_id},
        )
    except Exception:  # named boundary: a missing name never stops the post
        log.warning("outbound.requester_name_lookup_failed", exc_info=True)
        stored = {}
    return stored.get(user_id) or "someone"


async def _opt_outs(
    runtime: McpRuntime, auth: AuthIdentity, platform: Platform, user_ids: set[str]
) -> tuple[frozenset[str], dict[str, str], bool]:
    """Who opted out of tags, their names, and whether that could be checked.

    A failed lookup never loses the post. Discord keeps the mention text and pings
    nobody; Slack names everyone unchecked, as its replies do; Teams posts tag no one.
    """
    try:
        skip = await tag_opt_outs(
            runtime.session_factory, tenant_id=auth.tenant_id, platform=platform, user_ids=user_ids
        )
        names = await opt_out_names(
            runtime.session_factory, tenant_id=auth.tenant_id, platform=platform, user_ids=skip
        )
    except Exception:  # named boundary: the post goes out, pinging no one unchecked
        log.warning("outbound.opt_out_lookup_failed", exc_info=True)
        return (frozenset(user_ids) if platform == "slack" else frozenset()), {}, False
    return skip, names, True


async def prepare_posts(
    runtime: McpRuntime,
    auth: AuthIdentity,
    posts: Sequence[OutgoingPost],
    *,
    plan: ReachPlan | None,
    scope: TurnScope | None = None,
    validate: Callable[[list[RenderedPost]], None] | None = None,
    pings: bool = True,
) -> list[RenderedPost]:
    """Render every post, then check length, captions and the reach budget as a whole.

    ``validate`` checks the rendered posts, attribution included, and raises to
    refuse the call. Nothing has posted when this raises, and a refused call
    counts nothing; on success the call's reach is counted. ``pings=False`` is for a
    post sent without mentions: its people are not counted, since nobody is reached.
    """
    platform = _platform(auth)
    scope = scope or await turn_scope(runtime, auth)
    mentioned = {uid for post in posts for uid in mentioned_user_ids(post.text, platform)}
    if scope.requester_id:
        mentioned.add(scope.requester_id)
    skip, names, checked = await _opt_outs(runtime, auth, platform, mentioned)
    pings = pings and checked
    reach = [_reaches(post, scope, platform, skip if pings else None) for post in posts]
    credited = [p.place for p, r in zip(posts, reach, strict=True) if p.attribute and r]
    line = await _attribution(runtime, auth, scope, credited[0], skip) if credited else None
    # Only a post that reaches other people says who asked; the own conversation is theirs.
    rendered = [
        render_post(
            post.text,
            platform,
            do_not_tag=skip,
            names=names,
            attribution=line if post.attribute and r else None,
        )
        for post, r in zip(posts, reach, strict=True)
    ]
    ping_users.set(frozenset(uid for r in rendered for uid in r.user_ids) if pings else frozenset())
    if validate is not None:
        validate(rendered)
    if not scope.interactive:
        return rendered
    settings = runtime.settings.outbound
    # Length and captions guard other people's attention, item by item.
    for post in (p for p, r in zip(posts, reach, strict=True) if r):
        if settings.max_length and len(post.text) > settings.max_length:
            record_denial("outbound_too_long")
            raise ToolError(TOO_LONG)
        if (
            post.media
            and settings.min_media_caption
            and caption_length(post.text, platform) < settings.min_media_caption
        ):
            record_denial("outbound_short_caption")
            raise ToolError(SHORT_CAPTION)
    reaching = [(p, out) for p, out, r in zip(posts, rendered, reach, strict=True) if r]
    call = ReachTally(
        threads=sum(p.new_thread for p, _ in reaching),
        places=frozenset(p.place for p, _ in reaching if not p.new_thread),
        people=frozenset(uid for _, out in reaching for uid in out.user_ids) - skip
        if pings
        else frozenset(),
    )
    if not reaching:
        return rendered
    if plan is None and settings.reach_budget_enabled:
        # The own conversation never needs a plan; reaching others does, so the card shows it all.
        record_denial("reach_no_plan")
        raise ToolError(NO_PLAN)
    async with runtime.session_factory() as session, session.begin():
        so_far, approved = await lock_tally(session, tenant_id=auth.tenant_id, turn_key=scope.key)
        if settings.reach_budget_enabled:
            _gate(scope, so_far, approved, call, plan or call.counts(), runtime, auth)
        await save_tally(
            session, tenant_id=auth.tenant_id, turn_key=scope.key, tally=so_far.plus(call)
        )
    return rendered


async def _attribution(
    runtime: McpRuntime, auth: AuthIdentity, scope: TurnScope, place: str, skip: frozenset[str]
) -> str | None:
    """The "requested by" line: a Discord requester is tagged without a ping unless they
    opted out; everyone else, web and agent chat included, by plain name, never dropped."""
    platform = _platform(auth)
    requester = scope.requester_id
    tag = auth.platform == "discord" and requester is not None and requester not in skip
    requester_name = (
        await _requester_name(runtime, auth, place, requester) if requester and not tag else None
    )
    return attribution_line(
        platform,
        requester_id=requester if tag else None,
        requester_name=requester_name,
        routine_name=scope.routine_name,
    )


def _reaches(
    post: OutgoingPost, scope: TurnScope, platform: Platform, skip: frozenset[str] | None
) -> bool:
    """Whether a post goes to other people: outside the own conversation, or a new thread
    outside the own channel or tagging someone else. ``skip`` is None when nobody is pinged."""
    if not post.new_thread:
        return _outside(post, scope)
    if canonical_place(post.place) not in scope.own_channels:
        return True
    if skip is None:
        return False
    tagged = set(mentioned_user_ids(post.text, platform)) - skip - {scope.requester_id}
    return bool(tagged)


def _outside(post: OutgoingPost, scope: TurnScope) -> bool:
    place = canonical_place(post.place)
    return place not in scope.own_places and place != scope.requester_id


def _gate(
    scope: TurnScope,
    so_far: ReachTally,
    approved: ReachPlan | None,
    call: ReachTally,
    plan: ReachPlan,
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> None:
    settings = runtime.settings.outbound
    limits = ReachPlan(
        threads=settings.max_threads, places=settings.max_places, people=settings.max_people
    )
    verdict = check_reach(plan=plan, so_far=so_far, call=call, limits=limits, approved=approved)
    if verdict == "ok":
        return
    record_denial(f"reach_{verdict}")
    log.info("reach.over_budget", key=scope.key, has_card=scope.origin is not None)
    if scope.kind != "live" or auth.no_confirmation_surface:
        # No card can be shown (an outside client, or a backend without one): fail closed.
        raise ToolError(NO_CARD)
    raise ToolError(needs_approval(plan.widest(so_far.plus(call).counts()), limits))


async def approve(
    runtime: McpRuntime, auth: AuthIdentity, scope: TurnScope, plan: ReachPlan
) -> None:
    """Record the plan the requester approved on the card, for the rest of the turn."""
    async with runtime.session_factory() as session, session.begin():
        _, approved = await lock_tally(session, tenant_id=auth.tenant_id, turn_key=scope.key)
        widest = plan if approved is None else approved.widest(plan)
        await save_tally(session, tenant_id=auth.tenant_id, turn_key=scope.key, approved=widest)
    log.info("reach.approved", key=scope.key, **plan.model_dump())


class ThreadDraft(BaseModel):
    """One more thread for a batched create_thread call."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Thread title, 1-100 characters, where threads have titles.")
    content: str = Field(description="The thread's first message.")


class MessageDraft(BaseModel):
    """One more message for a batched send_message call."""

    model_config = ConfigDict(frozen=True)

    channel_id: str = Field(description="Where this message goes, as send_message takes it.")
    content: str = Field(description="The message text.")


async def any_media_upload(runtime: McpRuntime, auth: AuthIdentity, handles: Sequence[str]) -> bool:
    """True when a staged file is an image or video, so the post needs a real caption."""
    async with runtime.session_factory() as session:
        for handle in handles:
            row = await get_upload(session, tenant_id=auth.tenant_id, handle_id=handle)
            if row is not None and is_media(row.display_filename):
                return True
    return False


async def approve_reach_impl(
    runtime: McpRuntime, auth: AuthIdentity, plan: ReachPlan
) -> ReachApproved:
    """Record the approved plan once the card's Approve is proven; refuse where no card can show."""
    scope = await turn_scope(runtime, auth)
    if scope.kind != "live" or scope.origin is None or auth.no_confirmation_surface:
        record_denial("reach_no_card")
        raise ToolError(NO_CARD)
    gap = await session_card_gap(runtime, auth, scope.origin, tool_name="approve_reach")
    if gap is not None:
        record_denial("reach_no_card")
        raise ToolError(NO_CARD)
    await approve(runtime, auth, scope, plan)
    return ReachApproved(approved=True, plan=plan)


class ReachApproved(BaseModel):
    """The person approved this reach on the card; post within it this turn."""

    approved: bool
    plan: ReachPlan


async def refuse_dm_to_opted_out(
    runtime: McpRuntime, auth: AuthIdentity, recipient_id: str
) -> None:
    """A private message notifies its recipient, so it is not sent on someone else's behalf
    to a person who asked never to be tagged. Their own request to hear from Daimon stands."""
    if recipient_id == auth.platform_user_id:
        return
    skip, _, checked = await _opt_outs(runtime, auth, _platform(auth), {recipient_id})
    if not checked:
        # A DM always notifies, so one to someone unchecked is held, not sent.
        record_denial("outbound_opt_out_unchecked_dm")
        raise ToolError(OPT_OUT_UNCHECKED)
    if skip:
        record_denial("outbound_opted_out_dm")
        raise ToolError(
            "That person asked not to be tagged or pinged, so they are not messaged on "
            "someone else's request. Tell the requester; do not retry."
        )


async def render_edit(runtime: McpRuntime, auth: AuthIdentity, text: str, *, place: str) -> str:
    """Edits notify nobody, so they get no attribution and no count, but the length rule holds.

    An edit can turn a short post into a wall of text, so it meets R4 as a post would.
    """
    platform = _platform(auth)
    max_length = runtime.settings.outbound.max_length
    if max_length and len(text) > max_length:
        scope = await turn_scope(runtime, auth)
        if scope.interactive and _outside(OutgoingPost(text=text, place=place), scope):
            record_denial("outbound_too_long")
            raise ToolError(TOO_LONG)
    skip, names, _ = await _opt_outs(
        runtime, auth, platform, set(mentioned_user_ids(text, platform))
    )
    ping_users.set(frozenset())
    return render_post(text, platform, do_not_tag=skip, names=names, attribution=None).text

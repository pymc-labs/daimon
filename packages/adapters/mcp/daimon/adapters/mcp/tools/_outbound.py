"""Outbound etiquette at the tool boundary: every tool post is rendered, counted and gated here.

Rules are in `daimon.core.outbound`; the per-turn tally is a locked row
(`daimon.core.stores.reach_tallies`), since several MCP instances serve one turn.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
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
    SHORT_CAPTION,
    TOO_LONG,
    ReachPlan,
    ReachTally,
    check_reach,
    needs_approval,
    plan_too_small,
)
from daimon.core.security_audit import record_denial
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

    @property
    def interactive(self) -> bool:
        return self.kind != "unattended"


def is_media(filename: str) -> bool:
    return filename.rsplit(".", 1)[-1].lower() in _MEDIA_EXTENSIONS if "." in filename else False


def _platform(auth: AuthIdentity) -> Platform:
    if auth.platform in ("slack", "teams"):
        return auth.platform
    return "discord"


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
        origin = max(running, key=lambda o: o.created_at) if running else None
        if origin is not None:
            # Only the server records origins, and it marks the ones nobody attends.
            return TurnScope(
                key=f"turn:{origin.id}",
                kind="live" if origin.attended else "unattended",
                own_places=frozenset(
                    {origin.thread_id, origin.platform_channel_id or origin.thread_id}
                ),
                requester_id=auth.platform_user_id,
                origin=origin,
            )
    return TurnScope(
        key=f"client:{auth.token_jti or auth.account_id}",
        kind="outside",
        own_places=frozenset(),
        requester_id=auth.platform_user_id,
    )


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
    """A display name for Slack and Teams, which have no mention that does not ping."""
    if auth.platform == "slack" and auth.external_id:
        return await _slack_user_name(runtime, auth.external_id, user_id)
    if auth.platform == "teams" and runtime.teams_client is not None:
        try:
            member = await runtime.teams_client.get_member(place.split(";", 1)[0], user_id)
        except (httpx.HTTPError, ValueError):
            return None
        return member.name if member else None
    return None


async def prepare_posts(
    runtime: McpRuntime,
    auth: AuthIdentity,
    posts: Sequence[OutgoingPost],
    *,
    plan: ReachPlan | None,
    scope: TurnScope | None = None,
) -> list[RenderedPost]:
    """Render every post, then check length, captions and the reach budget as a whole.

    Nothing has posted when this raises; on success the call's reach is counted.
    """
    platform = _platform(auth)
    scope = scope or await turn_scope(runtime, auth)
    mentioned = {uid for post in posts for uid in mentioned_user_ids(post.text, platform)}
    if scope.requester_id:
        mentioned.add(scope.requester_id)
    skip = await tag_opt_outs(
        runtime.session_factory, tenant_id=auth.tenant_id, platform=platform, user_ids=mentioned
    )
    names = await opt_out_names(
        runtime.session_factory, tenant_id=auth.tenant_id, platform=platform, user_ids=skip
    )
    # An opted-out requester is credited by plain name, never by tag.
    requester_name = (
        names.get(scope.requester_id)
        if scope.requester_id and scope.requester_id in skip
        else (
            await _requester_name(runtime, auth, posts[0].place, scope.requester_id)
            if platform != "discord" and posts and scope.requester_id
            else None
        )
    )
    line = attribution_line(
        platform,
        requester_id=(
            scope.requester_id if platform == "discord" and scope.requester_id not in skip else None
        ),
        requester_name=requester_name,
        routine_name=scope.routine_name,
    )
    rendered = [
        render_post(
            post.text,
            platform,
            do_not_tag=skip,
            names=names,
            attribution=line if post.attribute else None,
        )
        for post in posts
    ]
    ping_users.set(frozenset(uid for r in rendered for uid in r.user_ids))
    if not scope.interactive:
        return rendered
    settings = runtime.settings.outbound
    # Length and captions guard other people's attention; the requester's own thread
    # and their own DM are theirs to fill.
    for post in (p for p in posts if _outside(p, scope)):
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
    call = ReachTally(
        threads=sum(post.new_thread for post in posts),
        places=frozenset(p.place for p in posts if not p.new_thread and _outside(p, scope)),
        people=frozenset(uid for r in rendered for uid in r.user_ids) - skip,
    )
    async with runtime.session_factory() as session, session.begin():
        so_far, approved = await lock_tally(session, tenant_id=auth.tenant_id, turn_key=scope.key)
        if settings.reach_budget_enabled:
            _gate(scope, so_far, approved, call, plan or call.counts(), runtime, auth)
        await save_tally(
            session, tenant_id=auth.tenant_id, turn_key=scope.key, tally=so_far.plus(call)
        )
    return rendered


def _outside(post: OutgoingPost, scope: TurnScope) -> bool:
    return post.place not in scope.own_places and post.place != scope.requester_id


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
    if verdict == "plan_too_small":
        raise ToolError(plan_too_small(so_far.plus(call).counts()))
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
    skip = await tag_opt_outs(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        platform=_platform(auth),
        user_ids={recipient_id},
    )
    if skip:
        record_denial("outbound_opted_out_dm")
        raise ToolError(
            "That person asked not to be tagged or pinged, so they are not messaged on "
            "someone else's request. Tell the requester; do not retry."
        )


async def render_edit(runtime: McpRuntime, auth: AuthIdentity, text: str) -> str:
    """Edits notify nobody, so they are only rendered: no attribution and no count."""
    platform = _platform(auth)
    skip = await tag_opt_outs(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        platform=platform,
        user_ids=set(mentioned_user_ids(text, platform)),
    )
    ping_users.set(frozenset())
    names = await opt_out_names(
        runtime.session_factory, tenant_id=auth.tenant_id, platform=platform, user_ids=skip
    )
    return render_post(text, platform, do_not_tag=skip, names=names, attribution=None).text

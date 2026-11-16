"""Opt-in Slack IM conversations and /dm-me workspace selection."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from time import monotonic
from typing import Any, cast

import aiohttp
import anthropic
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.agent_post import post_as_agent
from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.errors import bound_request_id, render_error
from daimon.adapters.slack.gating import is_slack_connect_external
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime, admission_refusal_message
from daimon.adapters.slack.user_groups import user_group_lookup
from daimon.core.agent_identity import (
    AgentIdentity,
    identity_enabled_for,
    is_builtin_agent,
    resolve_agent_identity,
)
from daimon.core.authz import Subject, build_subject
from daimon.core.config import DirectMessagePolicy, Settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.direct_messages import (
    DM_AGENT_REFUSED,
    ColdDmNotices,
    admit_cold_dm,
    cold_dm_text,
    is_sealed_slack_message,
    reply_to_dm,
    require_dm_enabled,
    require_unsealed_source,
    sealed_channel_ids,
    start_cold_dm,
    start_dm,
)
from daimon.core.dm_access import decide_dm, list_dm_agents, lookup_dm_groups
from daimon.core.dm_debounce import DmBurst, debounce_seconds, split_approvals
from daimon.core.dm_routing import DmChoose, DmDecision, DmRefused, DmStart, DmUnchecked, Route
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.handoff_context import TranscriptTurn
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import DirectMessageRow, dm_enabled, get_conversation
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import find_platform_principal
from daimon.core.turn.admission import admit
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn_hold import RequesterOrg
from daimon.core.workspace_settings import set_dms
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger(__name__)
_SEEN_REACTION = "eyes"
_NOT_SENT = "Your message wasn't sent. Please send it again."
_ERRORS = (
    DaimonError,
    MAResolverMissError,
    SlackApiError,
    anthropic.APIError,
    SQLAlchemyError,
    InvalidToken,
    aiohttp.ClientError,
    TimeoutError,
)


def is_direct_message_event(event: dict[str, Any]) -> bool:
    return bool(
        event.get("type") == "message"
        and event.get("channel_type") == "im"
        and not event.get("subtype")
        and not event.get("bot_id")
        and event.get("user")
    )


_REAUTHORIZE = (
    "Daimon can't use DMs in this workspace yet.\n\n"
    "Ask a workspace admin to finish Daimon's DM setup."
)
#: What an operator does about `_REAUTHORIZE`; logged, never posted.
DM_SETUP_STEPS = (
    "Reinstall or reauthorize the Slack app to grant the im:history and im:write bot scopes, "
    "subscribe it to the message.im event, and enable the App Home Messages tab."
)
DM_SETUP_DOCS = "https://pymc-labs.github.io/daimon/slack/#dm-setup"
_SCOPE_ERRORS = frozenset(
    {
        "missing_scope",
        "token_revoked",
        "token_expired",
        "invalid_auth",
        "not_authed",
        "account_inactive",
    }
)


def _log_dm_setup_needed(*, team_id: str, cause: str) -> None:
    log.warning(
        "slack.dm.setup_incomplete",
        team_id=team_id,
        cause=cause,
        steps=DM_SETUP_STEPS,
        docs=DM_SETUP_DOCS,
    )


def _error_message(exc: Exception, *, settings: Settings, team_id: str) -> str:
    """What a /dm-me or DM failure tells the person.

    Missing DM scopes and admission refusals have their own words. Everything
    else goes through `render_error`: `UserFacingError` text is shown, any
    other failure gets its cause lines and a Ref, never the exception's text.
    """
    if isinstance(exc, SlackApiError):
        code = exc.response.get("error")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  # SlackApiError.response is untyped
        if code in _SCOPE_ERRORS:
            _log_dm_setup_needed(team_id=team_id, cause=str(code))  # pyright: ignore[reportUnknownArgumentType]  # SlackApiError.response is untyped
            return _REAUTHORIZE
    if isinstance(exc, AdmissionDenied):
        return admission_refusal_message(
            exc.reason, settings, in_dm=True, cause=exc.cause, provider=exc.provider
        )
    return render_error(exc, request_id=bound_request_id())


async def _live_role(
    client: AsyncWebClient, *, user_id: str, team_id: str, require_im_scopes: bool = False
) -> Role:
    response = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]
    if require_im_scopes:
        headers = cast(dict[str, str], response.headers or {})  # pyright: ignore[reportUnknownMemberType]  # SDK headers are untyped
        granted = next(
            (str(value) for key, value in headers.items() if key.lower() == "x-oauth-scopes"), ""
        )
        if not {"im:history", "im:write"} <= {scope.strip() for scope in granted.split(",")}:
            _log_dm_setup_needed(team_id=team_id, cause="im_scopes_not_granted")
            raise UserFacingError(_REAUTHORIZE)
    user = cast(dict[str, Any], response.get("user", {}))
    if (
        user.get("team_id") != team_id
        or user.get("deleted")
        or user.get("is_bot")
        or user.get("is_stranger")
    ):
        raise UserFacingError(
            "Only current members of this workspace can use its DM conversations."
        )
    return (
        Role.ADMIN
        if any(user.get(key) for key in ("is_admin", "is_owner", "is_primary_owner"))
        else Role.USER
    )


async def handle_dm_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    team_id = str(payload.get("team_id") or "")
    user_id = str(payload.get("user_id") or "")
    channel_id = str(payload.get("channel_id") or "")
    if not team_id or not user_id or not channel_id:
        return
    client: AsyncWebClient | None = None
    try:
        client = await resolve_web_client(runtime, team_id=team_id)
        if client is None:
            return
        action = str(payload.get("text") or "").strip().lower()
        role = await _live_role(
            client, user_id=user_id, team_id=team_id, require_im_scopes=action != "disable"
        )
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        reply = refusal = None
        if action in {"enable", "disable"}:
            if role is not Role.ADMIN:
                refusal = "Only a workspace admin can turn Daimon DMs on or off here."
            else:
                async with runtime.sessionmaker.begin() as session:
                    await set_dms(
                        session,
                        tenant_id=tenant_id,
                        subject=Subject(is_admin=True, platform_user_id=user_id),
                        platform="slack",
                        enabled=action == "enable",
                    )
                reply = (
                    "Daimon DMs are on for this workspace."
                    if action == "enable"
                    else "Daimon DMs are off for this workspace."
                )
        elif action in {"", "move"}:
            await require_dm_enabled(runtime.turn_deps, tenant_id=tenant_id)
            if channel_id.startswith("D"):
                raise UserFacingError(
                    "You can't start a private conversation from here.\n\n"
                    "Run /dm-me in the channel you want to continue from."
                )
            admission = await admit(
                runtime.turn_deps,
                origin="chat",
                tenant_id=tenant_id,
                requester_org=RequesterOrg.HOST,
                platform="slack",
                external_user_id=user_id,
                channel_id=channel_id,
                role=role,
                is_dm=True,
                dm_source_channel_id=channel_id,
                now=datetime.now(UTC),
            )
            require_unsealed_source(admission)
            response = await client.conversations_history(channel=channel_id, limit=12)  # pyright: ignore[reportUnknownMemberType]
            messages = cast(list[dict[str, Any]], response.get("messages", []))
            # /dm-me carries no thread_ts, so admission cannot see a thread sealed
            # on its own: drop such threads' roots and broadcast replies here.
            sealed = await sealed_channel_ids(runtime.turn_deps, tenant_id=tenant_id)
            messages = [
                item
                for item in messages
                if not is_sealed_slack_message(sealed, channel_id=channel_id, message=item)
            ]
            context = [
                TranscriptTurn(
                    role="user", text=f"{item.get('user', 'agent')}: {item.get('text', '')}"
                )
                for item in reversed(messages)
                if item.get("text")
            ]
            opened = await client.conversations_open(users=user_id)  # pyright: ignore[reportUnknownMemberType]
            dm_channel = str(cast(dict[str, Any], opened["channel"])["id"])
            source_url = f"slack://channel?team={team_id}&id={channel_id}"
            groups = await lookup_dm_groups(
                runtime.sessionmaker,
                tenant_id=tenant_id,
                user_id=user_id,
                groups=user_group_lookup(runtime, client, team_id=team_id, fresh=True),
            )
            await start_dm(
                runtime.turn_deps,
                admission,
                tenant_id=tenant_id,
                platform="slack",
                workspace_id=team_id,
                route_key=f"{team_id}:{dm_channel}",
                channel_id=dm_channel,
                external_user_id=user_id,
                source_url=source_url,
                source_channel_id=channel_id,
                source_thread_id=None,
                context=context,
                # Each copied message's thread, so a later thread-only seal ends
                # this DM too.
                source_thread_keys=[
                    f"{channel_id}:{item.get('thread_ts') or item.get('ts')}" for item in messages
                ],
                groups=groups,
            )
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=dm_channel,
                **notice_fields(
                    f"Continuing from <#{channel_id}>.\n\nSend your next message here. "
                    "To start a new private conversation, run /dm-me in a channel.",
                    neutral=True,
                ),
                unfurl_links=False,
            )
            reply = "Ready in your DMs."
        else:
            refusal = (
                "That option isn't recognized.\n\n"
                "Run `/dm-me` in a channel to continue privately. "
                "Admins: `/dm-me enable` or `/dm-me disable`."
            )
        fields: dict[str, Any] = (
            notice_fields(refusal) if refusal else notice_fields(reply or "", neutral=True)
        )
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id, user=user_id, **fields
        )
    except _ERRORS as exc:
        log.warning("slack.dm.command_failed", error_type=type(exc).__name__)
        if client is not None:
            with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id,
                    user=user_id,
                    **notice_fields(
                        _error_message(exc, settings=runtime.settings, team_id=team_id)
                    ),
                )


#: Who has been told in this process that a private message has nothing to continue.
_COLD_DM_NOTICES = ColdDmNotices()


async def _explain_no_private_chat(
    client: AsyncWebClient, *, team_id: str, user_id: str, channel_id: str, dms_on: bool
) -> None:
    """Answer a DM that has no conversation to continue, once per person per process."""
    person = f"{team_id}:{user_id}"
    if not _COLD_DM_NOTICES.first_time(person):
        return
    try:
        await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id, **notice_fields(cold_dm_text("slack", dms_on=dms_on))
        )
    except (SlackApiError, aiohttp.ClientError, TimeoutError) as exc:
        _COLD_DM_NOTICES.forget(person)
        log.warning("slack.dm.cold_notice_failed", error_type=type(exc).__name__)


async def _react(client: AsyncWebClient, channel_id: str, event: dict[str, Any], add: bool) -> None:
    """Put the mark that says a message is held on, or take it off."""
    stamp = str(event.get("ts") or "")
    with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
        if add:
            await client.reactions_add(name=_SEEN_REACTION, channel=channel_id, timestamp=stamp)  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs are untyped
        else:
            await client.reactions_remove(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs are untyped
                name=_SEEN_REACTION, channel=channel_id, timestamp=stamp
            )


async def handle_direct_message(
    runtime: SlackRuntime, event: dict[str, Any], *, team_id: str
) -> None:
    """Answer a direct message, or the burst of them it opens, in one turn."""
    if not is_direct_message_event(event) or is_slack_connect_external(event, team_id=team_id):
        return
    channel_id = str(event.get("channel") or "")
    user_id = str(event.get("user") or "")
    message_id = str(event.get("ts") or "")
    if not channel_id or not message_id:
        return
    key = (team_id, channel_id, user_id)
    bursts = runtime.dm_bursts
    # No await before this: a message sent meanwhile joins the burst instead of racing it.
    if (joined := bursts.join(key, event)) is not None:
        # The turn waiting or running here answers this message too.
        with contextlib.suppress(*_ERRORS):
            if (client := await resolve_web_client(runtime, team_id=team_id)) is not None:
                await joined.mark(event, partial(_react, client, channel_id))
        return
    burst = bursts.open(key, event)
    thread_ts = event.get("thread_ts")
    thread_kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    client: AsyncWebClient | None = None
    try:
        client = await resolve_web_client(runtime, team_id=team_id)
        if client is not None:
            await _answer_burst(runtime, client, burst, key, thread=thread_kwargs)
    except _ERRORS as exc:
        await bursts.end(key, burst)  # its notice speaks for the whole burst
        await _tell_failure(
            runtime, client, exc, team_id=team_id, channel_id=channel_id, thread=thread_kwargs
        )
    finally:
        # An error no turn handled, or a cancellation: nothing left goes unheard.
        if left := await bursts.end(key, burst):
            log.warning("slack.dm.not_sent", messages=len(left), client=client is not None)
            if client is not None:
                with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
                    await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                        channel=channel_id, **notice_fields(_NOT_SENT), **thread_kwargs
                    )


async def _answer_burst(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    burst: DmBurst[dict[str, Any]],
    key: tuple[str, str, str],
    *,
    thread: dict[str, Any],
) -> None:
    """Answer every message of the burst, one turn per group, or tell it why not.

    A DM with no conversation is routed first: one agent starts at once; when several
    may answer, the burst waits for the person's pick.
    """
    team_id, channel_id, user_id = key
    route_key = f"{team_id}:{channel_id}"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session:
        conversation = await get_conversation(
            session, platform="slack", route_key=route_key, external_user_id=user_id
        )
        if conversation is not None:
            tenant_id = conversation.tenant_id
        dms_on = await dm_enabled(session, tenant_id=tenant_id)
    if not dms_on:
        await runtime.dm_bursts.end(key, burst)  # one notice speaks for the whole burst
        await _explain_no_private_chat(
            client, team_id=team_id, user_id=user_id, channel_id=channel_id, dms_on=dms_on
        )
        return
    role = await _live_role(client, user_id=user_id, team_id=team_id)
    seconds = await debounce_seconds(runtime.sessionmaker, runtime.settings, tenant_id, "slack")
    react = partial(_react, client, channel_id)
    if conversation is None:
        decision, groups = await _decide_cold(
            runtime, client, team_id=team_id, user_id=user_id, role=role
        )
        route = decision.route
        if isinstance(route, DmChoose):
            choices, held = route.agents, list[dict[str, Any]]()

            async def hold(group: list[dict[str, Any]]) -> None:
                offer = not held
                held.extend(group)  # the pick answers every message held so far
                if offer:
                    await _offer_agents(client, key, choices, held)

            await burst.answer(seconds, react, hold)
            return
        if not isinstance(route, DmStart):
            await runtime.dm_bursts.end(key, burst)  # one refusal speaks for the whole burst
            await _refuse_cold(
                client, route, team_id=team_id, user_id=user_id, channel_id=channel_id
            )
            return
        conversation = await _start_cold(
            runtime,
            client,
            team_id=team_id,
            user_id=user_id,
            channel_id=channel_id,
            role=role,
            agent=route.agent,
            groups=groups,
        )
        if conversation is None:
            return  # another start won: the burst is asked to send again
    else:
        groups = await lookup_dm_groups(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            user_id=user_id,
            groups=user_group_lookup(runtime, client, team_id=team_id),
        )
    answer = _answerer(runtime, client, conversation, key, role=role, groups=groups, thread=thread)
    await burst.answer(seconds, react, answer)


def _answerer(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    conversation: DirectMessageRow,
    key: tuple[str, str, str],
    *,
    role: Role,
    groups: frozenset[str] | None,
    thread: dict[str, Any],
) -> Callable[[list[dict[str, Any]]], Awaitable[None]]:
    """One turn per group of messages in `conversation`; its failure is told, not raised."""
    team_id, channel_id, user_id = key
    identity: AgentIdentity | None = None

    async def on_agent(name: str) -> None:
        nonlocal identity
        if not identity_enabled_for(runtime.settings, "slack", team_id):
            return
        try:
            agent = await find_agent_by_daimon_tag(
                runtime.anthropic,
                tenant_id=conversation.tenant_id,
                name=name,
                records=runtime.sessionmaker,
            )
            async with runtime.sessionmaker.begin() as session:
                identity = await resolve_agent_identity(
                    session,
                    tenant_id=conversation.tenant_id,
                    agent_name=name,
                    is_builtin=is_builtin_agent(
                        name=name,
                        metadata=agent.metadata if agent is not None else None,
                        default_agent_name=runtime.deployment_default.agent_name,
                    ),
                    public_base_url=runtime.settings.mcp.app_root_url,
                    enabled=identity_enabled_for(runtime.settings, "slack", team_id),
                    background_sessionmaker=runtime.sessionmaker,
                    wait_for_face=True,
                )
        except (anthropic.APIError, SQLAlchemyError) as exc:
            log.warning("slack.dm.identity_lookup_failed", error_type=type(exc).__name__)

    async def answer(group: list[dict[str, Any]]) -> None:
        content = "\n\n".join(t for e in group if (t := str(e.get("text") or "")).strip())
        if not content:
            return
        try:
            said = await reply_to_dm(
                runtime.turn_deps,
                platform="slack",
                route_key=f"{team_id}:{channel_id}",
                external_user_id=user_id,
                message_id=str(group[0].get("ts") or ""),
                merged_message_ids=tuple(str(e.get("ts") or "") for e in group[1:]),
                platform_channel_id=channel_id,
                expected_scope_id=conversation.scope_id,
                text=content,
                role=role,
                on_agent=on_agent,
                groups=groups,
            )
            if said is not None:
                await _post_answer(client, identity, said, channel_id=channel_id, thread=thread)
        except _ERRORS as exc:
            await _tell_failure(
                runtime, client, exc, team_id=team_id, channel_id=channel_id, thread=thread
            )

    return answer


async def _post_answer(
    client: AsyncWebClient,
    identity: AgentIdentity | None,
    answer: str,
    *,
    channel_id: str,
    thread: dict[str, Any],
) -> None:
    for offset in range(0, len(answer), 3500):
        await post_as_agent(
            client,
            identity,
            channel=channel_id,
            text=answer[offset : offset + 3500],
            parse="none",
            unfurl_links=False,
            **thread,
        )


async def _tell_failure(
    runtime: SlackRuntime,
    client: AsyncWebClient | None,
    exc: Exception,
    *,
    team_id: str,
    channel_id: str,
    thread: dict[str, Any],
) -> None:
    """Log a failed DM turn and, when a client is at hand, tell the person why."""
    log.warning("slack.dm.turn_failed", error_type=type(exc).__name__)
    if client is None:
        return
    with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
        await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            **notice_fields(_error_message(exc, settings=runtime.settings, team_id=team_id)),
            **thread,
        )


#: The select a person picks their DM agent with when several may answer.
DM_PICK_ACTION_ID = "dm_pick_agent"
_PICK_SECONDS = 300.0
_UNCHECKED = "I couldn't check your access just now.\n\nTry again in a minute."
_PICK_EXPIRED = "This choice expired.\n\nSend a new message to choose again."


@dataclass(frozen=True)
class _PendingPick:
    agents: tuple[str, ...]
    events: list[dict[str, Any]]
    expires_at: float


#: Each person's open agent choice and the messages it answers, by (team, channel, user).
_PENDING_PICKS: dict[tuple[str, str, str], _PendingPick] = {}


async def _decide_cold(
    runtime: SlackRuntime, client: AsyncWebClient, *, team_id: str, user_id: str, role: Role
) -> tuple[DmDecision, frozenset[str] | None]:
    """Which agent a DM with no conversation reaches, with the person's groups looked up afresh."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    groups = await lookup_dm_groups(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        groups=user_group_lookup(runtime, client, team_id=team_id, fresh=True),
    )
    recipients = runtime.settings.direct_message_policies.get(tenant_id, DirectMessagePolicy())
    if not recipients.allows(user_id):
        return DmDecision(DmRefused("no_agent")), groups
    async with runtime.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
        agents = await list_dm_agents(
            session,
            tenant_id=tenant_id,
            anthropic=runtime.turn_deps.anthropic,
            records=runtime.turn_deps.sessionmaker,
        )
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        decision = await decide_dm(
            session,
            runtime.settings,
            policy,
            tenant_id=tenant_id,
            platform="slack",
            agents=agents,
            subject=build_subject(is_admin=role is Role.ADMIN, platform_user_id=user_id),
            account_id=None if principal is None else principal.account_id,
            groups=groups,
        )
    return decision, groups


async def _start_cold(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    role: Role,
    agent: str,
    groups: frozenset[str] | None,
    remember_pick: bool = False,
) -> DirectMessageRow | None:
    """Start a private conversation on `agent` in this DM; None if another message won."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    admission = await admit_cold_dm(
        runtime.turn_deps,
        tenant_id=tenant_id,
        external_user_id=user_id,
        channel_id=channel_id,
        role=role,
        role_ids=(),
        verified_membership=None,
        agent_name=agent,
        platform="slack",
    )
    return await start_cold_dm(
        runtime.turn_deps,
        admission,
        tenant_id=tenant_id,
        workspace_id=team_id,
        route_key=f"{team_id}:{channel_id}",
        external_user_id=user_id,
        remember_pick=remember_pick,
        platform="slack",
        groups=groups,
        channel_id=channel_id,
    )


async def _refuse_cold(
    client: AsyncWebClient,
    route: Route,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
) -> None:
    """Say why a DM with no conversation gets no agent: unknown access, a lock, or nothing."""
    if isinstance(route, DmRefused) and route.reason == "no_agent":
        await _explain_no_private_chat(
            client, team_id=team_id, user_id=user_id, channel_id=channel_id, dms_on=True
        )
        return
    text = _UNCHECKED if isinstance(route, DmUnchecked) else DM_AGENT_REFUSED
    await client.chat_postMessage(channel=channel_id, **notice_fields(text))  # pyright: ignore[reportUnknownMemberType]


async def _offer_agents(
    client: AsyncWebClient,
    key: tuple[str, str, str],
    agents: tuple[str, ...],
    events: list[dict[str, Any]],
) -> None:
    """Ask which agent answers; the messages wait for the pick."""
    now = monotonic()
    for stale in [k for k, pick in _PENDING_PICKS.items() if pick.expires_at <= now]:
        del _PENDING_PICKS[stale]
    _PENDING_PICKS[key] = _PendingPick(agents, events, now + _PICK_SECONDS)
    text = "Choose who to talk to for this private conversation."
    options = [
        {"text": {"type": "plain_text", "text": name[:75]}, "value": name} for name in agents[:100]
    ]
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
            "accessory": {
                "type": "static_select",
                "action_id": DM_PICK_ACTION_ID,
                "placeholder": {"type": "plain_text", "text": "Choose an agent"},
                "options": options,
            },
        }
    ]
    await client.chat_postMessage(channel=key[1], text=text, blocks=blocks)  # pyright: ignore[reportUnknownMemberType]


async def handle_dm_agent_pick(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Start the conversation on the agent a person picked, then answer their waiting messages."""
    team = cast(dict[str, Any], payload.get("team") or {})
    user = cast(dict[str, Any], payload.get("user") or {})
    channel = cast(dict[str, Any], payload.get("channel") or {})
    team_id = str(team.get("id") or user.get("team_id") or "")
    user_id = str(user.get("id") or "")
    channel_id = str(channel.get("id") or "")
    actions = cast(list[dict[str, Any]], payload.get("actions") or [{}])
    agent = str(cast(dict[str, Any], actions[0].get("selected_option") or {}).get("value") or "")
    message_ts = str(cast(dict[str, Any], payload.get("container") or {}).get("message_ts") or "")
    if not team_id or not user_id or not channel_id:
        return
    key = (team_id, channel_id, user_id)
    pending = _PENDING_PICKS.pop(key, None)
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    try:
        if message_ts:
            with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
                await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id,
                    ts=message_ts,
                    **notice_fields(f"You chose {agent}.", neutral=True),
                )
        if pending is None or pending.expires_at <= monotonic() or agent not in pending.agents:
            await client.chat_postMessage(channel=channel_id, **notice_fields(_PICK_EXPIRED))  # pyright: ignore[reportUnknownMemberType]
            return
        role = await _live_role(client, user_id=user_id, team_id=team_id)
        decision, groups = await _decide_cold(
            runtime, client, team_id=team_id, user_id=user_id, role=role
        )
        route = decision.route
        if not (isinstance(route, DmStart) and route.agent == agent) and not (
            decision.may_switch_to(agent)
        ):
            refusal = _UNCHECKED if isinstance(route, DmUnchecked) else DM_AGENT_REFUSED
            await client.chat_postMessage(channel=channel_id, **notice_fields(refusal))  # pyright: ignore[reportUnknownMemberType]
            return
        conversation = await _start_cold(
            runtime,
            client,
            team_id=team_id,
            user_id=user_id,
            channel_id=channel_id,
            role=role,
            agent=agent,
            groups=groups,
            remember_pick=True,
        )
        if conversation is None:
            return
        answer = _answerer(runtime, client, conversation, key, role=role, groups=groups, thread={})
        for group in split_approvals(pending.events, lambda e: str(e.get("text") or "")):
            await answer(group)
    except _ERRORS as exc:
        log.warning("slack.dm.pick_failed", error_type=type(exc).__name__)
        with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                **notice_fields(_error_message(exc, settings=runtime.settings, team_id=team_id)),
            )

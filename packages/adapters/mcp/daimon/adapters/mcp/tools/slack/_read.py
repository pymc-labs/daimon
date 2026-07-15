"""Slack channel tool implementations: list/read/thread/get.

Hybrid read path: prefer the caller's own xoxp token (``slack_read_client``);
1:1 DM (``is_im``) sources on that path go through the leak gate
(``_gate_user_source``) instead of the bot-token visibility scan. Channel
content (public or private) and group DMs (``is_mpim``) are ungated on the
user-token path — the caller's own xoxp token is the reach authority, so they
may be answered wherever they asked. ``not_in_channel`` on the
user token (e.g. a public channel the user never joined) silently retries on
the bot token to preserve shipped behavior. The bot-token path is unchanged:
``conversations.info`` → ``check_channel_access`` → fetch, with a JIT connect
hint appended to ``MISSING_ACCESS`` denials for callers who have no user token
yet.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    build_connect_hint,
    slack_read_client,
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._leak_policy import (
    DM_REDIRECT_MSG,
    get_destination,
    is_dm_destination,
)
from daimon.adapters.mcp.tools.slack._models import (
    SlackChannelRow,
    SlackMessageRow,
    SlackThreadResult,
)
from daimon.adapters.mcp.tools.slack._visibility import (
    MISSING_ACCESS,
    _is_guest,  # pyright: ignore[reportPrivateUsage]
    check_channel_access,
    is_channel_visible,
    map_slack_api_error,
)
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

_HISTORY_LIMIT_CAP = 100


def _reraise_mapped(err: SlackApiError, *, as_user: bool = False) -> ToolError:
    mapped = map_slack_api_error(err, as_user=as_user)
    if mapped is not None:
        return mapped
    raise err


def _slack_error_code(err: SlackApiError) -> str:
    return str(err.response.get("error", ""))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like


def _is_im(channel: dict[str, Any]) -> bool:
    """True when the source is a 1:1 direct message."""
    return bool(channel.get("is_im"))


async def _gate_user_source(
    runtime: McpRuntime, auth: AuthIdentity, *, channel: dict[str, Any]
) -> None:
    """1:1 DM content may only be produced in a DM with daimon.

    Channel content (public or private) and group DMs (mpim) are ungated: the
    caller's own xoxp token already enforces that they can see them, so they
    may be answered wherever they asked (Discord parity).
    """
    if not _is_im(channel):
        return
    destination = await get_destination(runtime, auth, now=datetime.now(tz=UTC))
    if not is_dm_destination(destination):
        raise ToolError(DM_REDIRECT_MSG)


def _with_connect_hint(
    runtime: McpRuntime, err: ToolError, *, team_id: str, slack_user_id: str
) -> ToolError:
    """Append the JIT connect link to bot-path MISSING_ACCESS denials."""
    if MISSING_ACCESS not in str(err):
        return err
    hint = build_connect_hint(
        runtime, team_id=team_id, slack_user_id=slack_user_id, now=time.time()
    )
    if hint is None:
        return err
    return ToolError(f"{err}{hint}")


async def _channel_info(client: AsyncWebClient, *, channel_id: str) -> dict[str, Any]:
    resp = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    return cast(dict[str, Any], resp["channel"])


async def _resolve_usernames(client: AsyncWebClient, user_ids: set[str]) -> dict[str, str]:
    """Display-name lookup with one users.info call per distinct author."""
    names: dict[str, str] = {}
    for user_id in user_ids:
        resp = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        user = cast(dict[str, Any], resp["user"])
        profile = cast(dict[str, Any], user.get("profile") or {})
        names[user_id] = str(
            profile.get("display_name") or user.get("real_name") or user.get("name") or user_id
        )
    return names


def _to_message_rows(
    raw_messages: list[dict[str, Any]], usernames: dict[str, str]
) -> list[SlackMessageRow]:
    rows: list[SlackMessageRow] = []
    for msg in raw_messages:
        user_id = msg.get("user")
        rows.append(
            SlackMessageRow(
                ts=str(msg["ts"]),
                user_id=str(user_id) if user_id else None,
                username=usernames.get(str(user_id)) if user_id else None,
                text=str(msg.get("text", "")),
                thread_ts=str(msg["thread_ts"]) if msg.get("thread_ts") else None,
                reply_count=int(msg["reply_count"]) if msg.get("reply_count") else None,
            )
        )
    return rows


def _author_ids(raw_messages: list[dict[str, Any]]) -> set[str]:
    return {str(m["user"]) for m in raw_messages if m.get("user")}


async def _all_conversations(
    client: AsyncWebClient, *, user: str | None, types: str
) -> list[dict[str, Any]]:
    """Paginate users.conversations; ``user=None`` yields the bot's own channels."""
    channels: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "types": types,
            "exclude_archived": True,
            "limit": 200,
            "cursor": cursor,
        }
        if user is not None:
            kwargs["user"] = user
        resp = await client.users_conversations(**kwargs)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channels.extend(cast(list[dict[str, Any]], resp["channels"]))
        metadata = cast(dict[str, Any], resp.get("response_metadata") or {})
        cursor = str(metadata.get("next_cursor") or "") or None
        if cursor is None:
            return channels


async def _slack_list_channels_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime, auth: AuthIdentity
) -> list[SlackChannelRow]:
    user_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    rc = await slack_read_client(runtime, team_id=team_id, slack_user_id=user_id)

    if rc.runs_as_user:
        destination = await get_destination(runtime, auth, now=datetime.now(tz=UTC))
        try:
            channels = await _all_conversations(
                rc.client, user=user_id, types="public_channel,private_channel,im,mpim"
            )
        except SlackApiError as err:
            raise _reraise_mapped(err, as_user=True) from err
        rows: list[SlackChannelRow] = []
        for ch in channels:
            # 1:1 DM entries (their names/existence) obey the same rule as
            # their contents: only surfaced when the destination is a DM.
            if _is_im(ch) and not is_dm_destination(destination):
                continue
            topic = cast(dict[str, Any], ch.get("topic") or {})
            rows.append(
                SlackChannelRow(
                    id=str(ch["id"]),
                    name=str(ch.get("name", "")),
                    is_private=bool(ch.get("is_private")),
                    topic=str(topic["value"]) if topic.get("value") else None,
                    num_members=int(ch["num_members"]) if ch.get("num_members") else None,
                )
            )
        return rows

    # Bot path: unchanged shipped behavior (bot's channels ∩ caller visibility).
    client = rc.client
    try:
        bot_channels = await _all_conversations(
            client, user=None, types="public_channel,private_channel"
        )
        guest = await _is_guest(client, user_id=user_id)
        member_types = "public_channel,private_channel" if guest else "private_channel"
        user_channel_ids = {
            str(ch["id"])
            for ch in await _all_conversations(client, user=user_id, types=member_types)
        }
    except SlackApiError as err:
        raise _reraise_mapped(err) from err
    rows = []
    for ch in bot_channels:
        if not is_channel_visible(
            is_im_or_mpim=bool(ch.get("is_im") or ch.get("is_mpim")),
            is_private=bool(ch.get("is_private")),
            is_guest=guest,
            is_member=str(ch["id"]) in user_channel_ids,
        ):
            continue
        topic = cast(dict[str, Any], ch.get("topic") or {})
        rows.append(
            SlackChannelRow(
                id=str(ch["id"]),
                name=str(ch.get("name", "")),
                is_private=bool(ch.get("is_private")),
                topic=str(topic["value"]) if topic.get("value") else None,
                num_members=int(ch["num_members"]) if ch.get("num_members") else None,
            )
        )
    return rows


async def _fetch_channel_history(
    client: AsyncWebClient, *, channel_id: str, limit: int
) -> list[SlackMessageRow]:
    resp = await client.conversations_history(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel=channel_id, limit=min(limit, _HISTORY_LIMIT_CAP)
    )
    raw = cast(list[dict[str, Any]], resp["messages"])
    raw.reverse()  # Slack returns newest-first; tools return oldest-first
    usernames = await _resolve_usernames(client, _author_ids(raw))
    return _to_message_rows(raw, usernames)


async def _slack_read_channel_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime, auth: AuthIdentity, *, channel_id: str, limit: int
) -> list[SlackMessageRow]:
    user_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    rc = await slack_read_client(runtime, team_id=team_id, slack_user_id=user_id)

    if rc.runs_as_user:
        try:
            channel = await _channel_info(rc.client, channel_id=channel_id)
            await _gate_user_source(runtime, auth, channel=channel)
            return await _fetch_channel_history(rc.client, channel_id=channel_id, limit=limit)
        except SlackApiError as err:
            # Any not_in_channel from the user token (typically a public channel
            # the user never joined) falls back to the bot path, which
            # independently re-checks access via check_channel_access — so this
            # can never widen the audience.
            if _slack_error_code(err) != "not_in_channel":
                raise _reraise_mapped(err, as_user=True) from err

    bot_client = (
        rc.client if not rc.runs_as_user else await slack_web_client(runtime, team_id=team_id)
    )
    try:
        channel = await _channel_info(bot_client, channel_id=channel_id)
        await check_channel_access(bot_client, channel=channel, user_id=user_id)
        return await _fetch_channel_history(bot_client, channel_id=channel_id, limit=limit)
    except ToolError as terr:
        if rc.runs_as_user:
            raise  # user already has a token — a connect hint would be noise
        raise _with_connect_hint(runtime, terr, team_id=team_id, slack_user_id=user_id) from terr
    except SlackApiError as err:
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        if rc.runs_as_user:
            raise mapped from err
        raise _with_connect_hint(runtime, mapped, team_id=team_id, slack_user_id=user_id) from err


def _split_thread_id(thread_id: str) -> tuple[str, str]:
    channel_id, sep, thread_ts = thread_id.partition(":")
    if not sep or not channel_id or not thread_ts:
        raise ToolError(
            "slack thread ids have the form channel_id:thread_ts "
            "(e.g. C0123456789:1717171717.123456)"
        )
    return channel_id, thread_ts


async def _fetch_thread_replies(
    client: AsyncWebClient, *, channel_id: str, thread_ts: str, limit: int
) -> SlackThreadResult:
    resp = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel=channel_id, ts=thread_ts, limit=min(limit, _HISTORY_LIMIT_CAP)
    )
    raw = cast(list[dict[str, Any]], resp["messages"])  # already oldest-first
    has_more = bool(resp.get("has_more"))
    usernames = await _resolve_usernames(client, _author_ids(raw))
    return SlackThreadResult(
        channel_id=channel_id,
        thread_ts=thread_ts,
        messages=_to_message_rows(raw, usernames),
        has_more=has_more,
    )


async def _slack_read_thread_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime, auth: AuthIdentity, *, thread_id: str, limit: int
) -> SlackThreadResult:
    channel_id, thread_ts = _split_thread_id(thread_id)
    user_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    rc = await slack_read_client(runtime, team_id=team_id, slack_user_id=user_id)

    if rc.runs_as_user:
        try:
            channel = await _channel_info(rc.client, channel_id=channel_id)
            await _gate_user_source(runtime, auth, channel=channel)
            return await _fetch_thread_replies(
                rc.client, channel_id=channel_id, thread_ts=thread_ts, limit=limit
            )
        except SlackApiError as err:
            # Any not_in_channel from the user token (typically a public channel
            # the user never joined) falls back to the bot path, which
            # independently re-checks access via check_channel_access — so this
            # can never widen the audience.
            if _slack_error_code(err) != "not_in_channel":
                raise _reraise_mapped(err, as_user=True) from err

    bot_client = (
        rc.client if not rc.runs_as_user else await slack_web_client(runtime, team_id=team_id)
    )
    try:
        channel = await _channel_info(bot_client, channel_id=channel_id)
        await check_channel_access(bot_client, channel=channel, user_id=user_id)
        return await _fetch_thread_replies(
            bot_client, channel_id=channel_id, thread_ts=thread_ts, limit=limit
        )
    except ToolError as terr:
        if rc.runs_as_user:
            raise
        raise _with_connect_hint(runtime, terr, team_id=team_id, slack_user_id=user_id) from terr
    except SlackApiError as err:
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        if rc.runs_as_user:
            raise mapped from err
        raise _with_connect_hint(runtime, mapped, team_id=team_id, slack_user_id=user_id) from err


async def _fetch_single_message(
    client: AsyncWebClient, *, channel_id: str, message_id: str
) -> SlackMessageRow:
    resp = await client.conversations_history(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel=channel_id, latest=message_id, inclusive=True, limit=1
    )
    raw = cast(list[dict[str, Any]], resp["messages"])
    match = next((m for m in raw if str(m["ts"]) == message_id), None)
    if match is None:
        # Thread replies are absent from channel history — fall back to replies.
        # A bogus message_id has no thread to reply into, so Slack answers
        # thread_not_found/message_not_found here — map that locally to the
        # same "message not found" ToolError a plain not-found match gets,
        # rather than letting it fall through to the generic channel-level
        # mapping (or an opaque raw SlackApiError).
        try:
            resp = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel_id, ts=message_id, limit=_HISTORY_LIMIT_CAP
            )
        except SlackApiError as err:
            error_code = _slack_error_code(err)
            if error_code in ("thread_not_found", "message_not_found"):
                raise ToolError("message not found") from err
            raise
        raw = cast(list[dict[str, Any]], resp["messages"])
        match = next((m for m in raw if str(m["ts"]) == message_id), None)
    if match is None:
        raise ToolError("message not found")
    usernames = await _resolve_usernames(client, _author_ids([match]))
    return _to_message_rows([match], usernames)[0]


async def _slack_get_message_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime, auth: AuthIdentity, *, channel_id: str, message_id: str
) -> SlackMessageRow:
    user_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    rc = await slack_read_client(runtime, team_id=team_id, slack_user_id=user_id)

    if rc.runs_as_user:
        try:
            channel = await _channel_info(rc.client, channel_id=channel_id)
            await _gate_user_source(runtime, auth, channel=channel)
            return await _fetch_single_message(
                rc.client, channel_id=channel_id, message_id=message_id
            )
        except SlackApiError as err:
            # Any not_in_channel from the user token (typically a public channel
            # the user never joined) falls back to the bot path, which
            # independently re-checks access via check_channel_access — so this
            # can never widen the audience.
            if _slack_error_code(err) != "not_in_channel":
                raise _reraise_mapped(err, as_user=True) from err

    bot_client = (
        rc.client if not rc.runs_as_user else await slack_web_client(runtime, team_id=team_id)
    )
    try:
        channel = await _channel_info(bot_client, channel_id=channel_id)
        await check_channel_access(bot_client, channel=channel, user_id=user_id)
        return await _fetch_single_message(bot_client, channel_id=channel_id, message_id=message_id)
    except ToolError as terr:
        if rc.runs_as_user:
            raise
        raise _with_connect_hint(runtime, terr, team_id=team_id, slack_user_id=user_id) from terr
    except SlackApiError as err:
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        if rc.runs_as_user:
            raise mapped from err
        raise _with_connect_hint(runtime, mapped, team_id=team_id, slack_user_id=user_id) from err

"""Slack /memory handler — the channel agent's shared memory, and your own.

`/memory` and `/memory <path>` show the shared memory read-only; in a channel shared
with another organisation, that channel's own memory instead.
`/memory mine`, `/memory mine <path>` and `/memory forget-mine [code]` act on
the invoking person's own personal memory only (`daimon.core.memory_view`).

Ack-first contract: called from app.on_request AFTER the Socket Mode ack.
Ephemeral-only (no modal, no trigger_id), mirroring help.py. The slash
payload's `text` is the optional memory path argument.

Catches DaimonError | anthropic.APIError | SlackApiError at the listener
boundary (S3).
"""

from __future__ import annotations

import contextlib
from typing import Any, cast

import aiohttp
import anthropic
import structlog
from daimon.adapters.slack.blockkit import notice_fields
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.memory_view import (
    PersonalMemoryReply,
    get_channel_memory_store,
    get_memory_content,
    get_own_personal_memory_store,
    list_memory_paths,
    parse_memory_args,
    personal_memory_reply,
    resolve_channel_agent,
)
from daimon.core.mux_backend import resource_scope
from daimon.core.turn_hold import RequesterOrg
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()

_SLACK_LIMIT = 3800  # headroom under Slack's ~4000-char text limit
_EMPTY = "No memory to show here."


async def _requester_org(client: AsyncWebClient, *, team_id: str, user_id: str) -> RequesterOrg:
    """HOST when `users.info` says the person belongs to this workspace, OTHER when to another.

    A failed or unreadable lookup is UNKNOWN, which reads as held: nothing is
    recorded about the person from here.
    """
    try:
        info = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        user_team: object = cast("dict[str, Any]", info.get("user") or {}).get("team_id")
    except (SlackApiError, aiohttp.ClientError, TimeoutError, AttributeError):
        return RequesterOrg.UNKNOWN
    if not isinstance(user_team, str) or not user_team:
        return RequesterOrg.UNKNOWN
    return RequesterOrg.HOST if user_team == team_id else RequesterOrg.OTHER


async def _channel_shared(client: AsyncWebClient, *, channel_id: str) -> bool | None:
    """Slack's own word on whether the channel is shared with another organisation.

    None when the lookup fails; the recorded registry still applies then.
    """
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        shared: object = cast("dict[str, Any]", info.get("channel") or {}).get("is_ext_shared")
    except (SlackApiError, aiohttp.ClientError, TimeoutError, AttributeError):
        return None
    return shared if isinstance(shared, bool) else None


def _truncate(text: str) -> str:
    if len(text) <= _SLACK_LIMIT:
        return text
    return text[: _SLACK_LIMIT - 20] + "\n… (truncated)"


def _fenced(header: str, content: str, limit: int) -> str:
    """Wrap content in a closed code fence, truncating content to fit limit.

    Truncating the CONTENT before wrapping (rather than truncating the fully
    wrapped string) guarantees the closing ``` fence is always present — a
    naive `_truncate(header + fence + content + fence)` can slice mid-fence
    and leave an unclosed code block that corrupts rendering. Backtick runs
    inside the content get a zero-width space so an embedded ``` can't close
    the wrapping fence early.
    """
    content = content.replace("```", "`​``")
    overhead = len(header) + len("\n```\n\n```")
    budget = limit - overhead
    if len(content) > budget:
        content = content[: budget - 16] + "\n… (truncated)"
    return f"{header}\n```\n{content}\n```"


def _personal_text(reply: PersonalMemoryReply) -> tuple[str, bool]:
    """The reply's text, and whether it is memory content (else a message from Daimon)."""
    if reply.content is not None:
        return _fenced(f"*`{reply.title}`*", reply.content, _SLACK_LIMIT), True
    parts = [f"*{reply.title}*"]
    if reply.paths:
        parts.append("\n".join(f"• `{p}`" for p in reply.paths))
    if reply.hint:
        parts.append(f"_{reply.hint}_")
    return _truncate("\n\n".join(parts)), bool(reply.paths)


async def _post(
    client: AsyncWebClient, *, channel_id: str, user_id: str, text: str, content: bool
) -> None:
    """Memory content goes out as plain text; Daimon's own words as a notice card."""
    fields: dict[str, Any] = {"text": text} if content else notice_fields(text, neutral=True)
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel=channel_id, user=user_id, **fields
    )


async def _personal_memory_text(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    args: str,
) -> tuple[str, bool]:
    """The invoker's own personal memory: never anyone else's, whatever the args.

    Returns the text and whether it is memory content rather than a message.
    """
    request = parse_memory_args(args)
    agent = await resolve_channel_agent(
        runtime.sessionmaker,
        runtime.anthropic,
        tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
        platform="slack",
        user_id=user_id,
        channel_id=channel_id,
        default=runtime.deployment_default,
        requester_org=await _requester_org(client, team_id=team_id, user_id=user_id),
        channel_shared=await _channel_shared(client, channel_id=channel_id),
    )
    if agent is None:
        return _EMPTY, False
    store_id = await get_own_personal_memory_store(runtime.sessionmaker, agent)
    reply = await personal_memory_reply(
        runtime.anthropic,
        agent_name=agent.agent_name,
        store_id=store_id,
        request=request,
        command="/memory",
        scope=resource_scope(tenant_id=str(agent.tenant_id)),
        held=agent.hold is not None,
    )
    return _personal_text(reply)


async def handle_memory_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Ephemeral /memory handler.

    Ack-first contract: called from app.on_request AFTER the Socket Mode ack.
    Resolves the per-event web client, then posts a chat.postEphemeral listing
    memory paths (no argument) or showing one memory's content (path argument).

    Catches DaimonError | anthropic.APIError | SlackApiError at the listener
    boundary (S3).
    """
    team_id: str = str(payload.get("team_id") or "")
    user_id: str = str(payload.get("user_id") or "")
    channel_id: str = str(payload.get("channel_id") or "")
    path_arg: str = str(payload.get("text") or "").strip()

    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        log.warning("slack.memory_command.no_token", team_id=team_id)
        return

    try:
        if parse_memory_args(path_arg).kind not in ("shared_list", "shared_show"):
            personal, personal_is_content = await _personal_memory_text(
                runtime,
                client,
                team_id=team_id,
                user_id=user_id,
                channel_id=channel_id,
                args=path_arg,
            )
            await _post(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text=personal,
                content=personal_is_content,
            )
            return
        resolved = await get_channel_memory_store(
            runtime.sessionmaker,
            runtime.anthropic,
            tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
            platform="slack",
            user_id=user_id,
            channel_id=channel_id,
            default=runtime.deployment_default,
            requester_org=await _requester_org(client, team_id=team_id, user_id=user_id),
            channel_shared=await _channel_shared(client, channel_id=channel_id),
        )
        is_content = True
        if resolved is None:
            text, is_content = _EMPTY, False
        elif not path_arg:
            agent_name, store_id = resolved
            paths = await list_memory_paths(runtime.anthropic, store_id)
            is_content = bool(paths)
            text = (
                _EMPTY
                if not paths
                else _truncate(
                    f"*{agent_name}'s memory* ({len(paths)} files)\n"
                    + "\n".join(f"• `{p}`" for p in paths)
                )
            )
        else:
            _agent_name, store_id = resolved
            content = await get_memory_content(runtime.anthropic, store_id, path_arg)
            if content is None:
                text = f"No memory file called `{path_arg}`.\n\nRun `/memory` to see them all."
                is_content = False
            else:
                text = _fenced(f"*`{path_arg}`*", content, _SLACK_LIMIT)

        await _post(client, channel_id=channel_id, user_id=user_id, text=text, content=is_content)
    except (DaimonError, anthropic.APIError, SlackApiError) as exc:
        log.warning("slack.memory_command.failed", team_id=team_id, exc_info=exc)
        error_text = (
            str(exc)
            if isinstance(exc, DaimonError)
            else "Couldn't load the memory.\n\nTry again in a minute."
        )
        with contextlib.suppress(SlackApiError):
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                **notice_fields(error_text),
            )

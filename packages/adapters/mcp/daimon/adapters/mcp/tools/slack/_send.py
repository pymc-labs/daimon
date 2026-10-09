"""Slack send_message implementation: post + composite thread targets.

Also owns thread-root creation (``_slack_create_thread_impl``): posting a
root message and returning its ``ts`` as the thread anchor.

Bot-token only. Unlike the read tools, send never uses the caller's own
xoxp token — there is no impersonation path and no hybrid client to fall
back to; every post is authenticated as the workspace bot.

Content is posted raw, with no entity escaping: send content is
deliberately authored (by the agent or a routine), so `<@U…>` mentions,
`<#C…>` channel links, and bold markers are meant to render, and escaping
them would break that intent.

A thread target is validated with `conversations.replies` before anything
is posted. Slack accepts a `thread_ts` that does not exist, silently drops
it, and posts the message at the channel root instead of erroring — so the
post itself can never surface a bad thread target; only a pre-flight check
can. The composite `channel_id:thread_ts` form is how a caller aims a reply
at a specific thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import anthropic
import httpx
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.slack_file_proxy import fetch_slack_file
from daimon.adapters.mcp.tools._channel_policy import (
    OPEN_READ_POLICY,
    ChannelReadPolicy,
    require_channel_writable,
)
from daimon.adapters.mcp.tools._file_handles import staged_uploads
from daimon.adapters.mcp.tools._tidy import PostRecord, record_agent_posts
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._files import require_file_source, resolve_file_link
from daimon.adapters.mcp.tools.slack._leak_policy import is_dm_destination
from daimon.adapters.mcp.tools.slack._models import SlackMessageRow
from daimon.adapters.mcp.tools.slack._visibility import (
    check_channel_access,
    map_slack_api_error,
)
from daimon.core.agent_identity import (
    identity_enabled_for,
    is_builtin_agent,
    resolve_agent_identity,
)
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.output_delivery import MAX_BYTES_PER_FILE
from daimon.core.slack_customize_scope import (
    _NO_CUSTOMIZE_SCOPE as _NO_CUSTOMIZE_SCOPE,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.slack_customize_scope import (
    missing_customize_scope,
    remember_missing_customize_scope,
)
from daimon.core.slack_file_token import SlackFileRef
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

# Slack's {"type": "markdown"} block cap is 12,000 CHARACTERS (not bytes);
# over it, chat.postMessage answers msg_too_long. Kept local to this module —
# it is platform-specific and must not leak into core.
_MAX_CONTENT_CHARS = 12_000

# Duplicated from the Slack adapter's bounded notification-text fallback
# (daimon.adapters.slack.lifecycle) — adapters must not import each other.
_NOTIFICATION_TEXT_MAX = 3000

_OVER_LENGTH_MSG = (
    f"content is over Slack's {_MAX_CONTENT_CHARS:,}-character message limit — "
    "shorten it, or send it in parts with several send_message calls "
    "(thread replies work well for parts)"
)
_NOT_IN_CHANNEL_MSG = "daimon isn't in that channel — ask a member to /invite @daimon"
_ATTACHMENT_NOT_A_FILE_LINK_MSG = (
    "on Slack an attachment url must be a file link from read_thread, read_channel, "
    "get_message or search_messages (…/slack/file/<token>) — to post a file you made "
    "yourself, use create_file_upload_url and file_handles"
)
_ATTACHMENT_LINK_REJECTED_MSG = (
    "that file link has expired or belongs to another workspace — read the message "
    "again to get a fresh one"
)
_ATTACHMENT_LINKS_UNCONFIGURED_MSG = (
    "this deployment cannot resolve file links — use create_file_upload_url and "
    "file_handles instead"
)
_FILES_NEED_CONTENT_MSG = "Slack files need a short caption — content cannot be empty"
_MISSING_FILES_SCOPE_MSG = (
    "this workspace's daimon install has no files:write scope — a workspace "
    "admin must reinstall daimon from the install link before files can be posted"
)
_UPLOAD_FAILED_SUFFIX = " — the message text was already posted, do not send it again"
_MAX_FILES = 10
_THREAD_NOT_FOUND_MSG = "that thread does not exist — check the thread_ts and try again"
log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _FileUpload:
    filename: str
    content: bytes


def _split_send_target(channel_id: str) -> tuple[str, str | None]:
    """Split a `channel_id` or `channel_id:thread_ts` composite target."""
    target, sep, thread_ts = channel_id.partition(":")
    if not sep:
        return channel_id, None
    if not target or not thread_ts:
        raise ToolError(
            "slack send targets have the form channel_id or channel_id:thread_ts "
            "(e.g. C0123456789:1717171717.123456)"
        )
    return target, thread_ts


def _notification_text(content: str) -> str:
    """Bound content for use as the `text` notification fallback."""
    if len(content) <= _NOTIFICATION_TEXT_MAX:
        return content
    return content[: _NOTIFICATION_TEXT_MAX - 1] + "…"


def _slack_error_code(err: SlackApiError) -> str:
    return str(err.response.get("error", ""))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like


async def _validate_channel_access(
    client: AsyncWebClient, *, channel_id: str, requester_id: str
) -> None:
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
        channel = cast(dict[str, Any], info["channel"])
        await check_channel_access(client, channel=channel, user_id=requester_id, allow_own_im=True)
    except SlackApiError as err:
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        raise mapped from err


async def _validate_thread_target(
    client: AsyncWebClient, *, channel_id: str, thread_ts: str
) -> None:
    try:
        await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id, ts=thread_ts, limit=1
        )
    except SlackApiError as err:
        code = _slack_error_code(err)
        if code in ("thread_not_found", "message_not_found"):
            raise ToolError(_THREAD_NOT_FOUND_MSG) from err
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        raise mapped from err


async def _post_message(
    client: AsyncWebClient,
    *,
    channel_id: str,
    content: str,
    thread_ts: str | None,
    identity_kwargs: dict[str, str] | None = None,
) -> dict[str, Any]:
    post_kwargs: dict[str, Any] = {
        "channel": channel_id,
        "text": _notification_text(content),
        "blocks": [{"type": "markdown", "text": content}],
    }
    if thread_ts is not None:
        post_kwargs["thread_ts"] = thread_ts
    try:
        resp = await _post_with_identity(client, identity_kwargs, **post_kwargs)
    except SlackApiError as err:
        code = _slack_error_code(err)
        if code == "not_in_channel":
            raise ToolError(_NOT_IN_CHANNEL_MSG) from err
        if code == "msg_too_long":
            raise ToolError(_OVER_LENGTH_MSG) from err
        mapped = map_slack_api_error(err)
        if mapped is None:
            raise
        raise mapped from err
    return cast(dict[str, Any], resp.data)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


async def _post_with_identity(
    client: AsyncWebClient,
    identity_kwargs: dict[str, str] | None,
    **post_kwargs: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Apply an agent header, retrying only a missing customize scope."""
    token = getattr(client, "token", None) if identity_kwargs else None
    custom = identity_kwargs if not missing_customize_scope(token) else None
    try:
        return await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
            **(post_kwargs | (custom or {}))
        )
    except SlackApiError as err:
        needed = str(err.response.get("needed", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if (
            not custom
            or _slack_error_code(err) != "missing_scope"
            or "chat:write.customize" not in {scope.strip() for scope in needed.split(",")}
        ):
            raise
        remember_missing_customize_scope(token)
        return await client.chat_postMessage(**post_kwargs)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]


async def _agent_identity_kwargs(runtime: McpRuntime, auth: AuthIdentity) -> dict[str, str] | None:
    if not identity_enabled_for(runtime.settings, "slack", auth.external_id):
        return None
    agent_id = auth.chat_agent_id or auth.agent_id
    if agent_id is None:
        return None
    try:
        agent = await find_agent_by_derived_uuid(
            runtime.client, tenant_id=auth.tenant_id, agent_id=agent_id
        )
        if agent is None:
            return None
        async with runtime.session_factory.begin() as session:
            identity = await resolve_agent_identity(
                session,
                tenant_id=auth.tenant_id,
                agent_name=agent.name,
                is_builtin=is_builtin_agent(
                    name=agent.name,
                    metadata=agent.metadata,
                    default_agent_name=runtime.deployment_default.agent_name,
                ),
                public_base_url=runtime.settings.mcp.app_root_url,
                enabled=identity_enabled_for(runtime.settings, "slack", auth.external_id),
                background_sessionmaker=runtime.session_factory,
            )
    except (anthropic.APIError, SQLAlchemyError) as exc:
        log.warning("slack.agent_identity_lookup_failed", error_type=type(exc).__name__)
        return None
    if identity.builtin:
        return None
    result = {"username": identity.name}
    if identity.avatar_url is not None:
        result["icon_url"] = identity.avatar_url
    return result


def _resolve_attachment_ref(runtime: McpRuntime, *, url: str, team_id: str) -> SlackFileRef:
    ref = resolve_file_link(runtime, url, team_id=team_id)
    if ref == "unconfigured":
        raise ToolError(_ATTACHMENT_LINKS_UNCONFIGURED_MSG)
    if ref == "not_a_link":
        raise ToolError(_ATTACHMENT_NOT_A_FILE_LINK_MSG)
    if ref == "rejected":
        raise ToolError(_ATTACHMENT_LINK_REJECTED_MSG)
    return ref


async def _fetch_attachments(
    http_client: httpx.AsyncClient,
    *,
    bot_token: str,
    specs: list[dict[str, str]],
    refs: list[SlackFileRef],
) -> list[_FileUpload]:
    uploads: list[_FileUpload] = []
    for spec, ref in zip(specs, refs, strict=True):
        try:
            content, _, fetched_name = await fetch_slack_file(
                http_client,
                bot_token=bot_token,
                file_id=ref.file_id,
                max_bytes=MAX_BYTES_PER_FILE,
            )
        except httpx.HTTPError as exc:
            raise ToolError(
                f"failed to fetch attachment {ref.file_id!r} "
                f"(maximum {MAX_BYTES_PER_FILE // (1024 * 1024)} MiB): {exc}"
            ) from exc
        if len(content) > MAX_BYTES_PER_FILE:
            raise ToolError(f"attachment exceeds {MAX_BYTES_PER_FILE // (1024 * 1024)} MiB")
        uploads.append(_FileUpload(filename=spec.get("filename") or fetched_name, content=content))
    return uploads


async def _upload_files(
    client: AsyncWebClient,
    *,
    channel_id: str,
    thread_ts: str,
    uploads: list[_FileUpload],
) -> list[dict[str, Any]]:
    """Upload into ``thread_ts`` and return Slack's file objects."""
    try:
        resp = await client.files_upload_v2(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id,
            thread_ts=thread_ts,
            file_uploads=[
                {"content": upload.content, "filename": upload.filename, "title": upload.filename}
                for upload in uploads
            ],
        )
    except Exception as err:
        # Whatever failed, the caption is already posted, so every failure says so.
        # Cancellation is a BaseException and still reaches the turn driver.
        message = f"file upload failed ({type(err).__name__})"
        if isinstance(err, SlackApiError):
            try:
                code = _slack_error_code(err)
                if code == "missing_scope":
                    message = _MISSING_FILES_SCOPE_MSG
                elif code == "not_in_channel":
                    message = _NOT_IN_CHANNEL_MSG
                else:
                    mapped = map_slack_api_error(err)
                    message = str(mapped) if mapped is not None else f"file upload failed ({code})"
            except (AttributeError, TypeError, ValueError):
                # SDK responses can wrap a raw HTTP response or non-object JSON.
                message = "file upload failed (invalid Slack response)"
        raise ToolError(message + _UPLOAD_FAILED_SUFFIX) from err
    return cast(list[dict[str, Any]], resp.get("files") or [])


def _upload_message_ts(
    files: list[dict[str, Any]], *, channel_id: str, thread_ts: str
) -> list[str]:
    """The ts of each message carrying the uploaded files in ``thread_ts``.

    Slack shares an upload into the channel asynchronously, so a file whose
    share has not landed when the upload call returns has no ts here, and its
    message is not recorded for tidying.
    """
    found: list[str] = []
    for f in files:
        shares = cast(dict[str, dict[str, list[dict[str, Any]]]], f.get("shares") or {})
        for by_channel in (shares.get("public") or {}, shares.get("private") or {}):
            for entry in by_channel.get(channel_id) or []:
                ts = str(entry.get("ts") or "")
                if ts and str(entry.get("thread_ts") or "") == thread_ts and ts not in found:
                    found.append(ts)
    return found


async def _slack_send_message_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    content: str,
    attachments: list[dict[str, str]] | None,
    file_handles: list[str] | None,
    http_client: httpx.AsyncClient | None = None,
    read_policy: ChannelReadPolicy = OPEN_READ_POLICY,
) -> SlackMessageRow:
    if len(content) > _MAX_CONTENT_CHARS:
        raise ToolError(_OVER_LENGTH_MSG)
    if len(attachments or []) + len(file_handles or []) > _MAX_FILES:
        raise ToolError(f"max {_MAX_FILES} attachments per message")
    if (attachments or file_handles) and not content.strip():
        raise ToolError(_FILES_NEED_CONTENT_MSG)

    target_channel_id, thread_ts = _split_send_target(channel_id)
    requester_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    refs = [
        _resolve_attachment_ref(runtime, url=spec.get("url", ""), team_id=team_id)
        for spec in (attachments or [])
    ]
    uploads: list[_FileUpload] = []
    if file_handles:
        staged = await staged_uploads(
            file_handles, session_factory=runtime.session_factory, tenant_id=auth.tenant_id
        )
        uploads = [_FileUpload(filename=row.display_filename, content=data) for row, data in staged]
        if any(len(upload.content) > MAX_BYTES_PER_FILE for upload in uploads):
            raise ToolError(f"attachment exceeds {MAX_BYTES_PER_FILE // (1024 * 1024)} MiB")
    client = await slack_web_client(runtime, team_id=team_id)

    await _validate_channel_access(client, channel_id=target_channel_id, requester_id=requester_id)
    await require_channel_writable(runtime, auth, channel_id=target_channel_id)
    if thread_ts is not None:
        await _validate_thread_target(client, channel_id=target_channel_id, thread_ts=thread_ts)

    for ref in refs:
        await require_file_source(
            client,
            file_id=ref.file_id,
            requester_id=requester_id,
            read_policy=read_policy,
            dm_ok=is_dm_destination(target_channel_id),
            in_place=(target_channel_id, thread_ts),
        )
    if refs:
        if http_client is not None:
            fetched = await _fetch_attachments(
                http_client, bot_token=str(client.token), specs=attachments or [], refs=refs
            )
        else:
            async with httpx.AsyncClient(timeout=30.0) as owned_client:
                fetched = await _fetch_attachments(
                    owned_client, bot_token=str(client.token), specs=attachments or [], refs=refs
                )
        uploads = fetched + uploads

    resp = await _post_message(
        client,
        channel_id=target_channel_id,
        content=content,
        thread_ts=thread_ts,
        identity_kwargs=await _agent_identity_kwargs(runtime, auth),
    )
    await record_agent_posts(
        runtime,
        auth,
        platform="slack",
        posts=[
            PostRecord(
                channel_id=target_channel_id,
                message_id=str(resp["ts"]),
                thread_ts=thread_ts,
                content=content,
            )
        ],
    )
    message = cast(dict[str, Any], resp.get("message") or {})
    if uploads:
        upload_thread_ts = thread_ts or str(resp["ts"])
        uploaded = await _upload_files(
            client, channel_id=target_channel_id, thread_ts=upload_thread_ts, uploads=uploads
        )
        await record_agent_posts(
            runtime,
            auth,
            platform="slack",
            posts=[
                PostRecord(channel_id=target_channel_id, message_id=ts, thread_ts=upload_thread_ts)
                for ts in _upload_message_ts(
                    uploaded, channel_id=target_channel_id, thread_ts=upload_thread_ts
                )
            ],
        )
    response_thread_ts = message.get("thread_ts")
    response_user_id = message.get("user")
    return SlackMessageRow(
        ts=str(resp["ts"]),
        user_id=str(response_user_id) if response_user_id else None,
        text=content,
        thread_ts=str(response_thread_ts) if response_thread_ts else None,
    )


async def _slack_create_thread_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    content: str,
) -> SlackMessageRow:
    """Post a root message and return its ``ts`` as the thread anchor.

    No ``name`` parameter — Slack threads have no title. A composite
    ``channel_id:thread_ts`` target is refused: a thread root goes to a
    channel, not into an existing thread (use send_message for a reply).
    """
    if len(content) > _MAX_CONTENT_CHARS:
        raise ToolError(_OVER_LENGTH_MSG)

    target_channel_id, thread_ts = _split_send_target(channel_id)
    if thread_ts is not None:
        raise ToolError(
            "a thread root is posted to a channel, not into an existing thread — "
            "use send_message with the composite channel_id:thread_ts form to reply "
            "into an existing thread"
        )
    requester_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    client = await slack_web_client(runtime, team_id=team_id)

    await _validate_channel_access(client, channel_id=target_channel_id, requester_id=requester_id)
    await require_channel_writable(runtime, auth, channel_id=target_channel_id)

    resp = await _post_message(
        client,
        channel_id=target_channel_id,
        content=content,
        thread_ts=None,
        identity_kwargs=await _agent_identity_kwargs(runtime, auth),
    )
    await record_agent_posts(
        runtime,
        auth,
        platform="slack",
        posts=[
            PostRecord(channel_id=target_channel_id, message_id=str(resp["ts"]), content=content)
        ],
    )
    message = cast(dict[str, Any], resp.get("message") or {})
    response_thread_ts = message.get("thread_ts")
    response_user_id = message.get("user")
    return SlackMessageRow(
        ts=str(resp["ts"]),
        user_id=str(response_user_id) if response_user_id else None,
        text=content,
        thread_ts=str(response_thread_ts) if response_thread_ts else None,
    )

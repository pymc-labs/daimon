"""Opt-in Slack IM conversations and /dm workspace selection."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any, cast

import aiohttp
import anthropic
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.agent_post import post_as_agent
from daimon.adapters.slack.channel_admin_groups import user_group_ids
from daimon.adapters.slack.gating import is_slack_connect_external
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime, admission_refusal_message
from daimon.core.agent_identity import (
    AgentIdentity,
    identity_enabled_for,
    is_builtin_agent,
    resolve_agent_identity,
)
from daimon.core.config import Settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.direct_messages import (
    is_sealed_slack_message,
    reply_to_dm,
    require_dm_enabled,
    require_unsealed_source,
    sealed_channel_ids,
    start_dm,
)
from daimon.core.errors import DaimonError
from daimon.core.handoff_context import TranscriptTurn
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.stores.direct_messages import dm_enabled, get_conversation, set_dm_enabled
from daimon.core.stores.domain import Role
from daimon.core.turn.admission import admit
from daimon.core.turn.errors import AdmissionDenied
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger(__name__)
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
    "A workspace admin must reinstall or reauthorize daimon to grant im:history and im:write. "
    "Update the app manifest to subscribe to message.im and enable the Messages tab."
)


def _error_message(exc: Exception, fallback: str, *, settings: Settings) -> str:
    if isinstance(exc, SlackApiError) and exc.response.get("error") in {  # pyright: ignore[reportUnknownMemberType]  # SlackApiError.response is untyped
        "missing_scope",
        "token_revoked",
        "token_expired",
        "invalid_auth",
        "not_authed",
        "account_inactive",
    }:
        return _REAUTHORIZE
    if isinstance(exc, AdmissionDenied):
        return admission_refusal_message(exc.reason, settings, in_dm=True)
    return str(exc) if isinstance(exc, DaimonError) else fallback


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
            raise DaimonError(_REAUTHORIZE)
    user = cast(dict[str, Any], response.get("user", {}))
    if (
        user.get("team_id") != team_id
        or user.get("deleted")
        or user.get("is_bot")
        or user.get("is_stranger")
    ):
        raise DaimonError("Only current members of this workspace can use its DM conversations.")
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
        if action in {"enable", "disable"}:
            if role is not Role.ADMIN:
                raise DaimonError("Only a workspace admin can change the DM policy.")
            async with runtime.sessionmaker.begin() as session:
                await set_dm_enabled(session, tenant_id=tenant_id, enabled=action == "enable")
            reply = (
                "DM conversations enabled." if action == "enable" else "DM conversations disabled."
            )
        elif action in {"", "move"}:
            await require_dm_enabled(runtime.turn_deps, tenant_id=tenant_id)
            if channel_id.startswith("D"):
                raise DaimonError("Run /dm in the workspace channel you want to continue from.")
            admission = await admit(
                runtime.turn_deps,
                tenant_id=tenant_id,
                platform="slack",
                external_user_id=user_id,
                channel_id=channel_id,
                role=role,
                platform_role_ids=()
                if role is Role.ADMIN
                else sorted(
                    await user_group_ids(runtime, client, tenant_id=tenant_id, user_id=user_id)
                ),
                is_dm=True,
                dm_source_channel_id=channel_id,
                now=datetime.now(UTC),
            )
            require_unsealed_source(admission)
            response = await client.conversations_history(channel=channel_id, limit=12)  # pyright: ignore[reportUnknownMemberType]
            messages = cast(list[dict[str, Any]], response.get("messages", []))
            # /dm carries no thread_ts, so admission cannot see a thread sealed
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
            )
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=dm_channel,
                text=(
                    f"Continuing from <{source_url}|this channel>. Send your next message here. "
                    "Run /dm in a channel again to start a new private conversation."
                ),
                unfurl_links=False,
            )
            reply = "Ready in your DMs."
        else:
            reply = "Use /dm to continue privately, or /dm enable|disable for the workspace policy."
        await client.chat_postEphemeral(channel=channel_id, user=user_id, text=reply)  # pyright: ignore[reportUnknownMemberType]
    except _ERRORS as exc:
        log.warning("slack.dm.command_failed", error_type=type(exc).__name__)
        if client is not None:
            with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id,
                    user=user_id,
                    text=_error_message(
                        exc,
                        "Couldn't open that private conversation. Please retry.",
                        settings=runtime.settings,
                    ),
                )


async def handle_direct_message(
    runtime: SlackRuntime, event: dict[str, Any], *, team_id: str
) -> None:
    if not is_direct_message_event(event) or is_slack_connect_external(event, team_id=team_id):
        return
    channel_id = str(event.get("channel") or "")
    user_id = str(event.get("user") or "")
    message_id = str(event.get("ts") or "")
    if not channel_id or not message_id:
        return
    route_key = f"{team_id}:{channel_id}"
    client: AsyncWebClient | None = None
    try:
        async with runtime.sessionmaker() as session:
            conversation = await get_conversation(
                session, platform="slack", route_key=route_key, external_user_id=user_id
            )
            if conversation is None or not await dm_enabled(
                session, tenant_id=conversation.tenant_id
            ):
                return
        client = await resolve_web_client(runtime, team_id=team_id)
        if client is None:
            return
        role = await _live_role(client, user_id=user_id, team_id=team_id)
        content = str(event.get("text") or "")
        if not content.strip():
            return
        identity: AgentIdentity | None = None

        async def on_agent(name: str) -> None:
            nonlocal identity
            if not identity_enabled_for(runtime.settings, "slack", team_id):
                return
            try:
                agent = await find_agent_by_daimon_tag(
                    runtime.anthropic, tenant_id=conversation.tenant_id, name=name
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

        answer = await reply_to_dm(
            runtime.turn_deps,
            platform="slack",
            route_key=route_key,
            external_user_id=user_id,
            message_id=message_id,
            expected_scope_id=conversation.scope_id,
            text=content,
            role=role,
            on_agent=on_agent,
        )
        if answer is not None:
            for offset in range(0, len(answer), 3500):
                await post_as_agent(
                    client,
                    identity,
                    channel=channel_id,
                    text=answer[offset : offset + 3500],
                    parse="none",
                    unfurl_links=False,
                )
    except _ERRORS as exc:
        log.warning("slack.dm.turn_failed", error_type=type(exc).__name__)
        if client is not None:
            with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
                await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel_id,
                    text=_error_message(
                        exc,
                        "Couldn't complete this private conversation. Please retry.",
                        settings=runtime.settings,
                    ),
                )

"""Typed provider cards; opaque local tokens never contain native payloads."""

from __future__ import annotations

import asyncio
import html
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

import aiohttp
import structlog
from daimon.adapters.slack.agent_post import post_as_agent
from daimon.core.agent_identity import AgentIdentity
from daimon.core.posted_controls.lifecycle import edit_card_within
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.provider_actions import (
    BrowserAuthentication,
    BrowserOriginAccess,
    NativeActionResponse,
    ProviderActionHook,
    ProviderActionPrompt,
)
from mux.contracts.ids import ResourceRef
from mux.profiles import emits_legacy_events_for
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

ACTION_PREFIX = "dpa:"
_ACTION = re.compile(r"^dpa:([A-Za-z0-9_-]{8,64}):([0-2])$")
_SEND_ERRORS = (SlackApiError, aiohttp.ClientError, asyncio.TimeoutError)
log = structlog.get_logger(__name__)


def _words(
    prompt: ProviderActionPrompt,
) -> tuple[str, tuple[tuple[str, NativeActionResponse], ...]]:
    match prompt.surface:
        case BrowserOriginAccess() as surface:
            return f"Allow access to {surface.origin}?", (
                ("Approve", surface.approve),
                ("Deny", surface.deny),
                ("Cancel", surface.cancel),
            )
        case BrowserAuthentication() as surface:
            choices = (("Sign-in complete", surface.submit),) if surface.submit is not None else ()
            return "Sign-in required", (*choices, ("Cancel", surface.cancel))


def action_blocks(
    prompt: ProviderActionPrompt, *, token: str | None, status: str | None = None
) -> list[dict[str, Any]]:
    title, choices = _words(prompt)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "plain_text", "text": status or title}},
    ]
    if status is not None:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": title}})
    if token is not None:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": label},
                        "action_id": f"{ACTION_PREFIX}{token}:{index}",
                        "value": token,
                    }
                    for index, (label, _) in enumerate(choices)
                ],
            }
        )
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "plain_text", "text": "Only the requester may answer."}],
            }
        )
    return blocks


@dataclass
class PendingAction:
    prompt: ProviderActionPrompt
    answer: asyncio.Future[NativeActionResponse | None]
    client: AsyncWebClient
    channel: str
    thread_ts: str
    ts: str | None = None
    choice: str | None = None


class SlackProviderActions:
    def __init__(self) -> None:
        self.pending: dict[str, PendingAction] = {}

    def hook(
        self,
        client: AsyncWebClient,
        *,
        channel: str,
        thread_ts: str,
        identity: AgentIdentity | None = None,
        record_post: Callable[[str], Awaitable[None]] | None = None,
    ) -> ProviderActionHook:
        async def request(prompt: ProviderActionPrompt) -> NativeActionResponse | None:
            if (
                prompt.context.requester.platform != "slack"
                or prompt.context.requester.thread_id != thread_ts
                or datetime.now(UTC) >= prompt.context.expires_at
            ):
                return None
            answer: asyncio.Future[NativeActionResponse | None] = (
                asyncio.get_running_loop().create_future()
            )
            token = secrets.token_urlsafe(12)
            posted = PendingAction(prompt, answer, client, channel, thread_ts)
            self.pending[token] = posted
            status = "Expired"
            title, _ = _words(prompt)
            try:
                response = await post_as_agent(
                    client,
                    identity,
                    channel=channel,
                    thread_ts=thread_ts,
                    text=html.escape(title, quote=False),
                    blocks=action_blocks(prompt, token=token),
                    mrkdwn=False,
                    parse="none",
                )
                posted.ts = str(response.get("ts") or "")
                if not posted.ts:
                    return None
                if record_post is not None:
                    await record_post(posted.ts)
                remaining = max(
                    0.0, (prompt.context.expires_at - datetime.now(UTC)).total_seconds()
                )
                async with asyncio.timeout(remaining):
                    selected = await asyncio.shield(answer)
                if datetime.now(UTC) >= prompt.context.expires_at:
                    return None
                if selected is not None:
                    status = {
                        "Approve": "Approved",
                        "Deny": "Denied",
                        "Cancel": "Cancelled",
                        "Sign-in complete": "Completed",
                    }[posted.choice or "Cancel"]
                return selected
            except TimeoutError:
                if posted.ts is None:
                    log.warning("slack.provider_action.post_failed")
                return None
            except _SEND_ERRORS:
                log.warning("slack.provider_action.post_failed")
                return None
            except asyncio.CancelledError:
                status = "Expired" if datetime.now(UTC) >= prompt.context.expires_at else "Stopped"
                raise
            finally:
                self.pending.pop(token, None)
                if not answer.done():
                    answer.cancel()
                if posted.ts:
                    await edit_card_within(
                        client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs are Unknown
                            channel=channel,
                            ts=posted.ts,
                            text=html.escape(status, quote=False),
                            blocks=action_blocks(prompt, token=None, status=status),
                            mrkdwn=False,
                            parse="none",
                        ),
                        card_key=("slack", channel, posted.ts),
                        budget_s=2.0,
                        failure_errors=_SEND_ERRORS,
                        failed_event="slack.provider_action.retire_failed",
                    )

        return request

    async def handle_click(self, payload: dict[str, Any]) -> None:
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, list) or not raw_actions:
            return
        action = _object(cast(list[object], raw_actions)[0])
        match = _ACTION.fullmatch(str(action.get("action_id") or ""))
        if match is None or (posted := self.pending.get(match[1])) is None:
            return
        user = str(_object(payload.get("user")).get("id") or "")
        channel = str(_object(payload.get("channel")).get("id") or "")
        thread = str(_object(payload.get("message")).get("thread_ts") or "")
        _, choices = _words(posted.prompt)
        index = int(match[2])
        refusal = None
        if (
            user != posted.prompt.context.requester.platform_user_id
            or channel != posted.channel
            or thread != posted.thread_ts
        ):
            refusal = "Only the requester can answer this request."
        elif posted.answer.done() or index >= len(choices):
            refusal = "This request is no longer pending."
        elif datetime.now(UTC) >= posted.prompt.context.expires_at:
            posted.answer.set_result(None)
            refusal = "This request expired."
        if refusal is not None:
            try:
                await posted.client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs are Unknown
                    channel=posted.channel,
                    user=user,
                    text=refusal,
                    mrkdwn=False,
                    parse="none",
                )
            except _SEND_ERRORS:
                log.warning("slack.provider_action.refusal_failed")
            return
        posted.choice, response = choices[index]
        posted.answer.set_result(response)


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


class ProviderActionOptions(TypedDict, total=False):
    provider_action: ProviderActionHook


def provider_action_options(
    prepared: PreparedTurn, factory: Callable[[], ProviderActionHook]
) -> ProviderActionOptions:
    ref = getattr(prepared, "session_ref", None)
    if isinstance(ref, ResourceRef) and not emits_legacy_events_for(ref.provider):
        return {"provider_action": factory()}
    return {}

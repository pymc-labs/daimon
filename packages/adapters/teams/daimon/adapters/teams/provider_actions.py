"""Turn-local typed provider cards and requester-only Teams action routing.

A card posted in a channel thread also sends the requester an activity feed
notice (`activity_feed`).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict

import structlog
from daimon.adapters.teams.activity_feed import ActivityFeed
from daimon.adapters.teams.card_actions import button, submitted_fields, toast
from daimon.adapters.teams.identity import canonical_uuid
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.posted_controls.confirmation import EXPIRED_MESSAGE
from daimon.core.posted_controls.lifecycle import edit_card_within
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.provider_actions import (
    BrowserAuthentication,
    BrowserOriginAccess,
    NativeActionResponse,
    ProviderActionHook,
    ProviderActionPrompt,
)
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import ActionSet, AdaptiveCard, CardElement, RichTextBlock, TextRun
from mux.contracts.ids import ResourceRef
from mux.profiles import emits_legacy_events_for

VERB = "provider_action"
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


def provider_action_card(
    prompt: ProviderActionPrompt, *, token: str | None, status: str | None = None
) -> AdaptiveCard:
    title, choices = _words(prompt)
    body: list[CardElement] = [
        RichTextBlock(inlines=[TextRun(text=status or title, weight="Bolder")])
    ]
    if status is not None:
        body.append(RichTextBlock(inlines=[TextRun(text=title)]))
    if token is not None:
        body.append(
            ActionSet(
                actions=[
                    button(VERB, label, str(index), token=token)
                    for index, (label, _) in enumerate(choices)
                ]
            )
        )
        body.append(RichTextBlock(inlines=[TextRun(text="Only the requester may answer.")]))
    return AdaptiveCard(body=body, fallback_text=status or title)


@dataclass
class PendingAction:
    prompt: ProviderActionPrompt
    answer: asyncio.Future[NativeActionResponse | None]
    conversation_id: str
    service_url: str | None
    message_id: str | None = None
    choice: str | None = None


class TeamsProviderActions:
    def __init__(self, sender: TeamsSender, *, feed: ActivityFeed | None = None) -> None:
        self.sender = sender
        self.feed = feed
        self.pending: dict[str, PendingAction] = {}

    def hook(
        self,
        *,
        conversation_id: str,
        service_url: str | None,
        thread_id: str,
        team_group_id: str | None = None,
    ) -> ProviderActionHook:
        """Posts each prompt's card; `team_group_id` is the channel's team, for its notice."""

        async def request(prompt: ProviderActionPrompt) -> NativeActionResponse | None:
            if (
                prompt.context.requester.platform != "teams"
                or prompt.context.requester.thread_id != thread_id
                or datetime.now(UTC) >= prompt.context.expires_at
            ):
                return None
            answer: asyncio.Future[NativeActionResponse | None] = (
                asyncio.get_running_loop().create_future()
            )
            token = secrets.token_urlsafe(12)
            posted = PendingAction(prompt, answer, conversation_id, service_url)
            self.pending[token] = posted
            status = "Expired"
            try:
                activity = MessageActivityInput().add_card(
                    provider_action_card(prompt, token=token)
                )
                sent = await self.sender.send(conversation_id, activity, service_url=service_url)
                posted.message_id = sent.id
                if self.feed is not None:
                    self.feed.notify(
                        conversation_id,
                        sent.id,
                        recipient_id=prompt.context.requester.platform_user_id,
                        text="Waiting for your answer",
                        preview=_words(prompt)[0],
                        group_id=team_group_id,
                    )
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
                if posted.message_id is None:
                    log.warning("teams.provider_action.post_failed")
                return None
            except TEAMS_SEND_ERRORS:
                log.warning("teams.provider_action.post_failed")
                return None
            except asyncio.CancelledError:
                status = "Expired" if datetime.now(UTC) >= prompt.context.expires_at else "Stopped"
                raise
            finally:
                self.pending.pop(token, None)
                if not answer.done():
                    answer.cancel()
                if posted.message_id is not None:
                    edit = MessageActivityInput(id=posted.message_id).add_card(
                        provider_action_card(prompt, token=None, status=status)
                    )
                    await edit_card_within(
                        self.sender.send(conversation_id, edit, service_url=service_url),
                        card_key=("teams", conversation_id, posted.message_id),
                        budget_s=2.0,
                        failure_errors=TEAMS_SEND_ERRORS,
                        failed_event="teams.provider_action.retire_failed",
                    )

        return request

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        activity = ctx.activity
        data = submitted_fields(activity.value.action.data)
        posted = self.pending.get(str(data.get("token") or ""))
        index = str(data.get("op") or "")
        if posted is None or index not in {"0", "1", "2"}:
            return toast("This request is no longer pending.")
        _, choices = _words(posted.prompt)
        if (
            canonical_uuid(activity.from_.aad_object_id)
            != posted.prompt.context.requester.platform_user_id
            or activity.conversation.id != posted.conversation_id
            or (posted.service_url is not None and activity.service_url != posted.service_url)
        ):
            return toast("Only the requester can answer this request.")
        if posted.answer.done() or int(index) >= len(choices):
            return toast("This request is no longer pending.")
        if datetime.now(UTC) >= posted.prompt.context.expires_at:
            posted.answer.set_result(None)
            return toast(EXPIRED_MESSAGE)
        posted.choice, response = choices[int(index)]
        posted.answer.set_result(response)
        return toast("Response recorded.")


class ProviderActionOptions(TypedDict, total=False):
    provider_action: ProviderActionHook


def provider_action_options(
    prepared: PreparedTurn, factory: Callable[[], ProviderActionHook]
) -> ProviderActionOptions:
    ref = getattr(prepared, "session_ref", None)
    if isinstance(ref, ResourceRef) and not emits_legacy_events_for(ref.provider):
        return {"provider_action": factory()}
    return {}

"""Slack's `ConfirmationHook`: post a confirmation card, wait for its button.

The card's words, states and Block Kit come from
`daimon.core.posted_controls.confirmation`; this module posts the card into
the turn's thread, routes the Approve/Deny `block_actions` click back to the
waiting turn through `PendingConfirmations`, and edits the card to the answer.

The registry is in-process, like the cancel registry: the turn waiting on a
card runs in this process, and a restart ends both together.
"""

from __future__ import annotations

import asyncio
import html
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
import structlog
from daimon.adapters.slack.agent_post import post_as_agent
from daimon.adapters.slack.external_access import host_only_notice
from daimon.adapters.slack.gating import is_external_interactive
from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_identity import AgentIdentity
from daimon.core.authz import Subject
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationHook,
    ConfirmationPrompt,
)
from daimon.core.native_admins import load_live_subject
from daimon.core.posted_controls.confirmation import (
    CONFIRMATION_CUSTOM_ID_PREFIX,
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    ConfirmationCardState,
    ConfirmationHow,
    build_confirmation_blocks,
    build_confirmation_card,
    confirmation_card_text,
    parse_always_custom_id,
    parse_confirmation_custom_id,
    parse_details_custom_id,
)
from daimon.core.posted_controls.lifecycle import (
    STILL_WAITING_MESSAGE,
    PostedConfirmations,
    check_confirmation_admin,
    edit_card_within,
    queue_card_edit,
)
from daimon.core.posted_controls.tool_rule_confirmation import (
    ASK_ADMIN_MESSAGE,
    ONLY_ADMIN_MESSAGE,
    SAVE_FAILED_MESSAGE,
    STALE_RULE_MESSAGE,
    allowed_rule_scope,
    save_always_allow,
    snapshot_tool_rule,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.adapters.slack.runtime import SlackRuntime
    from daimon.core.turn.admission import Admission

__all__ = ["CONFIRMATION_CUSTOM_ID_PREFIX", "SlackConfirmationCards"]

log = structlog.get_logger(__name__)
_COLORS: dict[ConfirmationCardState, str] = {
    "pending": "#FEE75C",
    "approved": "#57F287",
    "denied": "#99AAB5",
    "expired": "#99AAB5",
    "stopped": "#99AAB5",
}


def _plain_message_text(value: str) -> str:
    """Keep Slack control characters literal in fallback and private messages."""
    return html.escape(value, quote=False)


def _attachment(card: ConfirmationCard, prompt: ConfirmationPrompt) -> list[dict[str, Any]]:
    blocks = build_confirmation_blocks(card, prompt=prompt)
    if prompt.admin and card.state == "pending":
        for block in blocks:
            if block["type"] == "actions":
                block["elements"][0]["text"]["text"] = "Confirm"
        blocks.insert(
            1,
            {
                "type": "section",
                "text": {"type": "plain_text", "text": "\n".join(card.detail_lines)},
            },
        )
    return [{"color": _COLORS[card.state], "blocks": blocks}]


#: Most time a card edit may take.
EDIT_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class _PostedCard:
    prompt: ConfirmationPrompt
    client: AsyncWebClient
    channel: str
    ts: str
    thread_ts: str | None
    answered_edit_done: asyncio.Event = field(default_factory=asyncio.Event)
    retired: asyncio.Event = field(default_factory=asyncio.Event)
    check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None
    rule_factory: async_sessionmaker[AsyncSession] | None = None
    rule_subject: Callable[[str], Awaitable[Subject]] | None = None


class SlackConfirmationCards:
    """Posted confirmation cards awaiting a click, keyed by token."""

    def __init__(self) -> None:
        self._controls = PostedConfirmations[_PostedCard]()

    def hook(
        self,
        client: AsyncWebClient,
        *,
        channel: str,
        thread_ts: str,
        team_id: str | None = None,
        identity: AgentIdentity | None = None,
        record_post: Callable[[str], Awaitable[None]] | None = None,
        runtime: SlackRuntime | None = None,
        admission: Admission | None = None,
        tenant_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        prepare_admin: Callable[[ConfirmationPrompt], Awaitable[ConfirmationPrompt]] | None = None,
        check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None,
    ) -> ConfirmationHook:
        """A hook that posts each prompt's card into `channel`/`thread_ts`.

        `team_id` is the workspace hosting the app, which an admin re-check requires;
        without it nobody counts as an admin (a card that needs one is never settled).
        """

        if runtime is not None:
            from daimon.core.channel_backend import backend_selection_enabled, make_backend_service

            if backend_selection_enabled(runtime.settings.turn):
                from daimon.adapters.slack.admin import resolve_is_admin
                from daimon.core.confirmation import backend_switch_callbacks
                from mux.contracts.ids import ChannelRef

                if tenant_id is None or account_id is None:
                    raise ValueError("Backend confirmation requires an admitted account and tenant")

                async def current_platform_admin(clicker: str) -> bool:
                    from daimon.adapters.slack.admin import resolve_is_platform_admin

                    return team_id is not None and await resolve_is_platform_admin(
                        client, user_id=clicker, host_team_id=team_id
                    )

                async def current_admin(clicker: str) -> bool:
                    return team_id is not None and await resolve_is_admin(
                        client,
                        user_id=clicker,
                        host_team_id=team_id,
                        action=DelegatedAction.AGENT_SETTINGS,
                    )

                from functools import partial

                from daimon.core.turn.admission import capture_admitted_backend_source

                capture = partial(
                    capture_admitted_backend_source,
                    runtime.turn_deps,
                    admission=admission,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    platform="slack",
                )

                prepare_admin, check_admin = backend_switch_callbacks(
                    make_backend_service(
                        runtime.sessionmaker, runtime.settings.turn, capture_source=capture
                    ),
                    sessionmaker=runtime.sessionmaker,
                    anthropic=runtime.anthropic,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    current_admin=current_admin,
                    current_platform_admin=current_platform_admin,
                    answering_agent_name=(
                        getattr(getattr(admission, "config", None), "agent_name", None)
                    ),
                    default_agent_name=runtime.deployment_default.agent_name,
                    place_word="workspace",
                    # A channel-pin card clears only the channel this turn runs in.
                    pin_channel=ChannelRef(
                        tenant_id=str(tenant_id),
                        platform="slack",
                        channel_id=admission.origin_channel_id,
                    )
                    if admission is not None and admission.origin_channel_id is not None
                    else None,
                )

        rule_factory = runtime.sessionmaker if runtime is not None else None

        async def rule_subject(clicker: str) -> Subject:
            from daimon.adapters.slack.admin_caller import admin_caller

            if runtime is None or tenant_id is None:
                return Subject()
            caller = await admin_caller(client, user_id=clicker)
            async with runtime.sessionmaker() as session:
                return await load_live_subject(
                    session,
                    tenant_id=tenant_id,
                    platform="slack",
                    caller=caller,
                    action=DelegatedAction.TOOL_RULES,
                )

        async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer | ApprovedConfirmation:
            if prompt.admin:
                if prepare_admin is None or check_admin is None:
                    return "denied"
                prompt = await prepare_admin(prompt)
            if prompt.tool_rule is not None and rule_factory is not None:
                prompt = await snapshot_tool_rule(prompt, rule_factory)
            posted_cards: list[_PostedCard] = []

            async def post(token: str) -> _PostedCard:
                card = build_confirmation_card(
                    prompt, state="pending", token=token, allow_always=prompt.tool_rule is not None
                )
                response = await post_as_agent(
                    client,
                    identity,
                    channel=channel,
                    thread_ts=thread_ts,
                    text=_plain_message_text(confirmation_card_text(card)),
                    attachments=_attachment(card, prompt),
                    mrkdwn=False,
                    parse="none",
                )
                ts = str(response.get("ts") or "")  # pyright: ignore[reportUnknownMemberType]
                if record_post is not None and ts:
                    await record_post(ts)
                parent_thread_ts = (
                    thread_ts
                    if thread_ts and thread_ts != ts and not channel.startswith("D")
                    else None
                )
                posted = _PostedCard(
                    prompt=prompt,
                    client=client,
                    channel=channel,
                    ts=ts,
                    thread_ts=parent_thread_ts,
                    check_admin=check_admin,
                    rule_factory=rule_factory,
                    rule_subject=rule_subject,
                )
                posted_cards.append(posted)
                return posted

            async def retire(posted: _PostedCard, state: ConfirmationCardState) -> None:
                posted.retired.set()
                await _edit(posted, state, answered_by=None)

            async def remind() -> None:
                # Once, after a while: the requester is pinged in the card's thread.
                card_ts = posted_cards[0].ts if posted_cards else None
                await post_as_agent(
                    client,
                    identity,
                    channel=channel,
                    thread_ts=thread_ts or card_ts,
                    text=f"<@{prompt.requester_platform_user_id}> {STILL_WAITING_MESSAGE}",
                )

            result = await self._controls.confirm(
                prompt, post=post, retire=retire, post_errors=(SlackApiError,), remind=remind
            )
            if result == "approved" and posted_cards:
                answered_posted = posted_cards[0]

                async def retire_unsent() -> None:
                    # Queued behind the Approved edit on the same card
                    # (`edit_card_within` keeps per-card call order).
                    await answered_posted.answered_edit_done.wait()
                    answered_posted.retired.set()
                    await _edit(answered_posted, "stopped", answered_by=None)

                return ApprovedConfirmation(answer="approved", retire_unsent=retire_unsent)
            return result

        return _confirm

    async def handle_click(self, payload: dict[str, Any]) -> None:
        """Route an Approve/Deny `block_actions` click to its waiting turn."""
        actions: list[dict[str, Any]] = payload.get("actions") or []
        action_id = str(actions[0].get("action_id") or "") if actions else ""
        if details_token := parse_details_custom_id(action_id):
            posted = self._controls.cards.get(details_token)
            if posted is not None:
                details_user: dict[str, Any] = payload.get("user") or {}
                details_clicker = str(details_user.get("id") or "")
                if is_external_interactive(payload) and details_clicker != str(
                    posted.prompt.requester_platform_user_id
                ):
                    # What an agent asks to do is for the people who asked it to.
                    await _ephemeral(
                        posted,
                        details_clicker,
                        NOT_YOURS_MESSAGE.format(
                            requester=f"<@{posted.prompt.requester_platform_user_id}>"
                        ),
                    )
                    return
                await _ephemeral(
                    posted,
                    details_clicker,
                    _plain_message_text("\n".join(posted.prompt.detail_lines))
                    or "No additional details.",
                )
            return
        if always_token := parse_always_custom_id(action_id):
            await self._handle_always(payload, always_token)
            return
        parsed = parse_confirmation_custom_id(action_id)
        if parsed is None:
            return
        token, answer = parsed
        user: dict[str, Any] = payload.get("user") or {}
        clicker = str(user.get("id") or "")
        posted = self._controls.cards.get(token)
        if posted is None:
            await _ephemeral_from_payload(payload, clicker, self._controls.missing_message(token))
            return
        refusal = await check_confirmation_admin(posted.prompt, clicker, posted.check_admin, answer)
        if refusal is not None:
            await _ephemeral(posted, clicker, refusal)
            return
        if refusal := self._controls.claim(token, clicker, answer):
            if refusal == NOT_YOURS_MESSAGE:
                refusal = refusal.format(requester=f"<@{posted.prompt.requester_platform_user_id}>")
            await _ephemeral(posted, clicker, refusal)
            return
        try:
            await _edit(posted, answer, answered_by=clicker)
        finally:
            posted.answered_edit_done.set()

    async def _handle_always(self, payload: dict[str, Any], token: str) -> None:
        user: dict[str, Any] = payload.get("user") or {}
        clicker = str(user.get("id") or "")
        posted = self._controls.cards.get(token)
        if posted is None:
            await _ephemeral_from_payload(payload, clicker, self._controls.missing_message(token))
            return
        if is_external_interactive(payload):
            # "Always allow" writes a standing rule: admin only, and a member of
            # another workspace is never one.
            await _ephemeral(posted, clicker, host_only_notice(None))
            return
        reference = posted.prompt.tool_rule
        if reference is None or posted.rule_factory is None or posted.rule_subject is None:
            await _ephemeral(posted, clicker, self._controls.missing_message(token))
            return
        try:
            subject = await posted.rule_subject(clicker)
            scope = allowed_rule_scope(reference, subject)
        except Exception:
            scope = None
            subject = Subject()
        if scope is None:
            message = ONLY_ADMIN_MESSAGE
            if clicker == posted.prompt.requester_platform_user_id:
                message += "\n" + ASK_ADMIN_MESSAGE
            await _ephemeral(posted, clicker, message)
            return
        if refusal := self._controls.claim_admin(token):
            await _ephemeral(posted, clicker, refusal)
            return
        queue_card_edit(
            _card_update(posted, "approved", answered_by=clicker, how="once"),
            card_key=("slack", posted.channel, posted.ts),
            failure_errors=(SlackApiError,),
            failed_event="slack.tool_confirmation.edit_failed",
        )
        posted.answered_edit_done.set()
        try:
            result = await save_always_allow(
                posted.rule_factory,
                reference=reference,
                scope=scope,
                subject=subject,
                clicker=clicker,
            )
        except Exception:
            result = "failed"
        if result == "saved" and not posted.retired.is_set():
            how: ConfirmationHow = "always" if scope == "agent" else "channel"
            await _edit(posted, "approved", answered_by=clicker, how=how)
        if result != "saved":
            await _ephemeral(
                posted,
                clicker,
                STALE_RULE_MESSAGE if result == "stale" else SAVE_FAILED_MESSAGE,
            )


async def _edit(
    posted: _PostedCard,
    state: ConfirmationCardState,
    *,
    answered_by: str | None,
    how: ConfirmationHow | None = None,
) -> None:
    await edit_card_within(
        _card_update(posted, state, answered_by=answered_by, how=how),
        card_key=("slack", posted.channel, posted.ts),
        budget_s=EDIT_TIMEOUT_S,
        failure_errors=(SlackApiError,),
        failed_event="slack.tool_confirmation.edit_failed",
    )


def _card_update(
    posted: _PostedCard,
    state: ConfirmationCardState,
    *,
    answered_by: str | None,
    how: ConfirmationHow | None = None,
) -> Awaitable[object]:
    card = build_confirmation_card(
        posted.prompt,
        state=state,
        answered_by_platform_user_id=answered_by,
        how=how
        if how is not None
        else "once"
        if answered_by and posted.prompt.tool_rules_enabled
        else None,
    )
    return posted.client.chat_update(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # slack_sdk **kwargs: Unknown
        channel=posted.channel,
        ts=posted.ts,
        text=_plain_message_text(confirmation_card_text(card)),
        attachments=_attachment(card, posted.prompt),
        mrkdwn=False,
        parse="none",
    )


async def _ephemeral(posted: _PostedCard, user: str, text: str) -> None:
    try:
        await posted.client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=posted.channel,
            user=user,
            thread_ts=posted.thread_ts,
            text=text,
            mrkdwn=False,
            parse="none",
        )
    except SlackApiError as err:
        log.warning("slack.tool_confirmation.ephemeral_failed", error=str(err))


async def _ephemeral_from_payload(payload: dict[str, Any], user: str, text: str) -> None:
    # No card in this process to borrow a client from (answered, expired, or
    # posted before a restart); the click's own response_url still answers.
    response_url = str(payload.get("response_url") or "")
    if not response_url:
        return
    try:
        await AsyncWebhookClient(response_url).send(
            text=text, response_type="ephemeral", replace_original=False
        )
    except aiohttp.ClientError as err:
        log.warning("slack.tool_confirmation.ephemeral_failed", error=str(err), user=user)

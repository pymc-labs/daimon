"""Teams' `ConfirmationHook`: post a confirmation card, wait for its button.

Draws core's `posted_controls.confirmation` card as an Adaptive Card and routes
the click to the waiting turn through `PendingConfirmations`. The registry is
in-process, like the cancel registry: a restart ends the card and its turn together.
A card posted in a channel thread also sends the requester, the one person who
may answer it, an activity feed notice (`activity_feed`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.teams.activity_feed import ActivityFeed
from daimon.adapters.teams.card import answer_message
from daimon.adapters.teams.card_actions import (
    button,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.deployment_admins import admin_team_owners, is_deployment_admin
from daimon.adapters.teams.identity import canonical_uuid
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Subject
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationHook,
    ConfirmationPrompt,
)
from daimon.core.native_admins import load_live_subject
from daimon.core.posted_controls.confirmation import (
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    ConfirmationCardState,
    build_confirmation_card,
    confirmation_card_text,
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
from microsoft_teams.api import (
    Account,
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    CardElement,
    Container,
    RichTextBlock,
    TextBlock,
    TextRun,
    ToggleVisibilityAction,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.adapters.teams.runtime import TeamsRuntime
    from daimon.core.turn.admission import Admission

__all__ = ["VERB", "TeamsConfirmationCards", "confirmation_adaptive_card"]

log = structlog.get_logger(__name__)

VERB = "tool_confirm"
_ANSWERS: dict[str, ConfirmationAnswer] = {"approve": "approved", "deny": "denied"}
#: Waits between attempts to send a card edit; the last value is unused.
_CARD_SEND_RETRY_DELAYS_S: tuple[float, ...] = (1.0, 3.0, 0.0)
#: The click's brief acknowledgement; the card itself updates through its queue.
_ANSWER_TOASTS: dict[ConfirmationAnswer, str] = {
    "approved": "Approved",
    "denied": "Denied",
    "expired": "Expired",
}

#: The activity feed line for a card waiting on its requester.
APPROVAL_NOTICE = "Waiting for your approval"

#: Most time a card edit may take.
EDIT_TIMEOUT_S = 2.0

#: Most blocks the Details of one card use; Teams refuses a message over about 28 KB, and
#: every block costs bytes of its own.
MAX_DETAIL_BLOCKS = 12


async def _is_admin_now(
    runtime: TeamsRuntime,
    clicker: str,
    *,
    action: DelegatedAction | None = DelegatedAction.TOOL_RULES,
) -> bool:
    """A live admin check at a click: a revoked team ownership counts at once.

    The cached owners answer first and only a yes is read again, so a click
    by anyone else costs no Graph read and can't spend the shared rate limit.
    """
    settings = runtime.settings.teams
    if settings is None:
        return False
    for fresh in (False, True):
        owners = admin_team_owners(runtime, fresh=fresh)
        if not await is_deployment_admin(
            settings, owners, user_id=clicker, runtime=runtime, action=action
        ):
            return False
    return True


def _footer(
    card: ConfirmationCard, prompt: ConfirmationPrompt, answered_by: str | None
) -> tuple[str, ...]:
    name = prompt.requester_display_name or answered_by or "requester"
    if card.state == "pending":
        # Teams renders TIME() in the reader's own timezone.
        expires = prompt.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (f"Only {name} can approve or deny", f"Expires {{{{TIME({expires})}}}}")
    if card.state in {"approved", "denied"}:
        if prompt.tool_rules_enabled and card.footer is not None:
            return (card.footer.format(actor=answered_by or card.answered_by_platform_user_id),)
        return (f"by {answered_by or name}",)
    return ()


def _detail_blocks(lines: tuple[str, ...]) -> list[CardElement]:
    """Details as at most `MAX_DETAIL_BLOCKS` blocks, so a long body can't bloat the card.

    A short list gets one block a line; a longer one is cut into runs of lines
    that each share a block, with the line breaks kept.
    """
    run = -(-len(lines) // MAX_DETAIL_BLOCKS) or 1
    return [
        RichTextBlock(inlines=[TextRun(text="\n".join(lines[start : start + run]))])
        for start in range(0, len(lines), run)
    ]


def confirmation_adaptive_card(
    card: ConfirmationCard, prompt: ConfirmationPrompt, *, answered_by: str | None = None
) -> AdaptiveCard:
    """`card` as an Adaptive Card; buttons only while pending."""
    # Teams parses Markdown in TextBlock. TextRun keeps tool-provided words literal.
    body: list[CardElement] = [
        RichTextBlock(inlines=[TextRun(text=card.headline, weight="Bolder", size="Medium")])
    ]
    if card.body:
        body.append(RichTextBlock(inlines=[TextRun(text=card.body)]))
    if card.consequence:
        body.append(RichTextBlock(inlines=[TextRun(text=card.consequence)]))
    if prompt.admin:
        body.extend(RichTextBlock(inlines=[TextRun(text=line)]) for line in card.detail_lines)
    if card.token is not None:
        body.append(Container(items=[], separator=True))
        approve = button(
            VERB,
            "Confirm" if prompt.admin else "Approve",
            "approve",
            style="positive",
            token=card.token,
        )
        deny = button(
            VERB,
            "Deny",
            "deny",
            style="destructive",
            token=card.token,
        )
        actions = [approve, deny]
        if card.allow_always:
            actions.append(button(VERB, "Always allow", "always", token=card.token))
        detail_lines = card.detail_lines or ("No additional details.",)
        body.append(
            Container(
                id="approval-details",
                is_visible=False,
                items=_detail_blocks(tuple(detail_lines)),
            )
        )
        body.append(
            ActionSet(
                actions=[
                    *actions,
                    ToggleVisibilityAction(title="Details", target_elements=["approval-details"]),
                ],
            )
        )
    for line in _footer(card, prompt, answered_by):
        body.append(TextBlock(text=line, is_subtle=True, size="Small", wrap=True))
    style = (
        "warning" if card.state == "pending" else "good" if card.state == "approved" else "emphasis"
    )
    return AdaptiveCard(
        body=[Container(style=style, items=body)], fallback_text=confirmation_card_text(card)
    )


@dataclass(frozen=True)
class _PostedCard:
    prompt: ConfirmationPrompt
    conversation_id: str
    service_url: str | None
    message_id: str
    answered_edit_done: asyncio.Event = field(default_factory=asyncio.Event)
    retired: asyncio.Event = field(default_factory=asyncio.Event)
    check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None
    rule_factory: async_sessionmaker[AsyncSession] | None = None
    rule_subject: Callable[[str], Awaitable[Subject]] | None = None


class TeamsConfirmationCards:
    """Posted confirmation cards awaiting a click, keyed by token."""

    def __init__(self, sender: TeamsSender, *, feed: ActivityFeed | None = None) -> None:
        self._sender = sender
        self._feed = feed
        self._controls = PostedConfirmations[_PostedCard]()
        self._always_tasks: set[asyncio.Task[None]] = set()

    def hook(
        self,
        *,
        conversation_id: str,
        service_url: str | None,
        team_group_id: str | None = None,
        requester_display_name: str | None = None,
        runtime: TeamsRuntime | None = None,
        admission: Admission | None = None,
        tenant_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        prepare_admin: Callable[[ConfirmationPrompt], Awaitable[ConfirmationPrompt]] | None = None,
        check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None,
    ) -> ConfirmationHook:
        """A hook that posts each prompt's card into the turn's conversation.

        `team_group_id` is the channel's team, from the inbound activity, for its notice.
        """

        if runtime is not None:
            from daimon.core.channel_backend import backend_selection_enabled, make_backend_service

            if backend_selection_enabled(runtime.settings.turn):
                from daimon.core.confirmation import backend_switch_callbacks
                from mux.contracts.ids import ChannelRef

                if tenant_id is None or account_id is None:
                    raise ValueError("Backend confirmation requires an admitted account and tenant")

                async def current_platform_admin(clicker: str) -> bool:
                    return await _is_admin_now(runtime, clicker, action=None)

                async def current_admin(clicker: str) -> bool:
                    return await _is_admin_now(
                        runtime, clicker, action=DelegatedAction.AGENT_SETTINGS
                    )

                from functools import partial

                from daimon.core.turn.admission import capture_admitted_backend_source

                capture = partial(
                    capture_admitted_backend_source,
                    runtime.turn_deps,
                    admission=admission,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    platform="teams",
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
                    place_word="team",
                    # A channel-pin card clears only the channel this turn runs in.
                    pin_channel=ChannelRef(
                        tenant_id=str(tenant_id),
                        platform="teams",
                        channel_id=admission.origin_channel_id,
                    )
                    if admission is not None and admission.origin_channel_id is not None
                    else None,
                )

        rule_factory = runtime.sessionmaker if runtime is not None else None

        async def rule_subject(clicker: str) -> Subject:
            from daimon.adapters.teams.admin_caller import admin_caller

            if runtime is None or tenant_id is None:
                return Subject()
            await _is_admin_now(runtime, clicker)
            caller = await admin_caller(
                runtime,
                user_id=clicker,
            )
            async with runtime.sessionmaker() as session:
                return await load_live_subject(
                    session,
                    tenant_id=tenant_id,
                    platform="teams",
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
            if requester_display_name:
                prompt = prompt.model_copy(
                    update={"requester_display_name": requester_display_name}
                )

            async def post(token: str) -> _PostedCard:
                card = build_confirmation_card(
                    prompt, state="pending", token=token, allow_always=prompt.tool_rule is not None
                )
                message = MessageActivityInput().add_card(confirmation_adaptive_card(card, prompt))
                sent = await self._sender.send(conversation_id, message, service_url=service_url)
                if self._feed is not None:
                    self._feed.notify(
                        conversation_id,
                        sent.id,
                        recipient_id=prompt.requester_platform_user_id,
                        text=APPROVAL_NOTICE,
                        preview=card.headline,
                        group_id=team_group_id,
                    )
                posted = _PostedCard(
                    prompt,
                    conversation_id,
                    service_url,
                    sent.id,
                    check_admin=check_admin,
                    rule_factory=rule_factory,
                    rule_subject=rule_subject,
                )
                posted_cards.append(posted)
                return posted

            async def retire(posted: _PostedCard, state: ConfirmationCardState) -> None:
                posted.retired.set()
                await self._edit(posted, state)

            async def remind() -> None:
                # Once, after a while: a line in the conversation, and the
                # requester's activity feed (what reaches them in Teams).
                # An @mention of the requester, as the completion ping does.
                requester = Account(
                    id=prompt.requester_platform_user_id,
                    name=prompt.requester_display_name or "you",
                )
                sent = await self._sender.send(
                    conversation_id,
                    answer_message(STILL_WAITING_MESSAGE, mention=requester),
                    service_url=service_url,
                )
                if self._feed is not None:
                    self._feed.notify(
                        conversation_id,
                        sent.id,
                        recipient_id=prompt.requester_platform_user_id,
                        text=STILL_WAITING_MESSAGE,
                        preview=prompt.title,
                        group_id=team_group_id,
                    )

            result = await self._controls.confirm(
                prompt,
                post=post,
                retire=retire,
                post_errors=TEAMS_SEND_ERRORS,
                remind=remind,
            )
            if result == "approved" and posted_cards:
                answered_posted = posted_cards[0]

                async def retire_unsent() -> None:
                    # Queued behind the Approved edit on the same card
                    # (`edit_card_within` keeps per-card call order).
                    await answered_posted.answered_edit_done.wait()
                    answered_posted.retired.set()
                    await self._edit(answered_posted, "stopped")

                return ApprovedConfirmation(answer="approved", retire_unsent=retire_unsent)
            return result

        return _confirm

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        """Approve/Deny click: only the requester's first click answers."""
        activity = ctx.activity
        data = submitted_fields(activity.value.action.data)
        token = str(data.get("token") or "")
        if data.get("op") == "always":
            posted = self._controls.cards.get(token)
            if posted is None or posted.prompt.tool_rule is None:
                return toast(self._controls.missing_message(token))
            if canonical_uuid(activity.from_.aad_object_id) is None:
                return toast(ONLY_ADMIN_MESSAGE)
            task = asyncio.create_task(self._on_always(ctx, token))
            self._always_tasks.add(task)
            task.add_done_callback(self._always_tasks.discard)
            return toast("Checking you're an admin...")
        answer = _ANSWERS.get(str(data.get("op") or ""))
        posted = self._controls.cards.get(token)
        if posted is None or answer is None:
            return toast(self._controls.missing_message(token))
        clicker = canonical_uuid(activity.from_.aad_object_id)
        refusal = await check_confirmation_admin(posted.prompt, clicker, posted.check_admin, answer)
        if refusal is not None:
            return toast(refusal)
        if refusal := self._controls.claim(token, clicker, answer):
            if refusal == NOT_YOURS_MESSAGE:
                refusal = refusal.format(
                    requester=posted.prompt.requester_display_name or "requester"
                )
            return toast(refusal)
        card = build_confirmation_card(
            posted.prompt,
            state=answer,
            answered_by_platform_user_id=clicker,
            how="once" if posted.prompt.tool_rules_enabled else None,
        )
        name = activity.from_.name
        # The answered card goes through the card's edit queue, registered
        # before this returns, rather than as the invoke response: a delayed
        # response could otherwise land after a Stopped retire that followed.
        queue_card_edit(
            self._send_card(posted, card, answered_by=name),
            card_key=self._card_key(posted),
            failure_errors=TEAMS_SEND_ERRORS,
            failed_event="teams.tool_confirmation.edit_failed",
        )
        posted.answered_edit_done.set()
        return toast(_ANSWER_TOASTS[answer])

    async def _on_always(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity], token: str
    ) -> None:
        posted = self._controls.cards.get(token)
        if posted is None:
            return
        reference = posted.prompt.tool_rule
        if reference is None or posted.rule_factory is None or posted.rule_subject is None:
            return
        clicker = canonical_uuid(ctx.activity.from_.aad_object_id)
        if clicker is None:
            return
        try:
            subject = await posted.rule_subject(clicker)
            scope = allowed_rule_scope(reference, subject)
        except Exception:
            subject = Subject()
            scope = None
        if scope is None:
            message = ONLY_ADMIN_MESSAGE
            if clicker == posted.prompt.requester_platform_user_id:
                message += "\n" + ASK_ADMIN_MESSAGE
            await self._notify_clicker(ctx, message)
            return
        if refusal := self._controls.claim_admin(token):
            await self._notify_clicker(ctx, refusal)
            return
        once = build_confirmation_card(
            posted.prompt, state="approved", answered_by_platform_user_id=clicker, how="once"
        )
        queue_card_edit(
            self._send_card(posted, once, answered_by=ctx.activity.from_.name),
            card_key=self._card_key(posted),
            failure_errors=TEAMS_SEND_ERRORS,
            failed_event="teams.tool_confirmation.edit_failed",
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
            how = "always" if scope == "agent" else "channel"
            card = build_confirmation_card(
                posted.prompt,
                state="approved",
                answered_by_platform_user_id=clicker,
                how=how,
            )
            queue_card_edit(
                self._send_card(posted, card, answered_by=ctx.activity.from_.name),
                card_key=self._card_key(posted),
                failure_errors=TEAMS_SEND_ERRORS,
                failed_event="teams.tool_confirmation.edit_failed",
            )
        if result != "saved":
            await self._notify_clicker(
                ctx, STALE_RULE_MESSAGE if result == "stale" else SAVE_FAILED_MESSAGE
            )

    @staticmethod
    def _card_key(posted: _PostedCard) -> object:
        return ("teams", posted.conversation_id, posted.message_id)

    async def _notify_clicker(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity], message: str
    ) -> None:
        conversation_type = ctx.activity.conversation.conversation_type
        if conversation_type not in {"personal", "channel", "groupChat", "meeting"}:
            log.warning("teams.tool_confirmation.private_notice_unknown_conversation")
            return
        notice = MessageActivityInput(text=message)
        if conversation_type != "personal":
            notice.recipient = ctx.activity.from_.model_copy(update={"is_targeted": True})
        try:
            await ctx.send(notice)
        except Exception as err:
            log.warning("teams.tool_confirmation.private_notice_failed", error=str(err))

    def _send_card(
        self, posted: _PostedCard, card: ConfirmationCard, *, answered_by: str | None = None
    ) -> Awaitable[object]:
        edit = MessageActivityInput(id=posted.message_id).add_card(
            confirmation_adaptive_card(card, posted.prompt, answered_by=answered_by)
        )

        async def _send_with_retry() -> object:
            # The click was acknowledged and its token claimed, so a failed
            # card edit could never be repaired by another click: retry a
            # transient failure a few times before giving up (and logging).
            for attempt, delay in enumerate(_CARD_SEND_RETRY_DELAYS_S):
                try:
                    return await self._sender.send(
                        posted.conversation_id, edit, service_url=posted.service_url
                    )
                except TEAMS_SEND_ERRORS:
                    if attempt == len(_CARD_SEND_RETRY_DELAYS_S) - 1:
                        raise
                    await asyncio.sleep(delay)
            raise AssertionError("unreachable")

        return _send_with_retry()

    async def _edit(self, posted: _PostedCard, state: ConfirmationCardState) -> None:
        card = build_confirmation_card(posted.prompt, state=state)
        # Bounded for the turn, finished in the background (`edit_card_within`).
        await edit_card_within(
            self._send_card(posted, card),
            card_key=self._card_key(posted),
            budget_s=EDIT_TIMEOUT_S,
            failure_errors=TEAMS_SEND_ERRORS,
            failed_event="teams.tool_confirmation.edit_failed",
        )

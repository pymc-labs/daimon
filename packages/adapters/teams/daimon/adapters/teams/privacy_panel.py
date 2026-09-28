"""The `privacy` command and its card actions: see, export or delete what is held about you.

Mirrors Slack's `/privacy`. Reads never create a principal. Delete takes a
confirmation where the clicker types their Teams display name; the confirm
click re-verifies the clicker and refuses unless their account is still the
one the confirmation was rendered for. The purge then runs in the background
and edits the card when done, since deleting sessions can outlast an invoke.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import structlog
from daimon.adapters.teams.card_actions import (
    FAILED,
    PANEL_ERRORS,
    card_actor,
    guarded,
    replace_card,
    text_card,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.privacy_card import (
    CONFIRM_INPUT,
    confirm_card,
    export_card,
    no_data_card,
    panel_card,
    post_delete_card,
)
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.observability import capture_exception_with_scope
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account
from daimon.core.stores.identity import find_platform_principal
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import AdaptiveCard

log = structlog.get_logger()

NAME_MISMATCH = "That doesn't match your name."
STALE = "Could not verify your account, so nothing was deleted. Please send privacy again."
DELETING = "⏳ Deleting… this may take a moment."


class PrivacyPanel:
    """Handlers for the command and the panel buttons."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime
        self._purges: set[asyncio.Task[None]] = set()

    async def command(self, context: CommandContext) -> None:
        bot = context.inbound.bot_name or "daimon"
        await context.send_card(
            await self._panel(context.tenant_id, context.inbound.user_id, bot=bot)
        )

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx), toast(FAILED), "teams.privacy.failed")

    async def _account(self, tenant_id: uuid.UUID, user_id: str) -> uuid.UUID | None:
        async with self._runtime.sessionmaker() as session:
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform="teams", external_id=user_id
            )
        return None if principal is None else principal.account_id

    async def _panel(self, tenant_id: uuid.UUID, user_id: str, *, bot: str) -> AdaptiveCard:
        account_id = await self._account(tenant_id, user_id)
        if account_id is None:
            return no_data_card(bot)
        preview = await collect_purge_preview(sm=self._runtime.sessionmaker, account_id=account_id)
        policy_url = str(self._runtime.settings.privacy_policy_url)
        return panel_card(preview, bot=bot, policy_url=policy_url)

    async def _act(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        activity = ctx.activity
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        data = activity.value.action.data
        op, bot = data.get("op"), activity.recipient.name or "daimon"
        if op not in ("export", "delete", "confirm_delete"):
            return replace_card(await self._panel(actor.tenant_id, actor.user_id, bot=bot))
        account_id = await self._account(actor.tenant_id, actor.user_id)
        if account_id is None:
            return replace_card(no_data_card(bot))
        preview = await collect_purge_preview(sm=self._runtime.sessionmaker, account_id=account_id)
        if op == "export":
            return replace_card(export_card(preview, bot=bot))
        name = activity.from_.name or ""
        if op == "delete":
            return replace_card(confirm_card(preview, account_id=account_id, name=name))
        typed = str(data.get(CONFIRM_INPUT) or "").strip()
        if not name or typed != name:
            card = confirm_card(preview, account_id=account_id, name=name, error=NAME_MISMATCH)
            return replace_card(card)
        if str(data.get("account")) != str(account_id):
            log.warning("teams.privacy.account_mismatch", account_id=str(account_id))
            return replace_card(text_card("🔒 Privacy", STALE))
        purge = asyncio.create_task(self._purge(ctx, account_id, bot), name="teams.privacy.purge")
        self._purges.add(purge)
        purge.add_done_callback(self._purges.discard)
        return replace_card(text_card("🔒 Privacy", DELETING))

    async def _purge(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity], account_id: uuid.UUID, bot: str
    ) -> None:
        """Purge, then edit the card to the outcome. Logs carry counts, never who."""
        try:
            runtime = self._runtime
            result = await purge_account(
                sm=runtime.sessionmaker, account_id=account_id, anthropic=runtime.anthropic
            )
            log.info(
                "teams.privacy.deleted",
                account_id=str(account_id),
                sessions_deleted=result.sessions.deleted,
                sessions_failed=result.sessions.failed,
                **result.db.model_dump(),
            )
            card = post_delete_card(result, bot=bot)
        except PANEL_ERRORS as exc:
            log.error("teams.privacy.purge_failed", account_id=str(account_id), exc_info=exc)
            capture_exception_with_scope(exc)
            card = text_card("🔒 Privacy", FAILED)
        edit = MessageActivityInput(id=ctx.activity.reply_to_id).add_card(card)
        with contextlib.suppress(*TEAMS_SEND_ERRORS):
            await ctx.send(edit)

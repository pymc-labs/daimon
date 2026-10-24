"""Discord's `ConfirmationHook`: post a confirmation card, wait for its button.

The card's words and states come from `daimon.core.posted_controls.confirmation`;
this module only draws them as a components-v2 `LayoutView` with Approve and
Deny, and turns the click into a `ConfirmationAnswer`.

The view is non-persistent: the turn waiting on the answer lives in
this process, so a live View instance holds the future and no custom_id or
`DynamicItem` registration is needed. If the process restarts, the turn that
was waiting dies with it and the card's buttons simply stop answering.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.discord.theme import COLOR_AMBER, COLOR_GREEN, COLOR_GREYPLE
from daimon.core.authz import Subject
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationHook,
    ConfirmationPrompt,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.native_admins import AdminCaller, load_live_subject
from daimon.core.posted_controls.confirmation import (
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    ConfirmationCardState,
    ConfirmationHow,
    build_confirmation_card,
)
from daimon.core.posted_controls.lifecycle import (
    STILL_WAITING_MESSAGE,
    check_confirmation_admin,
    edit_card_within,
    queue_card_edit,
    settle_local_admin_confirmation,
    settle_local_confirmation,
    undo_confirmation_admin,
    wait_local_confirmation,
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.runtime import DiscordRuntime
    from daimon.core.turn.admission import Admission

__all__ = ["build_confirmation_view", "confirmation_requester", "discord_confirmation_hook"]

log = structlog.get_logger(__name__)

#: Most time a card edit may take while retiring it.
RETIRE_TIMEOUT_S = 2.0
INTERACTION_TOKEN_LIFETIME_S = 15 * 60


def _body(card: ConfirmationCard) -> list[str]:
    lines = [f"**{_safe_text(card.headline)}**"]
    if card.body:
        lines.append(_safe_text(card.body))
    if card.consequence:
        lines.append(_safe_text(card.consequence))
    return lines


def _safe_text(value: str) -> str:
    """Keep tool-provided words literal in Discord text displays and replies."""
    escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(value))
    return re.sub(r"<#(?=\d+>)", "<#\u200b", escaped)


def _footer(card: ConfirmationCard, prompt: ConfirmationPrompt) -> str | None:
    if card.footer is None:
        return None
    return card.footer.format(
        requester=f"<@{prompt.requester_platform_user_id}>",
        actor=f"<@{card.answered_by_platform_user_id}>",
        expires=f"<t:{int(prompt.expires_at.timestamp())}:R>",
    )


class _ConfirmationView(discord.ui.LayoutView):
    """One card; resolves `answer` on the requester's first click."""

    def __init__(
        self,
        card: ConfirmationCard,
        prompt: ConfirmationPrompt,
        answer: asyncio.Future[ConfirmationAnswer] | None,
        check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None,
        rule_factory: async_sessionmaker[AsyncSession] | None = None,
        rule_subject: Callable[[str], Awaitable[Subject]] | None = None,
        note: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._prompt = prompt
        self._answer = answer
        self._check_admin = check_admin
        self._rule_factory = rule_factory
        self._rule_subject = rule_subject
        self.answered_edit_done = asyncio.Event()
        self.retired = False
        #: The posted card's ordering key for `edit_card_within`; set once the
        #: card is sent, before anyone can click it.
        self.card_key: object = ("discord-view", id(self))
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
            accent_colour=discord.Colour(
                COLOR_AMBER
                if card.state == "pending"
                else COLOR_GREEN
                if card.state == "approved"
                else COLOR_GREYPLE
            )
        )
        for text in _body(card):
            container.add_item(discord.ui.TextDisplay(text))
        if prompt.admin:
            for text in card.detail_lines:
                container.add_item(discord.ui.TextDisplay(_safe_text(text)))
        if card.state == "pending":
            container.add_item(discord.ui.Separator())
            row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            approve: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.success, label="Confirm" if prompt.admin else "Approve"
            )
            deny: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.danger, label="Deny"
            )
            details: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                style=discord.ButtonStyle.secondary, label="Details"
            )
            approve.callback = self._on_approve
            deny.callback = self._on_deny
            details.callback = self._on_details
            row.add_item(approve)
            row.add_item(deny)
            if card.allow_always:
                always: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                    style=discord.ButtonStyle.secondary, label="Always allow"
                )
                always.callback = self._on_always
                row.add_item(always)
            # An admin card already shows its details above the buttons.
            if not prompt.admin:
                row.add_item(details)
            container.add_item(row)
        footer = _footer(card, prompt)
        if footer is not None:
            for line in footer.splitlines():
                container.add_item(discord.ui.TextDisplay(f"-# {line}"))
        if note is not None:
            container.add_item(discord.ui.TextDisplay(f"-# {note}"))
        self.add_item(container)

    def pending_requester(self) -> str | None:
        """The requester while this card still waits for an answer, else None."""
        if self._answer is None or self.is_finished():
            return None
        return self._prompt.requester_platform_user_id

    async def _on_approve(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, "approved")

    async def _on_deny(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, "denied")

    async def _on_always(self, interaction: discord.Interaction) -> None:
        clicked_at = monotonic()
        reference = self._prompt.tool_rule
        clicker = str(interaction.user.id)
        if reference is None or self._rule_factory is None or self._rule_subject is None:
            await interaction.response.send_message(NO_LONGER_PENDING_MESSAGE, ephemeral=True)
            return
        await interaction.response.defer()
        try:
            subject = await self._rule_subject(clicker)
            scope = allowed_rule_scope(reference, subject)
        except Exception:
            scope = None
            subject = Subject()
        if scope is None:
            message = ONLY_ADMIN_MESSAGE
            if clicker == self._prompt.requester_platform_user_id:
                message += "\n" + ASK_ADMIN_MESSAGE
            await interaction.followup.send(message, ephemeral=True)
            return
        if refusal := settle_local_admin_confirmation(self._prompt, self._answer):
            await interaction.followup.send(refusal, ephemeral=True)
            return
        self.stop()
        self._queue_always_answer(interaction, clicker, "once")
        self.answered_edit_done.set()
        try:
            result = await save_always_allow(
                self._rule_factory,
                reference=reference,
                scope=scope,
                subject=subject,
                clicker=clicker,
            )
        except Exception:
            result = "failed"
        if (
            result == "saved"
            and not self.retired
            and monotonic() - clicked_at < INTERACTION_TOKEN_LIFETIME_S
        ):
            how: ConfirmationHow = "always" if scope == "agent" else "channel"
            self._queue_always_answer(interaction, clicker, how)
        elif result == "saved" and not self.retired:
            log.info("tool_confirmation.always_edit_token_expired")
        if result != "saved":
            await interaction.followup.send(
                STALE_RULE_MESSAGE if result == "stale" else SAVE_FAILED_MESSAGE,
                ephemeral=True,
            )

    def _queue_always_answer(
        self, interaction: discord.Interaction, clicker: str, how: ConfirmationHow
    ) -> None:
        answered = build_confirmation_card(
            self._prompt,
            state="approved",
            answered_by_platform_user_id=clicker,
            how=how,
        )
        queue_card_edit(
            interaction.edit_original_response(
                view=_ConfirmationView(answered, self._prompt, answer=None),
                allowed_mentions=discord.AllowedMentions.none(),
            ),
            card_key=self.card_key,
            failure_errors=(discord.HTTPException,),
            failed_event="tool_confirmation.answer_edit_failed",
        )

    async def _on_details(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "\n".join(_safe_text(line) for line in self._prompt.detail_lines)
            or "No additional details.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def authorize(self, clicker: str, answer: ConfirmationAnswer) -> str | None:
        """Take `clicker`'s answer through every check a click runs; the refusal, or None.

        The admin re-check (with the card's own digest) first, then requester,
        expiry and single use. None means the answer was taken.
        """
        refusal = await check_confirmation_admin(self._prompt, clicker, self._check_admin, answer)
        if refusal is not None:
            return refusal
        settled = settle_local_confirmation(self._prompt, self._answer, clicker, answer)
        if settled is not None:
            await undo_confirmation_admin(self._prompt, clicker, answer, self._check_admin)
        return settled

    async def _settle(self, interaction: discord.Interaction, answer: ConfirmationAnswer) -> None:
        refusal = await self.authorize(str(interaction.user.id), answer)
        if refusal is not None:
            if refusal == NOT_YOURS_MESSAGE:
                refusal = refusal.format(requester=f"<@{self._prompt.requester_platform_user_id}>")
            await interaction.response.send_message(
                refusal, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        # Answered: nothing on this card listens any more.
        self.stop()
        answered = build_confirmation_card(
            self._prompt,
            state=answer,
            answered_by_platform_user_id=str(interaction.user.id),
            how="once" if self._prompt.tool_rules_enabled else None,
        )
        # Same per-card order as `_retire`, registered before any await, so a
        # Stopped retire that follows can never land before this answer.
        try:
            await edit_card_within(
                interaction.response.edit_message(
                    view=_ConfirmationView(answered, self._prompt, answer=None),
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                card_key=self.card_key,
                budget_s=RETIRE_TIMEOUT_S,
                failure_errors=(discord.HTTPException,),
                failed_event="tool_confirmation.answer_edit_failed",
            )
        finally:
            self.answered_edit_done.set()


def build_confirmation_view(
    card: ConfirmationCard,
    prompt: ConfirmationPrompt,
    answer: asyncio.Future[ConfirmationAnswer] | None = None,
) -> discord.ui.LayoutView:
    """The components-v2 view for `card`; buttons only while pending."""
    return _ConfirmationView(card, prompt, answer)


def confirmation_requester(view: object) -> str | None:
    """Who may answer `view`, when it is a live, unanswered confirmation card.

    None for any other view, so the staging confirm click (`confirm_click`) can only
    press Confirm cards that are still waiting.
    """
    return view.pending_requester() if isinstance(view, _ConfirmationView) else None


async def tenant_guild(
    client: discord.Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> discord.Guild | None:
    """The Discord server a tenant stands for, or None when it can't be found."""
    from daimon.core.stores.tenants import get_tenant

    async with sessionmaker() as session:
        tenant = await get_tenant(session, tenant_id)
    if tenant is None or tenant.platform != "discord" or not tenant.external_id.isdigit():
        return None
    guild_id = int(tenant.external_id)
    guild = client.get_guild(guild_id)
    if guild is not None:
        return guild
    try:
        return await client.fetch_guild(guild_id)
    except discord.HTTPException:
        return None


def discord_confirmation_hook(
    channel: discord.abc.Messageable,
    *,
    runtime: DiscordRuntime | None = None,
    admission: Admission | None = None,
    tenant_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    prepare_admin: Callable[[ConfirmationPrompt], Awaitable[ConfirmationPrompt]] | None = None,
    check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None,
    client: discord.Client | None = None,
) -> ConfirmationHook:
    """A hook that posts each prompt's card into `channel` (the turn's thread).

    The clicker's admin role is read live from the server the turn belongs to:
    the thread's or text channel's own server, or for a DM the tenant's server,
    which needs `client`. Anywhere else, or a DM without `client`, the clicker
    counts as no admin.
    """

    async def role_guild() -> discord.Guild | None:
        if isinstance(channel, (discord.Thread, discord.TextChannel)):
            return channel.guild
        if (
            not isinstance(channel, discord.DMChannel)
            or client is None
            or runtime is None
            or tenant_id is None
        ):
            return None
        return await tenant_guild(client, runtime.sessionmaker, tenant_id)

    if runtime is not None:
        from daimon.core.channel_backend import backend_selection_enabled, make_backend_service

        if backend_selection_enabled(runtime.settings.turn):
            from daimon.adapters.discord.bot import requester_role
            from daimon.core.confirmation import backend_switch_callbacks
            from daimon.core.stores.domain import Role
            from mux.contracts.ids import ChannelRef

            if tenant_id is None or account_id is None:
                raise ValueError("Backend confirmation requires an admitted account and tenant")

            async def current_admin(clicker: str) -> bool:
                guild = await role_guild()
                if guild is None:
                    return False
                role, _ = await requester_role(guild, clicker)
                return role is Role.ADMIN

            from daimon.core.turn.admission import capture_admitted_backend_source

            capture = partial(
                capture_admitted_backend_source,
                runtime.turn_deps,
                admission=admission,
                tenant_id=tenant_id,
                account_id=account_id,
                platform="discord",
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
                answering_agent_name=(
                    getattr(getattr(admission, "config", None), "agent_name", None)
                ),
                default_agent_name=runtime.deployment_default.agent_name,
                place_word="server",
                # A hand-off earlier in this thread decides whose conversation moves.
                handoff_thread=(
                    ("discord", str(channel.parent_id), str(channel.id))
                    if isinstance(channel, discord.Thread)
                    else None
                ),
                # A channel-pin card clears only the channel this turn runs in.
                pin_channel=ChannelRef(
                    tenant_id=str(tenant_id),
                    platform="discord",
                    channel_id=admission.origin_channel_id,
                )
                if admission is not None and admission.origin_channel_id is not None
                else None,
            )

    rule_factory = runtime.sessionmaker if runtime is not None else None

    async def rule_subject(clicker: str) -> Subject:
        from daimon.adapters.discord.bot import requester_role
        from daimon.core.stores.domain import Role

        if rule_factory is None or tenant_id is None:
            return Subject()
        guild = await role_guild()
        if guild is None:
            return Subject()
        role, _roles = await requester_role(guild, clicker)
        async with rule_factory() as session:
            return await load_live_subject(
                session,
                tenant_id=tenant_id,
                platform="discord",
                caller=AdminCaller(
                    platform_user_id=clicker,
                    is_server_admin=role is Role.ADMIN,
                ),
            )

    async def _qa_auto_approve(
        view: _ConfirmationView, prompt: ConfirmationPrompt
    ) -> ApprovedConfirmation | None:
        """On staging, answer a QA bot's own card as that bot; None posts the normal card."""
        from daimon.adapters.discord import qa_auto_approve as qa

        requester = prompt.requester_platform_user_id
        try:
            enabled = await qa.enabled_for(getattr(runtime, "settings", None), requester)
        except Exception:
            # The gate never breaks a card: any failure is a normal card.
            log.exception("discord.qa_auto_approve_gate_failed")
            enabled = False
        if not enabled:
            return None
        guild = getattr(channel, "guild", None)
        audit = partial(
            qa.audit,
            rule_factory,
            tenant_id=tenant_id
            if tenant_id is not None
            else derive_tenant_uuid(platform="discord", workspace_id=str(guild.id))
            if isinstance(guild, discord.Guild)
            else None,
            prompt=prompt,
            channel_id=getattr(channel, "id", None),
        )
        if prompt.lasting_public_link:
            await audit(outcome="denied", reason="manual: publishes a lasting public link")
            return None
        refusal = await view.authorize(requester, "approved")
        if refusal is not None:
            # Exactly what the requester's own click would get: no answer taken.
            await audit(outcome="denied", reason=f"refused: {refusal}")
            return None
        view.stop()
        answered = build_confirmation_card(
            prompt,
            state="approved",
            answered_by_platform_user_id=requester,
            how="once" if prompt.tool_rules_enabled else None,
        )
        try:
            message = await channel.send(
                view=_ConfirmationView(answered, prompt, answer=None, note=qa.NOTE),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except BaseException as exc:
            # Approved, but the card never showed: say so, then fail as a post would.
            await audit(outcome="error", reason=f"card not posted: {type(exc).__name__}")
            raise
        view.card_key = ("discord", message.id)
        view.answered_edit_done.set()
        await audit(outcome="allowed", reason="auto-approved for QA")

        async def retire_unsent() -> None:
            view.retired = True
            await _retire(message, prompt, "stopped", note=qa.NOTE)

        return ApprovedConfirmation(answer="approved", retire_unsent=retire_unsent)

    async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer | ApprovedConfirmation:
        if prompt.admin:
            if prepare_admin is None or check_admin is None:
                return "denied"
            prompt = await prepare_admin(prompt)
        if prompt.tool_rule is not None and rule_factory is not None:
            prompt = await snapshot_tool_rule(prompt, rule_factory)
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[ConfirmationAnswer] = loop.create_future()
        pending = build_confirmation_card(
            prompt,
            state="pending",
            token=secrets.token_urlsafe(12),
            allow_always=prompt.tool_rule is not None,
        )
        view = _ConfirmationView(pending, prompt, answer, check_admin, rule_factory, rule_subject)
        if (approved := await _qa_auto_approve(view, prompt)) is not None:
            return approved
        message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        view.card_key = ("discord", message.id)

        async def retire(state: ConfirmationCardState) -> None:
            view.retired = True
            await _retire(message, prompt, state)

        async def remind() -> None:
            # Once, after a while: the requester is pinged in the card's own thread.
            await message.reply(
                f"<@{prompt.requester_platform_user_id}> {STILL_WAITING_MESSAGE}",
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False, replied_user=False
                ),
                mention_author=False,
            )

        result = await wait_local_confirmation(
            prompt, answer, stop=view.stop, retire=retire, remind=remind
        )
        if result == "approved":

            async def retire_unsent() -> None:
                # Queued behind the Approved edit on the same card.
                await view.answered_edit_done.wait()
                view.retired = True
                await retire("stopped")

            return ApprovedConfirmation(answer="approved", retire_unsent=retire_unsent)
        return result

    return _confirm


async def _retire(
    message: discord.Message,
    prompt: ConfirmationPrompt,
    state: ConfirmationCardState,
    *,
    note: str | None = None,
) -> None:
    card = build_confirmation_card(prompt, state=state)
    # Bounded for the turn, finished in the background (`edit_card_within`).
    await edit_card_within(
        message.edit(
            view=_ConfirmationView(card, prompt, answer=None, note=note),
            allowed_mentions=discord.AllowedMentions.none(),
        ),
        card_key=("discord", message.id),
        budget_s=RETIRE_TIMEOUT_S,
        failure_errors=(discord.HTTPException,),
        failed_event="tool_confirmation.retire_failed",
    )

"""SupportEscalateButton + SupportModal -- the human-support path.

A third seeded reaction (`ESCALATE`) sits beside the two vote emoji on every
final answer. It is deliberately a REACTION rather than a button on the answer
itself: `feedback_seed.py` already seeds emoji on that message and
`feedback_reactions.py` already listens, so this reuses a path that exists and
is tested, and the turn's render path is untouched. A real button would mean
every final message carries a persistent View -- a much larger change for the
same affordance. If one-click ever matters more than that, the upgrade is to
add a View here, not to redesign this.

Because a reaction carries no interaction handle, the same bridge
`feedback_button.py` documents applies: react -> direct message carrying a
button -> modal. The custom_id carries `(channel_id, message_id)` rather than a
row id because NO ROW EXISTS YET. Reacting must not spend a credit; only
sending the note does. That is also why the credit gate is evaluated twice --
once here for a cheap early "you're out", and authoritatively inside the write
transaction, which is the one that counts.

Template-disjointness: the `sup:` prefix must never overlap `mfb:` (feedback),
`ztc:` (credential requests) or the wizard's prefixes. discord.py fullmatches
an incoming custom_id against EVERY registered template with no early break,
so an overlap fires two handlers on one click.

Requests land in a CHANNEL, not in operator direct messages. A channel
survives one person's DMs being closed, leaves a shared record anyone on the
rota can pick up, and does not silently make whoever is first in a config list
the only person who ever hears anything.

There is no exception: `daimon.core.support_routing` has no DM tier, so every
request goes to the channel and no one is messaged directly.

Delivery ordering is the load-bearing part of this module. The row is
committed BEFORE the post is attempted, and `delivered_at` is stamped only
once it actually lands. A support request from a paying trial client that
vanishes because the channel was misconfigured or the bot lost access is the
one outcome here worth engineering against. An undelivered row can be swept
later; a dropped one cannot.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Self, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot, requester_role
from daimon.adapters.discord.checks import is_member_guild_admin
from daimon.adapters.discord.notice_card import build_notice_embed
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.support_post import (
    discord_channel,
    post_to_support_channel,
    request_facts,
)
from daimon.adapters.discord.support_settle import settle_view
from daimon.core.features.snapshot import FeaturePlace, place_feature
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.permissions import readers_limited_at
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.platform_names import get_channel_row
from daimon.core.stores.support_escalation import (
    SubmitOutcome,
    asked_options,
    mark_delivered,
    resolve_plan,
    submit_request,
)
from daimon.core.stores.support_plans import plans_for_place
from daimon.core.stores.support_rows import PlanView, SupportEscalationRow
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support_escalation import (
    ALREADY_REQUESTED,
    ASK_THE_TEAM,
    CUSTOM_ID_TEMPLATE,
    EMPTY_NOTE,
    ESCALATE,
    SEALED_NOTE_HINT,
    SEALED_POST_MARKER,
    UNAVAILABLE,
    build_custom_id,
)
from daimon.core.support_plans import (
    CHOOSE_OPTION,
    EXEMPT_OFFER,
    MAX_PARAGRAPH_CHARS,
    OPTIONS_CHANGED,
    ChoiceField,
    RequestChoice,
    StepsPrice,
    SupportOption,
    SupportPlace,
    SupportPlanError,
    TextField,
    balance_text,
    credits_text,
    ended_text,
    governing_plan,
    inactive_text,
    offer_refusal,
    option_value,
    pick_refusal,
    picked_option,
    price_text,
    received_text,
    refusal_text,
)
from sqlalchemy.ext.asyncio import AsyncSession

import discord
from discord.ext import commands

_log = structlog.get_logger()

_CALLBACK_FAILED = "That didn't work. Try again."
_SUBMIT_FAILED = "That didn't work. Try again."
MENU_TEMPLATE: Final = (
    r"spm:(?P<guild_id>[0-9]{1,20}):(?P<channel_id>[0-9]{1,20})"
    r":(?P<message_id>[0-9]{1,20})"
)


@dataclass(frozen=True)
class AnswerOrigin:
    """The answer a request is about, and the place its plan and feature are read at."""

    guild_id: str
    channel_id: str
    message_id: str
    tenant_id: uuid.UUID
    parent_id: str
    thread_id: str | None
    agent_id: str | None
    ma_session_id: str | None
    budget_channel_id: str | None

    @property
    def link(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"

    @property
    def support_place(self) -> SupportPlace:
        return SupportPlace.of_thread(
            channel_id=self.parent_id,
            budget_channel_id=self.budget_channel_id,
            agent_id=self.agent_id,
        )

    @property
    def feature_place(self) -> FeaturePlace:
        return FeaturePlace.of(
            self.tenant_id, "discord", self.parent_id, self.thread_id, agent_id=self.agent_id
        )


async def _answer_place(
    bot: commands.Bot,
    runtime: DiscordRuntime,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    fetch: bool,
) -> tuple[str, str | None]:
    """`(channel, thread)` for the answer's channel id: a thread names its parent.

    `fetch=False` is for the click that must open a modal within three seconds:
    the cache, then the channel as last recorded, and never a Discord request.
    """
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None and fetch:
            channel = await bot.fetch_channel(int(channel_id))
    except (discord.HTTPException, ValueError):
        return channel_id, None
    if channel is None:
        async with runtime.sessionmaker() as session:
            known = await get_channel_row(
                session, tenant_id=tenant_id, platform="discord", channel_id=channel_id
            )
        if known is not None and known.kind == "thread" and known.parent_id:
            return known.parent_id, channel_id
        return channel_id, None
    if isinstance(channel, discord.Thread) and channel.parent_id:
        return str(channel.parent_id), channel_id
    return channel_id, None


async def answer_origin(
    runtime: DiscordRuntime,
    bot: commands.Bot,
    *,
    guild_id: str,
    channel_id: str,
    message_id: str,
    fetch: bool = True,
) -> AnswerOrigin:
    """Read where the answer sits now: its channel, thread, agent and session."""
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    parent, thread = await _answer_place(
        bot, runtime, tenant_id=tenant_id, channel_id=channel_id, fetch=fetch
    )
    async with runtime.sessionmaker() as session:
        row = await get_latest_thread_session(
            session, tenant_id=tenant_id, platform="discord", thread_id=channel_id
        )
    return AnswerOrigin(
        guild_id=guild_id,
        channel_id=channel_id,
        message_id=message_id,
        tenant_id=tenant_id,
        parent_id=parent,
        thread_id=thread,
        agent_id=None if row is None else row.ma_agent_id,
        ma_session_id=None if row is None else row.ma_session_id,
        budget_channel_id=None if row is None else row.budget_channel_id,
    )


async def answer_is_sealed(runtime: DiscordRuntime, origin: AnswerOrigin) -> bool:
    """Whether only turns inside the answer's channel (or its thread) read it.

    The same core check Slack and Teams make (`readers_limited_at`). A policy
    that cannot be read counts as sealed: the hint and the marker only ask for
    care, so erring toward them costs nothing.
    """
    try:
        async with runtime.sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=origin.tenant_id)
    except AccessPolicyUnreadable:
        return True
    return readers_limited_at(policy, channel_id=origin.parent_id, thread_id=origin.thread_id)


async def requester_is_admin(bot: commands.Bot, *, guild_id: str, user_id: str) -> bool:
    """Decision 5: a server admin's requests are charged nowhere. Anything unknown charges."""
    guild = bot.get_guild(int(guild_id))
    if not isinstance(guild, discord.Guild):
        return False
    role, _roles = await requester_role(guild, user_id)
    return role == Role.ADMIN


def requester_is_admin_cached(
    interaction: discord.Interaction[Any], bot: commands.Bot, *, guild_id: str
) -> bool:
    """As `requester_is_admin`, from the interaction and the cache: no Discord request.

    For the click that must open a modal within three seconds. A member neither
    on the interaction nor cached charges; the submit decides for good.
    """
    guild = bot.get_guild(int(guild_id))
    if not isinstance(guild, discord.Guild):
        return False
    user = interaction.user
    member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
    return member is not None and is_member_guild_admin(member, guild_owner_id=guild.owner_id)


def _sealed_hint() -> str:
    """The form's hint as subtext, its two lines a blank line apart."""
    return "\n\n".join(f"-# {line}" for line in SEALED_NOTE_HINT.split("\n\n"))


async def _notice(
    interaction: discord.Interaction[Any], text: str, *, neutral: bool = False
) -> None:
    await interaction.followup.send(embed=build_notice_embed(text, neutral=neutral), ephemeral=True)


async def _report_failure(
    interaction: discord.Interaction[Any], text: str, *, neutral: bool = False
) -> None:
    embed = build_notice_embed(text, neutral=neutral)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _refuse(interaction: discord.Interaction[Any], outcome: SubmitOutcome) -> None:
    if outcome.status == "out_of_credits" and outcome.view is not None:
        _log.info("support.out_of_credits", charged_to=outcome.view.charged_to)
    text = refusal_text(outcome, now=datetime.now(UTC))
    await _notice(interaction, text, neutral=outcome.status == "duplicate")


async def _asked(session: AsyncSession, origin: AnswerOrigin, user_id: str) -> frozenset[str]:
    """The options `user_id` already asked with on this answer."""
    return await asked_options(
        session,
        tenant_id=origin.tenant_id,
        platform="discord",
        platform_user_id=user_id,
        channel_id=origin.channel_id,
        message_id=origin.message_id,
    )


def _post_body(
    interaction: discord.Interaction[Any],
    origin: AnswerOrigin,
    row: SupportEscalationRow,
    view: PlanView,
    *,
    sealed: bool,
) -> str:
    """Today's post for a default request; a plan's adds its terms, status and balance."""
    head = f"**Human support requested** by {interaction.user.mention} ({interaction.user})"
    marker = [f"*{SEALED_POST_MARKER}*"] if sealed else []
    facts: list[str] = []
    if row.charged_to != "default":
        balance = balance_text(view.balance, view.plan) if row.charged_to == "plan" else None
        facts.append(request_facts(row, menu=view.catalog is not None, balance=balance))
    return "\n\n".join(part for part in (head, *facts, origin.link, *marker, row.note) if part)


async def submit_answer_request(
    interaction: discord.Interaction[Any],
    *,
    runtime: DiscordRuntime,
    origin: AnswerOrigin,
    choice: RequestChoice,
) -> None:
    """Record one request and post it, after the interaction was deferred.

    The row is committed BEFORE the post is attempted, and `delivered_at` is
    stamped only once it lands: a failure downgrades the confirmation, never
    the request.
    """
    settings = runtime.settings
    channel_id = discord_channel(settings.support)
    offered = await place_feature(
        runtime.sessionmaker, settings, origin.feature_place, "support_escalation"
    )
    if channel_id is None or not offered.on:
        # Disabled between the offer and the submit. Nothing is spent.
        await _notice(interaction, UNAVAILABLE)
        return
    bot = cast(commands.Bot, interaction.client)
    user_id = str(interaction.user.id)
    exempt = await requester_is_admin(bot, guild_id=origin.guild_id, user_id=user_id)
    try:
        async with runtime.sessionmaker() as session, session.begin():
            principal = await find_platform_principal(
                session, tenant_id=origin.tenant_id, platform="discord", external_id=user_id
            )
            outcome = await submit_request(
                session,
                tenant_id=origin.tenant_id,
                account_id=None if principal is None else principal.account_id,
                platform="discord",
                platform_user_id=user_id,
                channel_id=origin.channel_id,
                message_id=origin.message_id,
                ma_session_id=origin.ma_session_id,
                place=origin.support_place,
                choice=choice,
                default_allowance=settings.support.credits_per_user,
                exempt=exempt,
            )
    except SupportPlanError as err:
        await _notice(interaction, str(err))
        return
    row, view = outcome.row, outcome.view
    if outcome.status != "recorded" or row is None or view is None:
        await _refuse(interaction, outcome)
        return
    # Decided again at submit, as Slack does: the rule may have changed since
    # the form opened, and the post's marker must match it now.
    sealed = await answer_is_sealed(runtime, origin)
    post = await post_to_support_channel(
        bot,
        channel_id=channel_id,
        body=_post_body(interaction, origin, row, view, sealed=sealed),
        view=settle_view(row.id) if row.status == "pending" else None,
    )
    if post is not None:
        async with runtime.sessionmaker() as session, session.begin():
            await mark_delivered(
                session,
                escalation_id=row.id,
                support_channel_id=post.channel_id,
                support_post_id=post.message_id,
            )
    _log.info(
        "support.escalation_recorded",
        escalation_id=str(row.id),
        tenant_id=str(origin.tenant_id),
        delivered=post is not None,
    )
    text = received_text(row, view)
    await _notice(interaction, text, neutral=post is not None)


class SupportModal(discord.ui.Modal, title=ASK_THE_TEAM):
    """The default menu's free-text form; writing it is what spends the credit."""

    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        guild_id: str,
        channel_id: str,
        message_id: str,
        sealed: bool = False,
    ) -> None:
        super().__init__()
        self._runtime = runtime
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._message_id = message_id
        if sealed:
            self.add_item(discord.ui.TextDisplay(_sealed_hint()))
        self.note_input: discord.ui.TextInput[SupportModal] = discord.ui.TextInput(
            label="What do you need help with?",
            # Discord caps a label at 45 characters; Slack says this in its label.
            placeholder="Write a few words.",
            style=discord.TextStyle.paragraph,
            required=True,
            # The core note limit, under Discord's 4000 so the lint can check it.
            max_length=min(MAX_PARAGRAPH_CHARS, 4000),
        )
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        note = str(self.note_input.value or "")
        if not note.strip():
            await _notice(interaction, EMPTY_NOTE)
            return
        # The button arrives by direct message, so `interaction.guild_id` is
        # None and the originating guild comes from the custom_id instead.
        origin = await answer_origin(
            self._runtime,
            cast(commands.Bot, interaction.client),
            guild_id=self._guild_id,
            channel_id=self._channel_id,
            message_id=self._message_id,
        )
        choice = RequestChoice.default(note)
        await submit_answer_request(
            interaction, runtime=self._runtime, origin=origin, choice=choice
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Adapter boundary: discord.py routes on_submit failures here, not to a dispatcher."""
        _log.exception("support_modal.on_error", err_type=type(error).__name__)
        await _report_failure(interaction, _SUBMIT_FAILED)


type _FieldInput = discord.ui.TextInput[SupportOptionModal] | discord.ui.Select[SupportOptionModal]


def _answer(item: _FieldInput) -> str:
    if isinstance(item, discord.ui.TextInput):
        return str(item.value or "")
    return str(item.values[0]) if item.values else ""


class SupportOptionModal(discord.ui.Modal, title=ASK_THE_TEAM):
    """One configured option's form: its terms, then a step and its fields (or just Submit)."""

    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        origin: AnswerOrigin,
        option: SupportOption,
        sealed: bool = False,
    ) -> None:
        super().__init__()
        self._runtime = runtime
        self._origin = origin
        self._option = option
        terms = [f"**{option.label}**: {price_text(option.price)}"]
        if option.description:
            terms.append(option.description)
        head = "\n".join(terms)
        self.add_item(discord.ui.TextDisplay(f"{head}\n\n{_sealed_hint()}" if sealed else head))
        self.step_input: discord.ui.Select[SupportOptionModal] | None = None
        price = option.price
        if isinstance(price, StepsPrice):
            steps: discord.ui.Label[SupportOptionModal] = discord.ui.Label(
                text=price.label,
                component=discord.ui.Select(
                    options=[
                        discord.SelectOption(
                            label=step.label,
                            value=str(index),
                            description=credits_text(step.amount),
                        )
                        for index, step in enumerate(price.steps)
                    ],
                ),
            )
            # `Label.component` is typed `Item`; it is the Select built just above.
            self.step_input = cast(discord.ui.Select[SupportOptionModal], steps.component)
            self.add_item(steps)
        self.field_inputs: dict[str, _FieldInput] = {}
        for spec in option.fields:
            if isinstance(spec, ChoiceField):
                label: discord.ui.Label[SupportOptionModal] = discord.ui.Label(
                    text=spec.label,
                    component=discord.ui.Select(
                        placeholder=spec.placeholder,
                        min_values=1 if spec.required else 0,
                        required=spec.required,
                        options=[discord.SelectOption(label=choice) for choice in spec.choices],
                    ),
                )
            else:
                label = discord.ui.Label(
                    text=spec.label,
                    component=discord.ui.TextInput(
                        style=discord.TextStyle.short
                        if isinstance(spec, TextField)
                        else discord.TextStyle.paragraph,
                        placeholder=spec.placeholder,
                        required=spec.required,
                        max_length=min(spec.max_chars, 4000),
                    ),
                )
            # As above: the component is the input built inside this `Label`.
            self.field_inputs[spec.key] = cast(_FieldInput, label.component)
            self.add_item(label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        picked = self.step_input.values if self.step_input is not None else []
        choice = RequestChoice.of(
            self._option,
            step=int(picked[0]) if picked else None,
            answers={key: _answer(item) for key, item in self.field_inputs.items()},
        )
        await submit_answer_request(
            interaction, runtime=self._runtime, origin=self._origin, choice=choice
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Adapter boundary: discord.py routes on_submit failures here, not to a dispatcher."""
        _log.exception("support_option_modal.on_error", err_type=type(error).__name__)
        await _report_failure(interaction, _SUBMIT_FAILED)


def _menu_lines(option: SupportOption) -> str:
    line = f"**{option.label}**: {price_text(option.price)}"
    return f"{line}\n{option.description}" if option.description else line


def menu_message(origin: AnswerOrigin, view: PlanView) -> tuple[discord.Embed, discord.ui.View]:
    """A configured menu: each option and its price, the balance, and a picker."""
    options = view.spec.options
    embed = discord.Embed(
        title=ASK_THE_TEAM,
        description="\n\n".join(
            [f"Need help with [this answer]({origin.link})?", *map(_menu_lines, options)]
        ),
        colour=discord.Colour.blurple(),
    )
    footer = EXEMPT_OFFER if view.exempt else balance_text(view.balance, view.plan)
    if footer:
        embed.set_footer(text=footer)
    menu = discord.ui.View(timeout=None)
    menu.add_item(
        SupportMenuSelect(
            guild_id=origin.guild_id,
            channel_id=origin.channel_id,
            message_id=origin.message_id,
            options=[
                discord.SelectOption(
                    label=option.label,
                    value=option_value(option),
                    description=price_text(option.price),
                )
                for option in options
            ],
        )
    )
    return embed, menu


class SupportMenuSelect(
    discord.ui.DynamicItem[discord.ui.Select[discord.ui.View]], template=MENU_TEMPLATE
):
    """The configured menu's picker; each option's value carries the terms it showed.

    Picking re-reads the plan (decision 9): a changed menu answers "Options
    changed", an unaffordable option "Not enough credits", before any form.
    """

    def __init__(
        self,
        *,
        guild_id: str,
        channel_id: str,
        message_id: str,
        options: Sequence[discord.SelectOption] = (),
    ) -> None:
        select: discord.ui.Select[discord.ui.View] = discord.ui.Select(
            custom_id=f"spm:{guild_id}:{channel_id}:{message_id}",
            placeholder=CHOOSE_OPTION,
            # Rebuilt from a click, the options are not needed: the pick is in the payload.
            options=list(options) or [discord.SelectOption(label=CHOOSE_OPTION)],
        )
        super().__init__(select)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(
            guild_id=match["guild_id"],
            channel_id=match["channel_id"],
            message_id=match["message_id"],
        )

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            await self._pick(interaction)
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary; discord.py's dispatcher swallows
            # anything raised here
            _log.exception("support_menu.callback_failed", err_type=type(err).__name__)
            await _report_failure(interaction, _CALLBACK_FAILED)

    async def _pick(self, interaction: discord.Interaction[commands.Bot]) -> None:
        bot = cast(DaimonBot, interaction.client)
        runtime = bot.runtime
        origin = await answer_origin(
            runtime,
            bot,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            message_id=self.message_id,
            fetch=False,
        )
        user_id = str(interaction.user.id)
        # No Discord request before the form: it must open inside the
        # interaction's three seconds. The submit decides who is an admin.
        exempt = requester_is_admin_cached(interaction, bot, guild_id=origin.guild_id)
        async with runtime.sessionmaker() as session:
            view = await resolve_plan(
                session,
                tenant_id=origin.tenant_id,
                place=origin.support_place,
                platform_user_id=user_id,
                default_allowance=runtime.settings.support.credits_per_user,
                exempt=exempt,
            )
            asked = await _asked(session, origin, user_id)
        values = self.item.values
        option = picked_option(view.spec, str(values[0])) if values else None
        if option is None:
            await _report_failure(interaction, OPTIONS_CHANGED)
            return
        if not view.active:
            await _report_failure(interaction, ended_text(view, now=datetime.now(UTC)))
            return
        if (refused := pick_refusal(view, option, asked=asked)) is not None:
            await _report_failure(interaction, refused, neutral=refused == ALREADY_REQUESTED)
            return
        sealed = await answer_is_sealed(runtime, origin)
        await interaction.response.send_modal(
            SupportOptionModal(runtime=runtime, origin=origin, option=option, sealed=sealed)
        )


class SupportEscalateButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Persistent button delivered by DM: the note form, or a configured menu.

    Defines no `interaction_check`, for the reason `feedback_button.py`
    documents: the button arrives in a one-to-one direct message, so Discord's
    own channel membership is the outer boundary, and the custom_id carries no
    user identity to compare a clicker against. The authoritative gate is the
    write path -- the escalation is recorded against the CLICKING user's id and
    counted against their own allowance, so a forwarded custom_id spends the
    forwarder's credit, not the original recipient's.
    """

    def __init__(self, *, guild_id: str, channel_id: str, message_id: str) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=ASK_THE_TEAM,
            emoji=ESCALATE,
            custom_id=build_custom_id(
                guild_id=guild_id, channel_id=channel_id, message_id=message_id
            ),
        )
        super().__init__(button)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(
            guild_id=match["guild_id"],
            channel_id=match["channel_id"],
            message_id=match["message_id"],
        )

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        try:
            await self._open(interaction)
        except Exception as err:
            # dynamic-item dispatch is an adapter boundary; discord.py's dispatcher swallows
            # anything raised here
            _log.exception("support_button.callback_failed", err_type=type(err).__name__)
            await _report_failure(interaction, _CALLBACK_FAILED)

    async def _open(self, interaction: discord.Interaction[commands.Bot]) -> None:
        bot = cast(DaimonBot, interaction.client)
        runtime = bot.runtime
        origin = await answer_origin(
            runtime,
            bot,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            message_id=self.message_id,
            fetch=False,
        )
        async with runtime.sessionmaker() as session:
            plans = await plans_for_place(
                session, tenant_id=origin.tenant_id, place=origin.support_place
            )
        plan = governing_plan(plans, origin.support_place)
        if plan is not None and (ended := inactive_text(plan, now=datetime.now(UTC))):
            await _report_failure(interaction, ended)
            return
        if plan is not None and plan.catalog_id is not None:
            # A menu configured after this button was sent: show it now.
            await interaction.response.defer(ephemeral=True, thinking=True)
            user_id = str(interaction.user.id)
            exempt = await requester_is_admin(bot, guild_id=origin.guild_id, user_id=user_id)
            async with runtime.sessionmaker() as session:
                view = await resolve_plan(
                    session,
                    tenant_id=origin.tenant_id,
                    place=origin.support_place,
                    platform_user_id=user_id,
                    default_allowance=runtime.settings.support.credits_per_user,
                    exempt=exempt,
                )
                asked = await _asked(session, origin, user_id)
            if (refused := offer_refusal(view, asked=asked)) is not None:
                await _notice(interaction, refused, neutral=refused.startswith(ALREADY_REQUESTED))
                return
            embed, menu = menu_message(origin, view)
            await interaction.followup.send(embed=embed, view=menu, ephemeral=True)
            return
        sealed = await answer_is_sealed(runtime, origin)
        await interaction.response.send_modal(
            SupportModal(
                runtime=runtime,
                guild_id=self.guild_id,
                channel_id=self.channel_id,
                message_id=self.message_id,
                sealed=sealed,
            )
        )


__all__ = [
    "AnswerOrigin",
    "SupportEscalateButton",
    "SupportMenuSelect",
    "SupportModal",
    "SupportOptionModal",
    "answer_origin",
    "discord_channel",
    "menu_message",
    "post_to_support_channel",
    "requester_is_admin",
    "submit_answer_request",
]

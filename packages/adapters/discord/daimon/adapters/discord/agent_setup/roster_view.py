"""View 1 — who answers in this channel, and what else this server has.

The screen answers one question before it offers anything: whichever agent a
mention here would reach comes first and says so, and everything else on the
server follows in the roster's own order. A reader who only wanted to know who
they are talking to is done after the first row.

Members and admins get the same components. Being an admin changes the voice of
the routing sentence on the other screens, not what is on this one — hiding the
map from members made them ask an admin what the bot even does.

`roster_rows` and `build_roster_container` are pure; `RosterView` attaches the
callbacks and owns the I/O.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Mapping, Sequence
from typing import Final, Literal

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.budget import ROSTER_PAGE_SIZE
from daimon.adapters.discord.agent_setup.conversations import open_setup_conversation
from daimon.adapters.discord.agent_setup.hydrate import (
    load_answering_map_for,
    load_details_for,
)
from daimon.adapters.discord.agent_setup.navigation import STALE_PANEL_MESSAGE, PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import DaimonError
from daimon.core.roster import Page, RosterAgent, paginate
from daimon.core.scope import ChannelConfigRow, ConfigTier, DeploymentDefault, TenantConfigRow
from daimon.core.scope import answering_places as core_answering_places
from daimon.core.setup_conversations import EMPTY_ROSTER_COPY, setup_target_label

import discord

log = structlog.get_logger()

RosterStatus = Literal["answers_here", "server_default", "unrouted", "routed_elsewhere"]
"""The one routing fact the compact roster shows for an agent."""

_TIER_HINTS: Final[Mapping[ConfigTier, str]] = {
    "thread": "this thread",
    "channel": "channel setting",
    "tenant": "server default",
    "deployment": "deployment default",
}

_STATUS_LABELS: Final[Mapping[RosterStatus, str]] = {
    "routed_elsewhere": "answers in other channels",
    "unrouted": "not assigned",
}


@dataclasses.dataclass(frozen=True)
class RosterRow:
    """One agent and the routing status rendered directly below its name."""

    agent: RosterAgent
    status: RosterStatus
    attribution: str | None
    # Which tier makes this a default, for `server_default` rows only. The tier
    # is kept because "the server says so" and "the deployment says so" are
    # changed in different places.
    default_tier: ConfigTier | None = None


def _classify(
    agent: RosterAgent,
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
) -> tuple[RosterStatus, ConfigTier | None]:
    """Classify one agent against the cascade the panel already read.

    The tiers come from `answering_places`, never re-derived here: a panel that
    decides for itself where an agent answers will eventually disagree with the
    code that routes the mention.
    """
    if agent.answering_tier is not None:
        return "answers_here", None
    places = core_answering_places(agent.name, tenant=tenant, channels=channels, default=default)
    for place in places:
        if place.tier == "tenant" or place.tier == "deployment":
            return "server_default", place.tier
    if places:
        return "routed_elsewhere", None
    return "unrouted", None


def roster_rows(state: PanelState, *, attributions: Mapping[str, str]) -> tuple[RosterRow, ...]:
    """Classify every agent on the roster, in the order the roster already set.

    Pure — no I/O, no clock.
    """
    tenant_row, channel_rows = state.cascade_view
    rows: list[RosterRow] = []
    for agent in state.roster_agents:
        status, default_tier = _classify(
            agent, tenant=tenant_row, channels=channel_rows, default=state.deployment_default
        )
        rows.append(
            RosterRow(
                agent=agent,
                status=status,
                attribution=attributions.get(agent.ma_agent_id),
                default_tier=default_tier,
            )
        )
    return tuple(rows)


def _channel_title(state: PanelState) -> str:
    return f"#{state.channel_name}" if state.channel_name else "this channel"


def _page_subtext(page: Page[RosterRow]) -> str | None:
    if page.page_count <= 1:
        return None
    return f"Page {page.page + 1} of {page.page_count}"


def _thread_line(state: PanelState) -> str | None:
    """The one line that says this thread is not the parent channel.

    A setup thread names what is being configured; a handoff thread has no
    configuration target to name, only a responder that took the task on.
    """
    context = state.thread_context
    if context is None:
        return None
    responder = context.responder_name or "Daimon"
    if context.kind == "handoff":
        return f"-# In this thread **{responder}** answers (handed off)"
    if context.target_name is None:
        return f"-# In this thread **{responder}** answers"
    return f"-# In this thread **{responder}** answers while setting up **{context.target_name}**"


def _status_line(row: RosterRow) -> str:
    """The routing fact that most changes what the reader should do next."""
    if row.status == "answers_here":
        return "answers here"
    if row.status == "server_default":
        tier = row.default_tier or "tenant"
        return _TIER_HINTS[tier]
    return _STATUS_LABELS[row.status]


def _row_text(row: RosterRow) -> str:
    """The full name and one routing status on predictable separate lines."""
    return f"**{row.agent.name}**\n-# {_status_line(row)}"


def build_roster_container(
    state: PanelState, page: Page[RosterRow]
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Render one page of the roster. Pure — no I/O, no callbacks attached.

    Every row is a `Section` carrying its own Details button, so the accessory
    the reader clicks is the one attached to the line they read.
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(header(f"Agents in {_channel_title(state)}", subtext=_page_subtext(page)))
    thread_line = _thread_line(state)
    if thread_line is not None:
        container.add_item(discord.ui.TextDisplay(thread_line))
    container.add_item(hairline())
    if not page.items:
        container.add_item(discord.ui.TextDisplay(EMPTY_ROSTER_COPY))
        return container
    for row in page.items:
        details: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label="🔍 Details", style=discord.ButtonStyle.secondary
        )
        container.add_item(
            discord.ui.Section(discord.ui.TextDisplay(_row_text(row)), accessory=details)
        )
    return container


class RosterView(PanelViewBase):
    """The panel's root screen, and the one every Back returns to."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        page = paginate(
            roster_rows(state, attributions=state.attributions),
            page=state.roster_page,
            page_size=ROSTER_PAGE_SIZE,
        )
        # `paginate` clamps; write the clamped page back so a roster that lost
        # rows since the last click does not keep re-clamping the same stale one.
        state.roster_page = page.page
        container = build_roster_container(state, page)
        self._attach_details_callbacks(container, page)

        actions: discord.ui.ActionRow[RosterView] = discord.ui.ActionRow()
        setup_button: discord.ui.Button[RosterView] = discord.ui.Button(
            label=setup_target_label(state.answering.name if state.answering is not None else None),
            style=discord.ButtonStyle.primary,
        )
        setup_button.callback = self._on_setup  # type: ignore[method-assign]  # per-instance callback
        new_button: discord.ui.Button[RosterView] = discord.ui.Button(
            label="➕ New agent", style=discord.ButtonStyle.secondary
        )
        new_button.callback = self._on_new  # type: ignore[method-assign]  # per-instance callback
        actions.add_item(setup_button)
        actions.add_item(new_button)
        container.add_item(actions)  # pyright: ignore[reportArgumentType]  # ActionRow[Self] is the same runtime item

        navigation: discord.ui.ActionRow[RosterView] = discord.ui.ActionRow()
        routing_button: discord.ui.Button[RosterView] = discord.ui.Button(
            label="📍 Who answers where", style=discord.ButtonStyle.secondary
        )
        routing_button.callback = self._on_routing  # type: ignore[method-assign]  # per-instance callback
        navigation.add_item(routing_button)
        navigation.add_item(self.done_button())
        container.add_item(navigation)  # pyright: ignore[reportArgumentType]  # ActionRow[Self] is the same runtime item

        pager = self.page_row(page, on_previous=self._on_previous, on_next=self._on_next)
        if pager is not None:
            container.add_item(pager)  # pyright: ignore[reportArgumentType]  # ActionRow[Self] is the same runtime item

        self.add_item(container)

    def _attach_details_callbacks(
        self, container: discord.ui.Container[discord.ui.LayoutView], page: Page[RosterRow]
    ) -> None:
        """Bind each rendered row's accessory to the agent on that row.

        The builder stays pure by emitting the Sections in page order; this
        walks them in the same order, so the pairing is positional and a
        mismatch is a length error rather than a silently wrong target.
        """
        sections = [child for child in container.children if isinstance(child, discord.ui.Section)]
        for section, row in zip(sections, page.items, strict=True):
            accessory = section.accessory
            assert isinstance(accessory, discord.ui.Button), (
                "every roster row's accessory is its Details button"
            )
            accessory.callback = functools.partial(  # type: ignore[method-assign]  # per-instance callback
                self._on_details, agent=row.agent
            )

    async def _on_details(self, interaction: discord.Interaction, *, agent: RosterAgent) -> None:
        from daimon.adapters.discord.agent_setup.details_view import DetailsView

        if self._is_superseded():
            await interaction.response.send_message(STALE_PANEL_MESSAGE, ephemeral=True)
            return
        self.state.details_request_seq += 1
        request_seq = self.state.details_request_seq
        render_seq = self.state.render_seq
        source_render_seq = self._render_seq
        await interaction.response.defer()
        if source_render_seq != self.state.render_seq or self._is_superseded():
            return
        try:
            details = await load_details_for(self.runtime, state=self.state, agent=agent)
        except (DaimonError, anthropic.APIError, discord.HTTPException) as error:
            request_id = generate_request_id()
            log.exception(
                "agent_setup.details.failed", agent_name=agent.name, request_id=request_id
            )
            await interaction.followup.send(
                render_error(error, request_id=request_id), ephemeral=True
            )
            return
        # Multiple clicks on one still-live roster can complete out of order.
        # Do not let an older read replace a newer selection or another screen.
        if request_seq != self.state.details_request_seq or render_seq != self.state.render_seq:
            return
        self.state.select_agent(agent)
        self.state.details = details
        await self.swap_to(
            interaction,
            DetailsView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                details=details,
                agent=agent,
            ),
        )

    async def _on_setup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_setup_conversation(
            interaction, runtime=self.runtime, state=self.state, target=self.state.answering
        )

    async def _on_new(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.agent_setup.new_agent import NewAgentModal

        await interaction.response.send_modal(
            NewAgentModal(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id)
        )

    async def _on_routing(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.agent_setup.routing_view import build_routing_view

        await interaction.response.defer()
        try:
            # Read the cascade fresh on every open: the map is a set of claims
            # about where mentions land, and a stale one is a wrong one.
            self.state.answering_map = await load_answering_map_for(self.runtime, state=self.state)
            routing = await build_routing_view(
                interaction,
                runtime=self.runtime,
                state=self.state,
                allowed_user_id=self.allowed_user_id,
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException) as error:
            request_id = generate_request_id()
            log.exception("agent_setup.routing.failed", request_id=request_id)
            await interaction.followup.send(
                render_error(error, request_id=request_id), ephemeral=True
            )
            return
        await self.swap_to(interaction, routing)

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        self.state.roster_page -= 1
        await self._rerender(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.state.roster_page += 1
        await self._rerender(interaction)

    async def _rerender(self, interaction: discord.Interaction) -> None:
        """Rebuild this screen from state — pagination needs no refetch."""
        await self.swap_to(
            interaction,
            RosterView(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id),
        )

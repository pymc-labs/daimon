"""Who answers where — the whole cascade, laid out instead of inferred.

The panel's other two screens answer "who answers *here*". This one shows every
tier at once: each channel that names its own agent, the server default, and the
deployment fall-through that a server default removes from the cascade entirely.
The environment each channel runs in resolves over the same tiers on its own,
so it gets its own block. Setup conversations sit in their own bounded list,
because a live setup thread is not a routing rule and reading it as one is
exactly the mistake this screen exists to prevent.

The precedence itself is `daimon.core.routing_facts`' to state, not this
module's; adapters render the cascade, they never re-derive it.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import datetime

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.budget import LAYOUT_TEXT_BUDGET, ROUTING_PAGE_SIZE
from daimon.adapters.discord.agent_setup.channel_admins_view import (
    CHANNEL_ADMINS_LABEL,
    ChannelAdminsView,
    load_grants,
)
from daimon.adapters.discord.agent_setup.channel_environment import (
    REFUSED_MESSAGE,
    audit_environment_pick,
    build_environment_select,
    load_environment_picker,
    load_picker_subject,
    may_pick_environment,
    panel_tenant_id,
    save_environment_choice,
)
from daimon.adapters.discord.agent_setup.channel_skills_view import (
    CHANNEL_SKILLS_LABEL,
    ChannelSkillsView,
    load_channel_skills,
)
from daimon.adapters.discord.agent_setup.isolation_view import (
    ISOLATION_LABEL,
    IsolationView,
    load_isolation_status,
)
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.operator_tokens_view import (
    OPERATOR_TOKENS_LABEL,
    OperatorTokensView,
    load_operator_tokens,
)
from daimon.adapters.discord.agent_setup.scope_default import resolve_account_display
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import refuse_if_not_admin
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.answering_map import AnsweringMap
from daimon.core.channel_admins import fit_lines
from daimon.core.channel_environments import EnvironmentPicker
from daimon.core.errors import DaimonError
from daimon.core.roster import Page, paginate
from daimon.core.routing_facts import PRECEDENCE_LINE, build_routing_request
from daimon.core.setup_conversations import setup_thread_name
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

import discord

log = structlog.get_logger()

BACK_LABEL = "◀ Back"
SERVER_DEFAULT_LABEL = "Server default"
DEPLOYMENT_NOT_IN_EFFECT = "not in effect while a server default is set"
MAX_SETUP_CONVERSATION_LINKS = 5
MAX_ENVIRONMENT_LINES = 10
_MORE_LINE_RESERVE = len("\n-# and 9999 more")


@dataclasses.dataclass(frozen=True)
class RoutingLine:
    """One rendered row of the cascade: where, who, and who decided it.

    ``audit_line`` is None when neither an actor nor a timestamp was recorded —
    saying "set (no audit)" would dress an absence up as a fact.
    """

    channel_label: str
    agent_name: str
    audit_line: str | None


async def _resolve_audit(
    session: AsyncSession, *, account_id: uuid.UUID | None, set_at: datetime | None
) -> str | None:
    """Build the audit sentence from whichever halves were recorded."""
    handle = (
        await resolve_account_display(session, account_id=account_id)
        if account_id is not None
        else None
    )
    stamp = f"<t:{int(set_at.timestamp())}:d>" if set_at is not None else None
    if handle is not None and stamp is not None:
        return f"set by {handle} on {stamp}"
    if handle is not None:
        return f"set by {handle}"
    if stamp is not None:
        return f"set on {stamp}"
    return None


def _channel_label(guild: discord.Guild | None, channel_id: str) -> str:
    """The channel's name from the cache, or its raw id when the cache misses."""
    channel = guild.get_channel(int(channel_id)) if guild is not None else None
    return f"#{channel.name}" if channel is not None else f"#{channel_id}"


async def load_routing_lines(
    session: AsyncSession, guild: discord.Guild | None, answering_map: AnsweringMap
) -> tuple[list[RoutingLine], RoutingLine | None]:
    """Resolve every channel override and the server default into rendered lines.

    Returns the channel lines in the map's own channel_id order and the server
    default separately, because the server default is a different tier and is
    never paged away with the channels.
    """
    channel_lines = [
        RoutingLine(
            channel_label=_channel_label(guild, override.channel_id),
            agent_name=override.agent_name,
            audit_line=await _resolve_audit(
                session, account_id=override.set_by_account_id, set_at=override.set_at
            ),
        )
        for override in answering_map.channel_overrides
    ]
    tenant_default = answering_map.tenant_default
    server_default = (
        RoutingLine(
            channel_label=SERVER_DEFAULT_LABEL,
            agent_name=tenant_default.agent_name,
            audit_line=await _resolve_audit(
                session,
                account_id=tenant_default.set_by_account_id,
                set_at=tenant_default.set_at,
            ),
        )
        if tenant_default is not None
        else None
    )
    return channel_lines, server_default


def routing_lines_from_map(
    answering_map: AnsweringMap,
) -> tuple[list[RoutingLine], RoutingLine | None]:
    """The cascade with nothing looked up: channel mentions, no audit.

    ``load_routing_lines`` is the full-fidelity version — it spends one session
    resolving who set each row and names the channels from the guild cache. This
    is what a caller holding neither gets, and it is still honest: Discord
    expands ``<#id>`` into the channel's name in the client, and an audit line
    nobody resolved is simply absent rather than guessed at.

    Pure — no I/O.
    """
    channel_lines = [
        RoutingLine(
            channel_label=f"<#{override.channel_id}>",
            agent_name=override.agent_name,
            audit_line=None,
        )
        for override in answering_map.channel_overrides
    ]
    tenant_default = answering_map.tenant_default
    server_default = (
        RoutingLine(
            channel_label=SERVER_DEFAULT_LABEL,
            agent_name=tenant_default.agent_name,
            audit_line=None,
        )
        if tenant_default is not None
        else None
    )
    return channel_lines, server_default


def setup_conversation_links(answering_map: AnsweringMap, *, guild_id: int) -> list[str]:
    """Jump links to the install's live setup conversations, newest first."""
    return [
        f"[{setup_thread_name(thread.target_name)}]"
        f"(https://discord.com/channels/{guild_id}/{thread.thread_id})"
        for thread in answering_map.setup_threads
    ]


def routing_request_agent(state: PanelState, answering_map: AnsweringMap) -> str | None:
    """Name the agent the example request should be about.

    An agent nothing routes to is the one a reader most likely wants routed, so
    it wins; failing that the agent answering here is at least a name the reader
    recognises. The deployment default counts as routed only while a server
    default is not swallowing the fall-through.
    """
    routed = {override.agent_name for override in answering_map.channel_overrides}
    if answering_map.tenant_default is not None:
        routed.add(answering_map.tenant_default.agent_name)
    if (
        answering_map.deployment_default is not None
        and not answering_map.tenant_consumes_fallthrough
    ):
        routed.add(answering_map.deployment_default)
    unrouted = next((row.name for row in state.roster_agents if row.name not in routed), None)
    if unrouted is not None:
        return unrouted
    return state.answering.name if state.answering is not None else None


def build_routing_sentence(state: PanelState, answering_map: AnsweringMap) -> str:
    """The precedence rule plus, when there is a name to use, the exact request.

    The voice is the only thing the reader's role changes here: both roles see
    the same map and the same rule, and only an admin can act on it alone.
    """
    agent_name = routing_request_agent(state, answering_map)
    if agent_name is None or state.channel_name is None:
        return PRECEDENCE_LINE
    lead = "Tell Daimon" if state.is_admin else "An admin can tell Daimon"
    request = build_routing_request(agent_name=agent_name, channel_label=f"#{state.channel_name}")
    return f"{PRECEDENCE_LINE} {lead}: {request}"


def _channel_block(page: Page[RoutingLine]) -> str:
    if not page.items:
        return "-# no channel picks its own agent yet"
    rendered: list[str] = []
    for line in page.items:
        rendered.append(f"{line.channel_label} → **{line.agent_name}**")
        if line.audit_line is not None:
            rendered.append(f"-# {line.audit_line}")
    return "\n".join(rendered)


def _defaults_block(
    server_default: RoutingLine | None,
    *,
    deployment_default: str | None,
    deployment_in_effect: bool,
) -> str:
    lines: list[str] = []
    if server_default is not None:
        lines.append(f"{SERVER_DEFAULT_LABEL} → **{server_default.agent_name}**")
        if server_default.audit_line is not None:
            lines.append(f"-# {server_default.audit_line}")
    else:
        lines.append("-# no server default")
    if deployment_default is not None:
        lines.append(f"Deployment default → **{deployment_default}**")
        if not deployment_in_effect:
            lines.append(f"-# {DEPLOYMENT_NOT_IN_EFFECT}")
    return "\n".join(lines)


def build_environments_block(
    answering_map: AnsweringMap, *, max_chars: int = LAYOUT_TEXT_BUDGET
) -> str | None:
    """Each channel's own environment, then the server and deployment defaults. Pure.

    Channel lines are cut to fit `max_chars`; None when even the defaults do not.
    """
    heading = "**Environments**"
    tenant, deployment = answering_map.tenant_environment, answering_map.deployment_environment
    defaults = [f"{SERVER_DEFAULT_LABEL} → **{tenant}**" if tenant else "-# no server default"]
    if deployment is not None:
        defaults.append(f"Deployment default → **{deployment}**")
        if tenant is not None:
            defaults.append(f"-# {DEPLOYMENT_NOT_IN_EFFECT}")
    fixed = len("\n".join([heading, *defaults]))
    rows = answering_map.channel_environments
    shown = fit_lines(
        (
            f"<#{row.channel_id}> → **{row.environment_name}**"
            for row in rows[:MAX_ENVIRONMENT_LINES]
        ),
        max_chars=max_chars - fixed - 1 - _MORE_LINE_RESERVE,
    )
    if len(shown) < len(rows):
        shown.append(f"-# and {len(rows) - len(shown)} more")
    body = shown or ["-# no channel picks its own environment yet"]
    text = "\n".join([heading, *body, *defaults])
    return text if len(text) <= max_chars else None


def _conversations_block(conversations: Sequence[str]) -> str:
    if not conversations:
        return "**Setup conversations**\n-# none open"
    shown = list(conversations[:MAX_SETUP_CONVERSATION_LINKS])
    remainder = len(conversations) - len(shown)
    body = "\n".join(shown)
    if remainder > 0:
        body = f"{body}\n-# and {remainder} more"
    return f"**Setup conversations**\n{body}"


def build_routing_container(
    page: Page[RoutingLine],
    *,
    server_default: RoutingLine | None,
    deployment_default: str | None,
    deployment_in_effect: bool,
    conversations: Sequence[str],
    sentence: str,
    environment_map: AnsweringMap | None = None,
    environment_select: discord.ui.Select[discord.ui.LayoutView] | None = None,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Fold one page of the cascade into the panel card. Pure — no I/O, no clock.

    ``environment_map`` adds the environments block, cut to the text the rest
    of the card leaves; ``environment_select`` sits right under it.
    """
    heading = header("Who answers where")
    channels = discord.ui.TextDisplay[discord.ui.LayoutView](_channel_block(page))
    defaults = discord.ui.TextDisplay[discord.ui.LayoutView](
        _defaults_block(
            server_default,
            deployment_default=deployment_default,
            deployment_in_effect=deployment_in_effect,
        )
    )
    setup = discord.ui.TextDisplay[discord.ui.LayoutView](_conversations_block(conversations))
    rule = discord.ui.TextDisplay[discord.ui.LayoutView](f"-# {sentence}")
    room = LAYOUT_TEXT_BUDGET - sum(
        len(item.content) for item in (heading, channels, defaults, setup, rule)
    )
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(heading)
    container.add_item(channels)
    container.add_item(defaults)
    environments = (
        build_environments_block(environment_map, max_chars=room)
        if environment_map is not None
        else None
    )
    if environments is not None or environment_select is not None:
        container.add_item(hairline())
    if environments is not None:
        container.add_item(discord.ui.TextDisplay(environments))
    if environment_select is not None:
        select_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        select_row.add_item(environment_select)
        container.add_item(select_row)
    container.add_item(hairline())
    container.add_item(setup)
    container.add_item(hairline())
    container.add_item(rule)
    return container


class RoutingView(PanelViewBase):
    """The Who answers where screen: paged, no setup button.

    Setup belongs to the roster and to Details, where a target is selected;
    offering it here would suggest this screen is the place a routing change
    gets made, and it is not. Server admins also get Channel admins, which
    edits who runs a channel rather than who answers in it, and Isolation,
    which keeps this channel's own agents inside it. They and this channel's
    admins get an environment select for this channel, which changes where its
    turns run rather than who answers.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        lines: Sequence[RoutingLine] | None = None,
        server_default: RoutingLine | None = None,
        environment_picker: EnvironmentPicker | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        answering_map = state.answering_map
        assert answering_map is not None, "RoutingView needs a loaded AnsweringMap on the state"
        if lines is None:
            lines, server_default = routing_lines_from_map(answering_map)
        self.lines = list(lines)
        self.server_default = server_default
        self.environment_picker = environment_picker
        self.environment_select = (
            build_environment_select(environment_picker, channel_name=state.channel_name)
            if environment_picker is not None
            else None
        )
        if self.environment_select is not None:
            self.environment_select.callback = self._on_environment  # type: ignore[method-assign]  # per-instance callback

        page = paginate(self.lines, page=state.routing_page, page_size=ROUTING_PAGE_SIZE)
        state.routing_page = page.page
        container = build_routing_container(
            page,
            server_default=server_default,
            deployment_default=answering_map.deployment_default,
            deployment_in_effect=not answering_map.tenant_consumes_fallthrough,
            conversations=setup_conversation_links(answering_map, guild_id=state.guild_id),
            sentence=build_routing_sentence(state, answering_map),
            environment_map=answering_map,
            environment_select=self.environment_select,
        )

        nav_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=BACK_LABEL, style=discord.ButtonStyle.secondary
        )
        back_button.callback = self._on_back  # type: ignore[method-assign]  # per-instance callback
        nav_row.add_item(back_button)
        if state.is_admin:
            admins_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label=CHANNEL_ADMINS_LABEL, style=discord.ButtonStyle.secondary
            )
            admins_button.callback = self._on_channel_admins  # type: ignore[method-assign]  # per-instance callback
            nav_row.add_item(admins_button)
            if state.channel_id:
                isolation_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                    label=ISOLATION_LABEL, style=discord.ButtonStyle.secondary
                )
                isolation_button.callback = self._on_isolation  # type: ignore[method-assign]  # per-instance callback
                nav_row.add_item(isolation_button)
            tokens_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label=OPERATOR_TOKENS_LABEL, style=discord.ButtonStyle.secondary
            )
            tokens_button.callback = self._on_operator_tokens  # type: ignore[method-assign]  # per-instance callback
            nav_row.add_item(tokens_button)
        nav_row.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(nav_row)
        if state.is_admin and state.channel_id:
            skills_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            skills_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label=CHANNEL_SKILLS_LABEL, style=discord.ButtonStyle.secondary
            )
            skills_button.callback = self._on_channel_skills  # type: ignore[method-assign]  # per-instance callback
            skills_row.add_item(skills_button)
            container.add_item(skills_row)

        pager = self.page_row(page, on_previous=self._on_previous, on_next=self._on_next)
        if pager is not None:
            container.add_item(pager)  # pyright: ignore[reportArgumentType]  # ActionRow[Self] is the same runtime item

        self.add_item(container)

    def _repaged(self) -> RoutingView:
        return RoutingView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            lines=self.lines,
            server_default=self.server_default,
            environment_picker=self.environment_picker,
        )

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        self.state.routing_page -= 1
        await self.swap_to(interaction, self._repaged())

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.state.routing_page += 1
        await self.swap_to(interaction, self._repaged())

    async def _on_channel_admins(self, interaction: discord.Interaction) -> None:
        """Open the channel admins screen; Manage Server is re-checked live."""
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            return
        await interaction.response.defer()
        grants = await load_grants(self.runtime, state=self.state)
        await self.swap_to(
            interaction,
            ChannelAdminsView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                grants=grants,
            ),
        )

    async def _on_environment(self, interaction: discord.Interaction) -> None:
        """Save this channel's environment; who may is re-checked live first."""
        select = self.environment_select
        assert select is not None, "only the environment select has this callback"
        subject = await load_picker_subject(
            interaction, runtime=self.runtime, state=self.state, live=True
        )
        user_id = str(interaction.user.id)
        if not await may_pick_environment(subject, runtime=self.runtime, state=self.state):
            await interaction.response.send_message(REFUSED_MESSAGE, ephemeral=True)
            await audit_environment_pick(
                runtime=self.runtime,
                state=self.state,
                user_id=user_id,
                outcome="denied",
                reason="needs_admin",
            )
            return
        await interaction.response.defer()
        # Lazy import: hydrate imports the view modules to type its own returns.
        from daimon.adapters.discord.agent_setup.hydrate import load_answering_map_for

        try:
            note = await save_environment_choice(
                runtime=self.runtime,
                state=self.state,
                subject=subject,
                user_id=user_id,
                value=select.values[0],
            )
            self.state.answering_map = await load_answering_map_for(self.runtime, state=self.state)
            routing = await build_routing_view(
                interaction,
                runtime=self.runtime,
                state=self.state,
                allowed_user_id=self.allowed_user_id,
            )
        except (DaimonError, anthropic.APIError, discord.HTTPException, SQLAlchemyError) as error:
            request_id = generate_request_id()
            log.exception(
                "agent_setup.channel_environment.failed",
                request_id=request_id,
                tenant_id=str(panel_tenant_id(self.state)),
                channel_id=str(self.state.channel_id),
                actor_account_id=str(self.state.account_id),
            )
            await interaction.followup.send(
                render_error(error, request_id=request_id), ephemeral=True
            )
            return
        await self.swap_to(interaction, routing)
        await interaction.followup.send(
            note, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _on_isolation(self, interaction: discord.Interaction) -> None:
        """Open this channel's isolation screen; Manage Server is re-checked live."""
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            return
        await interaction.response.defer()
        status = await load_isolation_status(self.runtime, state=self.state)
        await self.swap_to(
            interaction,
            IsolationView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                status=status,
            ),
        )

    async def _on_channel_skills(self, interaction: discord.Interaction) -> None:
        """Open this channel's skills screen; Manage Server is re-checked live."""
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            return
        await interaction.response.defer()
        rows = await load_channel_skills(self.runtime, state=self.state)
        await self.swap_to(
            interaction,
            ChannelSkillsView(
                self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id, rows=rows
            ),
        )

    async def _on_operator_tokens(self, interaction: discord.Interaction) -> None:
        """Open the operator tokens screen; Manage Server is re-checked live."""
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]  # only reads user/guild/response
            return
        await interaction.response.defer()
        rows = await load_operator_tokens(self.runtime, state=self.state)
        await self.swap_to(
            interaction,
            OperatorTokensView(
                self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id, rows=rows
            ),
        )

    async def _on_back(self, interaction: discord.Interaction) -> None:
        """Return to the roster with its page and selection exactly as they were."""
        # Lazy import: the roster screen opens this one, so a top-level import
        # here would close the cycle.
        from daimon.adapters.discord.agent_setup.roster_view import RosterView

        await self.swap_to(
            interaction,
            RosterView(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id),
        )


async def build_routing_view(
    interaction: discord.Interaction,
    *,
    runtime: DiscordRuntime,
    state: PanelState,
    allowed_user_id: int,
) -> RoutingView:
    """Read the cascade for ``state``'s install and return the screen that renders it.

    The caller swaps to the result; keeping the read out of ``__init__`` is what
    lets paging and Back rebuild the screen without touching the database again.
    """
    if state.answering_map is None:
        # Lazy import: hydrate is the shell that owns every panel read, and it
        # imports the view modules to type its own returns.
        from daimon.adapters.discord.agent_setup.hydrate import load_answering_map_for

        state.answering_map = await load_answering_map_for(runtime, state=state)
    async with runtime.sessionmaker() as session:
        lines, server_default = await load_routing_lines(
            session, interaction.guild, state.answering_map
        )
    return RoutingView(
        state,
        runtime=runtime,
        allowed_user_id=allowed_user_id,
        lines=lines,
        server_default=server_default,
        environment_picker=await load_environment_picker(interaction, runtime=runtime, state=state),
    )

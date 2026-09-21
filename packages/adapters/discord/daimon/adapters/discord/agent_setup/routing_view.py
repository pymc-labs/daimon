"""Who answers where — the whole cascade, laid out instead of inferred.

The panel's other two screens answer "who answers *here*". This one shows every
tier at once: each channel that names its own agent, the server default, and the
deployment fall-through that a server default removes from the cascade entirely.
Setup conversations sit in their own bounded list, because a live setup thread
is not a routing rule and reading it as one is exactly the mistake this screen
exists to prevent.

The precedence itself is `daimon.core.routing_facts`' to state, not this
module's; adapters render the cascade, they never re-derive it.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import datetime

import structlog
from daimon.adapters.discord.agent_setup.budget import ROUTING_PAGE_SIZE
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.scope_default import resolve_account_display
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.answering_map import AnsweringMap
from daimon.core.roster import Page, paginate
from daimon.core.routing_facts import PRECEDENCE_LINE, build_routing_request
from daimon.core.setup_conversations import setup_thread_name
from sqlalchemy.ext.asyncio import AsyncSession

import discord

log = structlog.get_logger()

BACK_LABEL = "◀ Back"
SERVER_DEFAULT_LABEL = "Server default"
DEPLOYMENT_NOT_IN_EFFECT = "not in effect while a server default is set"
MAX_SETUP_CONVERSATION_LINKS = 5


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
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Fold one page of the cascade into the panel card. Pure — no I/O, no clock."""
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(header("Who answers where"))
    container.add_item(discord.ui.TextDisplay(_channel_block(page)))
    container.add_item(
        discord.ui.TextDisplay(
            _defaults_block(
                server_default,
                deployment_default=deployment_default,
                deployment_in_effect=deployment_in_effect,
            )
        )
    )
    container.add_item(hairline())
    container.add_item(discord.ui.TextDisplay(_conversations_block(conversations)))
    container.add_item(hairline())
    container.add_item(discord.ui.TextDisplay(f"-# {sentence}"))
    return container


class RoutingView(PanelViewBase):
    """The Who answers where screen: read-only, paged, no setup button.

    Setup belongs to the roster and to Details, where a target is selected;
    offering it here would suggest this screen is the place a routing change
    gets made, and it is not.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        lines: Sequence[RoutingLine] | None = None,
        server_default: RoutingLine | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        answering_map = state.answering_map
        assert answering_map is not None, "RoutingView needs a loaded AnsweringMap on the state"
        if lines is None:
            lines, server_default = routing_lines_from_map(answering_map)
        self.lines = list(lines)
        self.server_default = server_default

        page = paginate(self.lines, page=state.routing_page, page_size=ROUTING_PAGE_SIZE)
        state.routing_page = page.page
        container = build_routing_container(
            page,
            server_default=server_default,
            deployment_default=answering_map.deployment_default,
            deployment_in_effect=not answering_map.tenant_consumes_fallthrough,
            conversations=setup_conversation_links(answering_map, guild_id=state.guild_id),
            sentence=build_routing_sentence(state, answering_map),
        )

        nav_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=BACK_LABEL, style=discord.ButtonStyle.secondary
        )
        back_button.callback = self._on_back  # type: ignore[method-assign]  # per-instance callback
        nav_row.add_item(back_button)
        nav_row.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(nav_row)

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
        )

    async def _on_previous(self, interaction: discord.Interaction) -> None:
        self.state.routing_page -= 1
        await self.swap_to(interaction, self._repaged())

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.state.routing_page += 1
        await self.swap_to(interaction, self._repaged())

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
    )

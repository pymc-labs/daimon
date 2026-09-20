"""Details — one agent's whole readable state on the panel's one message.

The card answers "what can I use, and how do I change it?" without teaching
anybody the configuration structure: what the agent is for, where a mention
actually reaches it, what it is wired to, and the two ways to act on it — talk
to Daimon about it, or drive it from a coding tool.

``build_details_container`` is pure: it folds an `AgentDetails` into a
container and never reads a clock, a session or a credential. ``DetailsView``
is the shell that attaches the callbacks. Configuration lists expose metadata
only; key values are never part of the rendered model.
"""

from __future__ import annotations

import functools
from urllib.parse import quote

import structlog
from daimon.adapters.discord.agent_setup.budget import LAYOUT_TEXT_BUDGET
from daimon.adapters.discord.agent_setup.conversations import open_setup_conversation
from daimon.adapters.discord.agent_setup.mcp_access import send_coding_tools_access
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.layout import hairline
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_detail_lists import (
    DETAIL_LIST_COLLAPSED_COUNT,
    DetailListName,
    format_detail_lists,
)
from daimon.core.agent_details import AgentDetails, RepoBinding
from daimon.core.github_repo_auth import RepoAccess, normalize_owner_repo
from daimon.core.scope import AnsweringPlace
from daimon.core.setup_conversations import (
    setup_target_label,
    shared_keys_sentence,
)

import discord

log = structlog.get_logger()

CODING_TOOLS_LABEL = "🧰 Use from your coding tools"
BACK_LABEL = "◀ Back"
SHOW_MORE_LABEL = "Show more"
SHOW_FEWER_LABEL = "Show fewer"
_LIST_TEXT_RESERVE = 256
_PURPOSE_MAX_CHARS = 800
_ROUTING_MAX_CHARS = 1000
_DETAIL_LIST_NAMES: tuple[DetailListName, ...] = ("keys", "skills", "connections")


def coding_tools_refusal(agent_name: str) -> str:
    """What a member sees instead of a token, naming the permission and the way round it."""
    return (
        f"Minting an access token for {agent_name} needs Manage Server. "
        f"Ask an admin to open Details and use this button."
    )


def _answers_line(places: tuple[AnsweringPlace, ...]) -> str:
    """Where mentions reach this agent, one place per line when there are several."""
    parts: list[str] = []
    for place in places:
        if place.tier == "channel" and place.channel_id is not None:
            parts.append(f"<#{place.channel_id}>")
        elif place.tier == "tenant":
            parts.append("the server default")
        else:
            parts.append("the deployment default")
    shown: list[str] = []
    for part in parts:
        candidate = "\n".join((*shown, part))
        omitted = len(parts) - len(shown) - 1
        suffix = f"\n+{omitted} more" if omitted > 0 else ""
        if len(candidate) + len(suffix) > _ROUTING_MAX_CHARS:
            break
        shown.append(part)
    omitted = len(parts) - len(shown)
    if omitted > 0:
        shown.append(f"+{omitted} more")
    body = "\n".join(shown)
    separator = " " if len(shown) == 1 else "\n"
    return f"**Answers in:**{separator}{body}"


def _purpose_text(purpose: str) -> str:
    """Bound optional prose while making its omission visible."""
    if len(purpose) <= _PURPOSE_MAX_CHARS:
        return purpose
    return f"{purpose[: _PURPOSE_MAX_CHARS - 1]}…"


def _last_checked(access: RepoAccess) -> str:
    return (
        f"\n-# Last checked <t:{int(access.checked_at.timestamp())}:R>"
        if access.checked_at is not None
        else ""
    )


def _repo_access_line(access: RepoAccess) -> str:
    """Say exactly what was recorded — never read a URL or an App as proof."""
    if access.kind == "needs_attention":
        return f"⚠️ needs attention — {access.corrective or 'nothing would authorize a clone.'}"
    if access.kind == "not_checked":
        return "not checked yet"
    if access.credential == "per_agent_token":
        return f"via token{_last_checked(access)}"
    if access.credential == "deployment_public":
        return f"public repo{_last_checked(access)}"
    if access.credential == "github_app":
        return f"via GitHub App{_last_checked(access)}"
    return f"access recorded{_last_checked(access)}"


def _repo_texts(repo: RepoBinding | None) -> tuple[str, ...]:
    if repo is None:
        return ()
    slug = normalize_owner_repo(repo.repo_url)
    return (
        f"**Repository:** [{slug}](https://github.com/{slug})",
        f"**Branch:** `{repo.default_branch}`",
        f"**Access:** {_repo_access_line(repo.access)}",
    )


def _detail_items(details: AgentDetails) -> dict[DetailListName, tuple[str, ...]]:
    items: dict[DetailListName, tuple[str, ...]] = {}
    if details.skills:
        items["skills"] = tuple(skill.title or skill.skill_id for skill in details.skills)
    if details.mcp_servers:
        items["connections"] = tuple(
            f"[{_escape_link_label(server.name)}]({quote(server.url, safe=':/?&=#%+-._~')})"
            for server in details.mcp_servers
        )
    if details.keys:
        items["keys"] = tuple(f"`{entry.name}`" for entry in details.keys)
    return items


def _escape_link_label(text: str) -> str:
    """Keep an external connection name inside its Markdown link label."""
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _list_heading(name: DetailListName) -> str:
    return {"skills": "Skills", "connections": "Connections", "keys": "Keys"}[name]


def _list_note(details: AgentDetails, name: DetailListName) -> str | None:
    if name == "skills" and details.skills_listing_truncated:
        return "-# Some skill names may be missing."
    if name == "keys":
        return f"-# {shared_keys_sentence(details.name)}"
    return None


def _list_item(
    *,
    name: DetailListName,
    body: str,
    count: int,
    expanded: DetailListName | None,
    note: str | None,
) -> discord.ui.Item[discord.ui.LayoutView]:
    text_content = f"**{_list_heading(name)}**\n{body}"
    if note is not None:
        text_content = f"{text_content}\n{note}"
    text: discord.ui.TextDisplay[discord.ui.LayoutView] = discord.ui.TextDisplay(text_content)
    if count <= DETAIL_LIST_COLLAPSED_COUNT:
        return text
    action = SHOW_FEWER_LABEL if expanded == name else SHOW_MORE_LABEL
    toggle: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
        label=action,
        style=discord.ButtonStyle.secondary,
    )
    return discord.ui.Section(text, accessory=toggle)


def build_details_container(
    state: PanelState,
    details: AgentDetails,
    *,
    expanded_detail: DetailListName | None,
    is_admin: bool,
    attribution: str | None,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Fold one agent's details into the panel card. Pure — no I/O, no clock.

    ``state`` and ``is_admin`` are carried for symmetry with the other two
    screens' builders; the role already reached this card through
    ``details.unrouted_note``, which core wrote in the reader's own voice.
    """
    del state, is_admin
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    fixed_texts = [f"## {details.name}"]
    if details.purpose:
        fixed_texts[0] = f"{fixed_texts[0]}\n{_purpose_text(details.purpose)}"
    if details.answers_in:
        routing_text = _answers_line(details.answers_in)
    elif details.unrouted_note is not None:
        routing_text = details.unrouted_note
    else:
        routing_text = ""
    if routing_text:
        fixed_texts.append(routing_text)
    fixed_texts.append(f"**Model:** {details.model_display_name}")
    fixed_texts.extend(_repo_texts(details.repo))
    if details.skills_listing_truncated and not details.skills:
        fixed_texts.append("-# Some skill names may be missing.")
    del attribution

    items = _detail_items(details)
    list_notes: dict[DetailListName, str | None] = {
        name: _list_note(details, name) for name in items
    }
    reserved = sum(map(len, fixed_texts)) + sum(
        len(f"**{_list_heading(name)}**\n") + len(note or "") + 1
        for name, note in list_notes.items()
    )
    list_bodies = format_detail_lists(
        items,
        expanded=expanded_detail,
        max_chars=LAYOUT_TEXT_BUDGET - reserved - _LIST_TEXT_RESERVE,
    )

    container.add_item(discord.ui.TextDisplay(fixed_texts[0]))
    if routing_text:
        container.add_item(discord.ui.TextDisplay(routing_text))
    setup_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
    setup_row.add_item(
        discord.ui.Button(label=setup_target_label(details.name), style=discord.ButtonStyle.primary)
    )
    container.add_item(setup_row)
    container.add_item(hairline())
    for text in fixed_texts[1 + bool(routing_text) :]:
        container.add_item(discord.ui.TextDisplay(text))
    for name, body in list_bodies.items():
        container.add_item(
            _list_item(
                name=name,
                body=body,
                count=len(items[name]),
                expanded=expanded_detail,
                note=list_notes[name],
            )
        )
    container.add_item(hairline())
    return container


def _toggle_buttons(
    container: discord.ui.Container[discord.ui.LayoutView],
) -> dict[DetailListName, discord.ui.Button[discord.ui.LayoutView]]:
    """Find the list accessories the pure builder left unwired."""
    found: dict[DetailListName, discord.ui.Button[discord.ui.LayoutView]] = {}
    for child in container.children:
        if isinstance(child, discord.ui.Section):
            accessory = child.accessory
            if not isinstance(accessory, discord.ui.Button) or accessory.label is None:
                continue
            text = next(
                (
                    item.content
                    for item in child.children
                    if isinstance(item, discord.ui.TextDisplay)
                ),
                "",
            )
            for name in _DETAIL_LIST_NAMES:
                if text.startswith(f"**{_list_heading(name)}**"):
                    found[name] = accessory
    return found


class DetailsView(PanelViewBase):
    """The Details screen: one agent, two actions, and the way back.

    Rebuilt from ``state`` on every render, so expanding a configuration list or coming
    back from a setup conversation costs no refetch.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        details = state.details
        assert details is not None, "DetailsView needs a loaded AgentDetails on the panel state"
        self.details = details
        container = build_details_container(
            state,
            details,
            expanded_detail=state.expanded_detail,
            is_admin=state.is_admin,
            attribution=None,
        )
        for name, toggle in _toggle_buttons(container).items():
            toggle.callback = functools.partial(  # type: ignore[method-assign]  # per-instance callback
                self._on_toggle_detail, name=name
            )

        setup_button = next(
            child
            for child in container.walk_children()
            if isinstance(child, discord.ui.Button)
            and child.label == setup_target_label(details.name)
        )
        setup_button.callback = self._on_setup  # type: ignore[method-assign]  # per-instance callback

        action_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        coding_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=CODING_TOOLS_LABEL, style=discord.ButtonStyle.secondary
        )
        coding_button.callback = self._on_coding_tools  # type: ignore[method-assign]  # per-instance callback
        action_row.add_item(coding_button)
        container.add_item(action_row)

        nav_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=BACK_LABEL, style=discord.ButtonStyle.secondary
        )
        back_button.callback = self._on_back  # type: ignore[method-assign]  # per-instance callback
        nav_row.add_item(back_button)
        nav_row.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(nav_row)

        self.add_item(container)

    async def _on_setup(self, interaction: discord.Interaction) -> None:
        """Open a setup conversation about the agent this card is describing."""
        log.info("agent_setup.details.setup.click", agent_name=self.details.name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_setup_conversation(
            interaction,
            runtime=self.runtime,
            state=self.state,
            target=self.state.selected_agent,
        )

    async def _on_coding_tools(self, interaction: discord.Interaction) -> None:
        """Mint a coding-tool token, but only for a caller who is an admin right now.

        The live re-check comes before anything else: the view's own
        ``is_admin`` is a snapshot from panel-open, and a caller demoted since
        then must reach no token material.
        """
        log.info("agent_setup.details.coding_tools.click", agent_name=self.details.name)
        if not is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]  # reads only user and guild
            await interaction.response.send_message(
                coding_tools_refusal(self.details.name), ephemeral=True
            )
            return
        await send_coding_tools_access(
            interaction,
            runtime=self.runtime,
            state=self.state,
            allowed_user_id=self.allowed_user_id,
        )

    async def _on_toggle_detail(
        self, interaction: discord.Interaction, *, name: DetailListName
    ) -> None:
        """Expand one detail list at a time, or collapse the open list."""
        self.state.expanded_detail = None if self.state.expanded_detail == name else name
        await self.swap_to(
            interaction,
            DetailsView(self.state, runtime=self.runtime, allowed_user_id=self.allowed_user_id),
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

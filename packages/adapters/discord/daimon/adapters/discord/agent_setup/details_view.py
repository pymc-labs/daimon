"""Details — one agent's whole readable state on the panel's one message.

The card answers "what can I use, and how do I change it?" without teaching
anybody the configuration structure: what the agent is for, where a mention
actually reaches it, what it is wired to, and the ways to act on it — talk
to Daimon about it, drive it from a coding tool, or add a skill.

``build_details_container`` is pure: it folds an `AgentDetails` into a
container and never reads a clock, a session or a credential. ``DetailsView``
is the shell that attaches the callbacks. Configuration lists expose metadata
only; key values are never part of the rendered model.
"""

from __future__ import annotations

import contextlib
import functools
from typing import cast
from urllib.parse import quote

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.add_skill import (
    ADD_SKILL_LABEL,
    AddSkillModal,
    skill_change_refusal,
)
from daimon.adapters.discord.agent_setup.avatar import (
    NONRETRYABLE_PICTURE_MESSAGES,
    avatar_public_url,
    reset_agent_avatar,
    upload_agent_avatar,
)
from daimon.adapters.discord.agent_setup.budget import LAYOUT_TEXT_BUDGET
from daimon.adapters.discord.agent_setup.conversations import open_setup_conversation
from daimon.adapters.discord.agent_setup.expiry import ExpiringView
from daimon.adapters.discord.agent_setup.mcp_access import send_coding_tools_access
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.layout import hairline
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_detail_lists import (
    DETAIL_LIST_COLLAPSED_COUNT,
    DetailListName,
    format_detail_lists,
)
from daimon.core.agent_details import AgentDetails, RepoBinding
from daimon.core.agent_identity import identity_enabled_for, is_builtin_agent
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import RepoAccess, normalize_owner_repo
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.roster import RosterAgent
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
AVATAR_CHANGE_ID = "agent-setup:avatar-change"
AVATAR_RESET_ID = "agent-setup:avatar-reset"
AVATAR_RESET_CONFIRM_ID = "agent-setup:avatar-reset-confirm"
AVATAR_RESET_CANCEL_ID = "agent-setup:avatar-reset-cancel"
AVATAR_DETAILS_ID = "agent-setup:avatar-details"


def picture_details_embed() -> discord.Embed:
    embed = discord.Embed(title="Picture", colour=discord.Colour.blurple())
    embed.add_field(
        name="Visibility", value="Anyone who sees a message can open the picture.", inline=False
    )
    embed.add_field(
        name="After change", value="The old picture may still appear for a while.", inline=False
    )
    embed.set_footer(text="Agent setup")
    return embed


def picture_status_embed(message: str, *, success: bool) -> discord.Embed:
    embed = discord.Embed(
        title=message.split(".", 1)[0] + ".",
        colour=discord.Colour.green() if success else discord.Colour.orange(),
    )
    remainder = message.partition(". ")[2]
    if remainder:
        embed.add_field(name="Next", value=remainder, inline=False)
    embed.set_footer(text="Agent setup")
    return embed


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
    is_builtin: bool = False,
    identity_enabled: bool = False,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Fold one agent's details into the panel card. Pure — no I/O, no clock.

    ``state`` and ``is_admin`` are carried for symmetry with the other two
    screens' builders; the role already reached this card through
    ``details.unrouted_note``, which core wrote in the reader's own voice.
    """
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
    if identity_enabled and not is_builtin:
        avatar_url = state.avatar_urls.get(details.name)
        avatar_copy = "**Picture**\nShown next to this agent's messages."
        if avatar_url:
            avatar_text: discord.ui.TextDisplay[discord.ui.LayoutView] = discord.ui.TextDisplay(
                avatar_copy
            )
            avatar_thumbnail: discord.ui.Thumbnail[discord.ui.LayoutView] = discord.ui.Thumbnail(
                avatar_url
            )
            container.add_item(discord.ui.Section(avatar_text, accessory=avatar_thumbnail))
        else:
            container.add_item(discord.ui.TextDisplay(avatar_copy))
        if is_admin:
            avatar_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            avatar_row.add_item(
                discord.ui.Button(
                    label="Change",
                    custom_id=AVATAR_CHANGE_ID,
                    style=discord.ButtonStyle.secondary,
                )
            )
            avatar_row.add_item(
                discord.ui.Button(
                    label="Use default",
                    custom_id=AVATAR_RESET_ID,
                    style=discord.ButtonStyle.secondary,
                )
            )
            container.add_item(avatar_row)
        if is_admin:
            details_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            details_row.add_item(
                discord.ui.Button(
                    label="Details",
                    custom_id=AVATAR_DETAILS_ID,
                    style=discord.ButtonStyle.secondary,
                )
            )
            container.add_item(details_row)
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
    """The Details screen: one agent, its actions, and the way back.

    Rebuilt from ``state`` on every render, so expanding a configuration list or coming
    back from a setup conversation costs no refetch.
    """

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        details: AgentDetails | None = None,
        agent: RosterAgent | None = None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        details = details or state.details
        assert details is not None, "DetailsView needs a loaded AgentDetails on the panel state"
        self.details = details
        self.agent = agent or state.selected_agent
        container = build_details_container(
            state,
            details,
            expanded_detail=state.expanded_detail,
            is_admin=state.is_admin,
            attribution=None,
            is_builtin=is_builtin_agent(
                name=details.name,
                metadata={MA_METADATA_KEY_MANAGED: "true"} if details.daimon_managed else None,
                default_agent_name=runtime.deployment_default.agent_name,
            ),
            identity_enabled=identity_enabled_for(runtime.settings, "discord", state.guild_id),
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

        for child in container.walk_children():
            if isinstance(child, discord.ui.Button) and child.custom_id == AVATAR_CHANGE_ID:
                child.callback = self._on_change_avatar  # type: ignore[method-assign]
            elif isinstance(child, discord.ui.Button) and child.custom_id == AVATAR_RESET_ID:
                child.callback = self._on_reset_avatar  # type: ignore[method-assign]
            elif isinstance(child, discord.ui.Button) and child.custom_id == AVATAR_DETAILS_ID:
                child.callback = self._on_avatar_details  # type: ignore[method-assign]

        action_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        coding_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=CODING_TOOLS_LABEL, style=discord.ButtonStyle.secondary
        )
        coding_button.callback = self._on_coding_tools  # type: ignore[method-assign]  # per-instance callback
        action_row.add_item(coding_button)
        if self.agent is not None:
            github_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label="🐙 GitHub repos", style=discord.ButtonStyle.secondary
            )
            github_button.callback = self._on_github_repos  # type: ignore[method-assign]
            action_row.add_item(github_button)
            add_skill_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label=ADD_SKILL_LABEL, style=discord.ButtonStyle.secondary
            )
            add_skill_button.callback = self._on_add_skill  # type: ignore[method-assign]  # per-instance callback
            action_row.add_item(add_skill_button)
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

    async def _on_github_repos(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.agent_setup.github_repos import GitHubReposView
        from daimon.core.github_panel import GrantsPanel, load_grants_panel
        from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid

        if self.agent is None or interaction.guild_id != self.state.guild_id:
            return
        gate = GitHubReposView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            agent=self.agent,
            panel=GrantsPanel(mode="legacy", repos=(), working_repo=None, has_pat=False),
        )
        if not await gate.allowed(interaction):
            await interaction.followup.send(
                "You cannot view this agent's GitHub repos.", ephemeral=True
            )
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=self.agent.ma_agent_id)
        async with self.runtime.sessionmaker() as session:
            panel = await load_grants_panel(session, tenant_id=tenant_id, agent_id=agent_id)
        if not any(repo.live_ceiling is not None for repo in panel.repos) and not panel.has_pat:
            from daimon.adapters.discord.agent_setup.github_add_repos import GitHubAddReposView

            await self.swap_to(
                interaction,
                GitHubAddReposView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    agent=self.agent,
                    panel=panel,
                ),
            )
            return
        await self.swap_to(
            interaction,
            GitHubReposView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
                panel=panel,
            ),
        )

    async def _on_setup(self, interaction: discord.Interaction) -> None:
        """Open a setup conversation about the agent this card is describing."""
        log.info("agent_setup.details.setup.click", agent_name=self.details.name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_setup_conversation(
            interaction,
            runtime=self.runtime,
            state=self.state,
            target=self.agent,
        )

    async def _on_coding_tools(self, interaction: discord.Interaction) -> None:
        """Mint a coding-tool token; `send_coding_tools_access` decides who may, live."""
        log.info("agent_setup.details.coding_tools.click", agent_name=self.details.name)
        await send_coding_tools_access(
            interaction,
            runtime=self.runtime,
            state=self.state,
            allowed_user_id=self.allowed_user_id,
            agent=self.agent,
        )

    async def _on_change_avatar(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PictureUploadModal(self))

    async def refresh_after_avatar_change(self) -> None:
        """Redraw the live Details panel with its new picture thumbnail."""
        panel_interaction = self._render_interaction
        if panel_interaction is None or self._is_superseded():
            return
        refreshed = DetailsView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            details=self.details,
            agent=self.agent,
        )
        try:
            await panel_interaction.edit_original_response(
                view=refreshed.bind_render_interaction(panel_interaction, panel=self.state),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            if exc.status != 401 or exc.code != 50027:
                raise

    async def _on_avatar_details(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=picture_details_embed(),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _on_reset_avatar(self, interaction: discord.Interaction) -> None:
        await self.swap_to(
            interaction,
            AvatarResetConfirmView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                details=self.details,
                agent=self.agent,
            ),
        )

    async def _on_add_skill(self, interaction: discord.Interaction) -> None:
        """Open the Add skill form for a caller who may change this agent's skills now."""
        agent = self.agent
        if agent is None:
            return
        try:
            refusal = await skill_change_refusal(
                interaction, runtime=self.runtime, state=self.state, agent=agent
            )
        except (DaimonError, anthropic.APIError) as exc:
            request_id = generate_request_id()
            log.exception("agent_setup.add_skill.check_failed", request_id=request_id)
            await interaction.response.send_message(
                render_error(exc, request_id=request_id), ephemeral=True
            )
            return
        if refusal is not None:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await interaction.response.send_modal(AddSkillModal(self, agent))

    async def _on_toggle_detail(
        self, interaction: discord.Interaction, *, name: DetailListName
    ) -> None:
        """Expand one detail list at a time, or collapse the open list."""
        self.state.expanded_detail = None if self.state.expanded_detail == name else name
        await self.swap_to(
            interaction,
            DetailsView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                details=self.details,
                agent=self.agent,
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


class PictureUploadModal(discord.ui.Modal):
    """One file field; submission reuses the slash command's validation path."""

    def __init__(self, view: DetailsView) -> None:
        super().__init__(title="Change picture")
        self._view = view
        self.add_item(discord.ui.TextDisplay("Choose a picture. Up to 2 MB."))
        label: discord.ui.Label[PictureUploadModal] = discord.ui.Label(
            text="Picture",
            description="PNG, JPG, GIF or WebP",
            component=discord.ui.FileUpload(required=True, min_values=1, max_values=1),
        )
        self.file_input = cast("discord.ui.FileUpload[PictureUploadModal]", label.component)
        self.add_item(label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # A channel-message defer gives this modal its own original response.
        # The retry view may then expire without replacing the setup panel.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.user.id != self._view.allowed_user_id:
            await interaction.edit_original_response(
                content="This panel has expired. Open agent setup."
            )
            return
        uploads = self.file_input.values
        if not uploads:
            await interaction.edit_original_response(content="Choose a picture.")
            return
        tenant_id = derive_tenant_uuid(
            platform="discord", workspace_id=str(self._view.state.guild_id)
        )
        message, avatar = await upload_agent_avatar(
            interaction,
            self._view.runtime,
            tenant_id=tenant_id,
            agent_name=self._view.details.name,
            attachment=uploads[0],
        )
        if avatar is not None:
            self._view.state.avatar_urls[self._view.details.name] = avatar_public_url(
                self._view.runtime, avatar
            )
        embed = picture_status_embed(message, success=avatar is not None)
        if avatar is None:
            if message in NONRETRYABLE_PICTURE_MESSAGES:
                await interaction.edit_original_response(embed=embed, view=None)
            else:
                await interaction.edit_original_response(
                    embed=embed,
                    view=PictureRetryView(self._view).bind_render_interaction(
                        interaction, panel=None
                    ),
                )
        else:
            await interaction.edit_original_response(embed=embed)
            await self._view.refresh_after_avatar_change()


class PictureRetryView(ExpiringView, discord.ui.View):
    """Let a rejected file be replaced from the same panel."""

    def __init__(self, details: DetailsView) -> None:
        super().__init__(timeout=600)
        self._details = details
        button: discord.ui.Button[PictureRetryView] = discord.ui.Button(
            label="Choose picture", style=discord.ButtonStyle.secondary
        )
        button.callback = self._on_retry  # type: ignore[method-assign]
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._details.allowed_user_id

    async def _on_retry(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PictureUploadModal(self._details))

    async def on_timeout(self) -> None:
        """Expire the retry button while keeping the response as an embed."""
        interaction = self._render_interaction
        if interaction is None:
            return
        with contextlib.suppress(discord.NotFound):
            await interaction.edit_original_response(
                embed=picture_status_embed("Panel expired. Open agent setup.", success=False),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class AvatarResetConfirmView(PanelViewBase):
    """Ask for a second click before rotating a public avatar URL."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        details: AgentDetails,
        agent: RosterAgent | None,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.details = details
        self.agent = agent
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
            discord.ui.TextDisplay("Use the default picture?")
        )
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        confirm: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label="Use default",
            custom_id=AVATAR_RESET_CONFIRM_ID,
            style=discord.ButtonStyle.danger,
        )
        confirm.callback = self._on_confirm  # type: ignore[method-assign]
        row.add_item(confirm)
        cancel: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label="Cancel",
            custom_id=AVATAR_RESET_CANCEL_ID,
            style=discord.ButtonStyle.secondary,
        )
        cancel.callback = self._on_cancel  # type: ignore[method-assign]
        row.add_item(cancel)
        container.add_item(row)
        self.add_item(container)

    def _details_view(self) -> DetailsView:
        return DetailsView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            details=self.details,
            agent=self.agent,
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self.swap_to(interaction, self._details_view())

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        message, avatar = await reset_agent_avatar(
            interaction,
            self.runtime,
            tenant_id=tenant_id,
            agent_name=self.details.name,
        )
        if avatar is not None:
            self.state.avatar_urls[self.details.name] = avatar_public_url(self.runtime, avatar)
            await self.swap_to(interaction, self._details_view())
        await interaction.followup.send(
            embed=picture_status_embed(message, success=avatar is not None), ephemeral=True
        )

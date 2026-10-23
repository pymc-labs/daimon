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

import functools
import re
from urllib.parse import quote

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.add_skill import (
    ADD_SKILL_LABEL,
    AddSkillModal,
    skill_change_refusal,
)
from daimon.adapters.discord.agent_setup.avatar import (
    avatar_public_url,
    reset_agent_avatar,
)
from daimon.adapters.discord.agent_setup.budget import LAYOUT_TEXT_BUDGET
from daimon.adapters.discord.agent_setup.conversations import open_setup_conversation
from daimon.adapters.discord.agent_setup.mcp_access import send_coding_tools_access
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import is_guild_admin, refuse_if_not_admin
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.layout import hairline
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core import channel_backend
from daimon.core.agent_detail_lists import (
    DETAIL_LIST_COLLAPSED_COUNT,
    DetailListName,
    format_detail_lists,
)
from daimon.core.agent_details import AgentDetails, RepoBinding
from daimon.core.agent_identity import identity_enabled_for, is_builtin_agent
from daimon.core.authz import build_subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import RepoAccess, normalize_owner_repo
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.roster import RosterAgent
from daimon.core.scope import AnsweringPlace
from daimon.core.setup_conversations import (
    setup_target_label,
    shared_keys_sentence,
)
from daimon.core.stores.domain import CompatibilityPreview
from mux.contracts.config import BackendConfig

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
            parts.append("the Daimon-wide default")
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
        # The URL only for a viewer who may read the agent's setup; else the name alone.
        items["connections"] = tuple(
            f"[{_escape_link_label(server.name)}]({quote(server.url, safe=':/?&=#%+-._~')})"
            if server.url is not None
            else _plain_label(server.name)
            for server in details.mcp_servers
        )
    if details.keys:
        items["keys"] = tuple(f"`{entry.name}`" for entry in details.keys)
    return items


def _plain_label(text: str) -> str:
    """A connection name as literal text: no Markdown, no link, whatever it holds."""
    return re.sub(r"([\\*_~|`>\[\]()#-])", r"\\\1", text)


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
        backend_preview: CompatibilityPreview | None = None,
        backend_clear: bool = False,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        details = details or state.details
        assert details is not None, "DetailsView needs a loaded AgentDetails on the panel state"
        self.details = details
        self.agent = agent or state.selected_agent
        self.backend_preview = backend_preview
        self.backend_clear = backend_clear
        if backend_preview is not None:
            selection = "Use default" if backend_clear else backend_preview.target_backend
            lines = [f"**Model provider: {selection}**"]
            for label, items in (
                ("Unsupported skills", backend_preview.unsupported_skills),
                ("Unsupported connections", backend_preview.unsupported_mcps),
                ("Unsupported capabilities", backend_preview.unsupported_capabilities),
                ("Shared-thread limits", backend_preview.shared_thread_limits),
            ):
                names = ", ".join(items) or "None"
                shown = names if len(names) <= 500 else names[:500] + "… (truncated)"
                lines.append(f"{label}: {shown}")
            lines.extend(
                [
                    backend_preview.cost_line,
                    "Existing threads keep their provider; channel settings still apply.",
                ]
            )
            if not backend_preview.allowed:
                lines.append("This provider is off for this server. Pick another.")
            preview_text: discord.ui.TextDisplay[discord.ui.LayoutView] = discord.ui.TextDisplay(
                "\n".join(lines)[:3000]
            )
            preview_container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
                preview_text
            )
            preview_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            confirm: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label="Confirm",
                style=discord.ButtonStyle.primary,
                disabled=not backend_preview.allowed,
            )
            confirm.callback = self._on_backend_confirm  # type: ignore[method-assign]
            cancel: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(label="Cancel")
            cancel.callback = self._on_backend_cancel  # type: ignore[method-assign]
            preview_row.add_item(confirm)
            preview_row.add_item(cancel)
            preview_container.add_item(preview_row)
            self.add_item(preview_container)
            return
        is_builtin = is_builtin_agent(
            name=details.name,
            metadata={MA_METADATA_KEY_MANAGED: "true"} if details.daimon_managed else None,
            default_agent_name=runtime.deployment_default.agent_name,
        )
        container = build_details_container(
            state,
            details,
            expanded_detail=state.expanded_detail,
            is_admin=state.is_admin,
            attribution=None,
            is_builtin=is_builtin,
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
            if isinstance(child, discord.ui.Button) and child.custom_id == AVATAR_RESET_ID:
                child.callback = self._on_reset_avatar  # type: ignore[method-assign]
            elif isinstance(child, discord.ui.Button) and child.custom_id == AVATAR_DETAILS_ID:
                child.callback = self._on_avatar_details  # type: ignore[method-assign]

        # Only the buttons that can work for this reader: a coding-tool token and a
        # skill are admin changes, and a built-in agent takes no skill at all, so a
        # member is never offered a button that only refuses.
        action_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        if state.is_admin:
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
        if self.agent is not None and state.is_admin and not is_builtin:
            add_skill_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
                label=ADD_SKILL_LABEL, style=discord.ButtonStyle.secondary
            )
            add_skill_button.callback = self._on_add_skill  # type: ignore[method-assign]  # per-instance callback
            action_row.add_item(add_skill_button)
        if action_row.children:  # Discord rejects an empty row
            container.add_item(action_row)

        if (
            channel_backend.backend_selection_enabled(runtime.settings.turn)
            and state.is_admin
            and state.backend_options
        ):
            backend_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
            select: discord.ui.Select[discord.ui.LayoutView] = discord.ui.Select(
                placeholder="Model provider",
                options=[
                    discord.SelectOption(
                        label=channel_backend.DEFAULT_BACKEND_LABEL,
                        value="clear",
                        default=state.backend_selection is None,
                    ),
                    *(
                        discord.SelectOption(
                            label=provider,
                            value=provider,
                            default=state.backend_selection == provider,
                        )
                        for provider in state.backend_options
                    ),
                ],
            )
            select.callback = functools.partial(  # type: ignore[method-assign]
                self._on_backend_select, select=select
            )
            backend_row.add_item(select)
            container.add_item(discord.ui.TextDisplay("**Model provider**"))
            container.add_item(backend_row)

        nav_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        back_button: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=BACK_LABEL, style=discord.ButtonStyle.secondary
        )
        back_button.callback = self._on_back  # type: ignore[method-assign]  # per-instance callback
        nav_row.add_item(back_button)
        nav_row.add_item(self.done_button())  # pyright: ignore[reportArgumentType]  # Button[Self] is the same runtime item
        container.add_item(nav_row)

        self.add_item(container)

    async def _backend_allowed(self, interaction: discord.Interaction) -> bool:
        if await refuse_if_not_admin(interaction):  # pyright: ignore[reportArgumentType]
            await self._backend_audit(interaction, outcome="denied", reason="needs_admin")
            return False
        if not await self.interaction_check(interaction):
            return False
        if (
            self.is_finished()
            or interaction.guild_id != self.state.guild_id
            or not channel_backend.backend_selection_enabled(self.runtime.settings.turn)
        ):
            await interaction.response.send_message(
                "Model provider wasn't changed. Open the panel again.", ephemeral=True
            )
            return False
        return True

    async def _backend_audit(
        self, interaction: discord.Interaction, *, outcome: PanelOutcome, reason: str
    ) -> None:
        await record_panel_write(
            self.runtime.sessionmaker,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id)),
            platform="discord",
            platform_user_id=str(interaction.user.id),
            op="agent_backend",
            outcome=outcome,
            reason=reason,
            agent_name=self.details.name,
        )

    async def _on_backend_select(
        self,
        interaction: discord.Interaction,
        *,
        select: discord.ui.Select[discord.ui.LayoutView],
    ) -> None:
        if not await self._backend_allowed(interaction):
            return
        await interaction.response.defer()
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        service = channel_backend.make_backend_service(
            self.runtime.sessionmaker, self.runtime.settings.turn
        )
        clear = select.values[0] == "clear"
        target = BackendConfig.model_validate(
            {"backend": None if clear else select.values[0]}
        ).backend
        selection = await service.read_selection(
            self.runtime.anthropic,
            tenant_id=tenant_id,
            agent_name=self.details.name,
            target_backend=target,
        )
        if selection is None:
            await self._backend_audit(interaction, outcome="denied", reason="selection_unavailable")
            await interaction.followup.send(
                "That preview isn't available. Nothing was changed; choose again.", ephemeral=True
            )
            return
        preview = selection.preview
        await self.swap_to(
            interaction,
            DetailsView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                details=self.details,
                agent=self.agent,
                backend_preview=preview,
                backend_clear=clear,
            ),
        )

    async def _on_backend_cancel(self, interaction: discord.Interaction) -> None:
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

    async def _on_backend_confirm(self, interaction: discord.Interaction) -> None:
        if not await self._backend_allowed(interaction) or self.backend_preview is None:
            return
        await interaction.response.defer()
        preview = self.backend_preview
        service = channel_backend.make_backend_service(
            self.runtime.sessionmaker, self.runtime.settings.turn
        )
        actor = channel_backend.BackendActor(
            tenant_id=preview.tenant_id,
            account_id=self.state.account_id,
            subject=build_subject(
                is_admin=is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]
                platform_user_id=str(interaction.user.id),
            ),
        )
        try:
            agent = await channel_backend.load_backend_agent(
                self.runtime.anthropic,
                self.runtime.sessionmaker,
                tenant_id=preview.tenant_id,
                agent_name=self.details.name,
            )
            if self.backend_clear:
                revision = await service.clear_backend(
                    agent, preview.setting_revision, actor, expected_preview_digest=preview.digest
                )
            else:
                revision = await service.set_backend(
                    agent,
                    preview.target_backend,
                    preview.setting_revision,
                    actor,
                    expected_preview_digest=preview.digest,
                )
        except channel_backend.AgentBackendRefused as exc:
            await self._backend_audit(interaction, outcome="denied", reason=exc.reason)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self._backend_audit(interaction, outcome="allowed", reason="completed")
        self.state.backend_selection = revision.backend
        await self._on_backend_cancel(interaction)

    async def _on_github_repos(self, interaction: discord.Interaction) -> None:
        from daimon.adapters.discord.agent_setup.github_repos import (
            GitHubReposView,
            mint_agent_connect_url,
        )
        from daimon.core.github_panel import GrantsPanel, load_grants_panel
        from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid

        if self.agent is None or interaction.guild_id != self.state.guild_id:
            return
        gate = GitHubReposView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            agent=self.agent,
            panel=GrantsPanel(repos=(), working_repo=None),
        )
        if not await gate.allowed(interaction):
            from daimon.core.github_connect_cards import ask_manager_line

            await interaction.followup.send(ask_manager_line(self.agent.name), ephemeral=True)
            return
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id))
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=self.agent.ma_agent_id)
        async with self.runtime.sessionmaker() as session:
            panel = await load_grants_panel(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=self.agent.name
            )
        try:
            connect_url = await mint_agent_connect_url(
                interaction, runtime=self.runtime, state=self.state, agent=self.agent
            )
        except ValueError:
            connect_url = None
        await self.swap_to(
            interaction,
            GitHubReposView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                agent=self.agent,
                panel=panel,
                connect_url=connect_url,
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

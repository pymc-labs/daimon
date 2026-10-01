"""Add skill, from Details: paste or upload one skill, see what it holds, then add it.

The skill becomes the agent's own copy; the shared library and the built-in
agents are never touched. A server admin may add to any agent they could edit,
a channel admin to one that answers only in their channels, anyone to one that
answers nowhere. The button, the submit and the Add click each re-check that
live, and Add re-reads the agent itself.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, cast

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.hydrate import load_details_for
from daimon.adapters.discord.agent_setup.navigation import PanelViewBase
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import ADMIN_NOUN, channel_admin_caller
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.layout import hairline
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_details import AgentDetails
from daimon.core.agent_reach import load_target_facts
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.operation_policy import decide_operation
from daimon.core.roster import RosterAgent
from daimon.core.skill_zip import MAX_UNCOMPRESSED_BYTES
from daimon.core.skills.add import add_agent_skill
from daimon.core.skills.ingest import (
    SkillBundle,
    SkillIngestError,
    SkillPreview,
    bundle_from_markdown,
    bundle_from_upload,
    require_upload_suffix,
)

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.agent_setup.details_view import DetailsView

log = structlog.get_logger()

ADD_SKILL_LABEL: Final = "➕ Add skill"
ADD_LABEL: Final = "Add"
CANCEL_LABEL: Final = "Cancel"
BUILT_IN_MESSAGE: Final = (
    "This is a starting agent and can't be changed directly. "
    "Ask me to fork it, then add the skill to the fork."
)
_SHOWN_FILES: Final = 15
_PATH_CHARS: Final = 80


def needs_admin_message(agent_name: str) -> str:
    return (
        f"Others use {agent_name} (a channel or server default, a thread, or someone else's "
        f"routine or queued task), so adding a skill needs {ADMIN_NOUN} or an admin of every "
        "channel it answers in."
    )


async def skill_change_refusal(
    interaction: discord.Interaction,
    *,
    runtime: DiscordRuntime,
    state: PanelState,
    agent: RosterAgent,
) -> str | None:
    """Why the caller may not change this agent's skills right now, or None."""
    if agent.is_built_in:
        return BUILT_IN_MESSAGE
    caller = channel_admin_caller(interaction.user)
    async with runtime.sessionmaker() as session:
        facts = await load_target_facts(
            session,
            "skill_add",
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id)),
            platform="discord",
            agent_name=agent.name,
            default=runtime.deployment_default,
            caller=caller,
            is_daimon_managed=False,
        )
    if decide_operation("skill_add", is_admin=caller.is_server_admin, target=facts) == "allow":
        return None
    return needs_admin_message(agent.name)


def _bounded(paths: list[str]) -> str:
    shown = [f"`{path[:_PATH_CHARS]}`" for path in paths[:_SHOWN_FILES]]
    if len(paths) > _SHOWN_FILES:
        shown.append(f"+{len(paths) - _SHOWN_FILES} more")
    return ", ".join(shown)


def preview_text(preview: SkillPreview, *, agent_name: str) -> str:
    """What the skill holds, in the words the Add button is confirming. Pure."""
    description = discord.utils.escape_mentions(discord.utils.escape_markdown(preview.description))
    lines = [
        f"## Add {preview.name} to {agent_name}?",
        description,
        f"**Files:** {_bounded(preview.files)}",
    ]
    if preview.scripts:
        lines.append(f"⚠️ **{agent_name} could run:** {_bounded(preview.scripts)}")
    lines.append(f"-# It becomes {agent_name}'s own skill. Shared skills are not changed.")
    return "\n".join(lines)


class AddSkillModal(discord.ui.Modal):
    """Paste a SKILL.md, or upload a SKILL.md or a .zip of one skill's folder."""

    def __init__(self, view: DetailsView, agent: RosterAgent) -> None:
        super().__init__(title=f"Add a skill to {agent.name}"[:45])
        self._view, self._agent = view, agent
        self.add_item(
            discord.ui.TextDisplay(
                "-# Paste a SKILL.md or upload one file. You see what it holds before "
                "anything is added."
            )
        )
        paste_label: discord.ui.Label[AddSkillModal] = discord.ui.Label(
            text="SKILL.md",
            component=discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=4000,
                placeholder="---\nname: my-skill\ndescription: What it does\n---",
            ),
        )
        self.paste = cast("discord.ui.TextInput[AddSkillModal]", paste_label.component)
        self.add_item(paste_label)
        upload_label: discord.ui.Label[AddSkillModal] = discord.ui.Label(
            text="Or a file",
            description="A SKILL.md, or a .zip of one skill's folder",
            component=discord.ui.FileUpload(required=False, min_values=0, max_values=1),
        )
        self.upload = cast("discord.ui.FileUpload[AddSkillModal]", upload_label.component)
        self.add_item(upload_label)

    async def _bundle(self) -> tuple[SkillBundle, str]:
        pasted = self.paste.value.strip()
        uploads = self.upload.values
        if bool(pasted) == bool(uploads):
            raise SkillIngestError("Paste a SKILL.md or upload one file, not both or neither.")
        if pasted:
            return await asyncio.to_thread(bundle_from_markdown, pasted), "pasted"
        upload = uploads[0]
        require_upload_suffix(upload.filename)
        if upload.size > MAX_UNCOMPRESSED_BYTES:
            raise SkillIngestError(f"{upload.filename} is larger than a skill may be.")
        try:
            data = await upload.read()
        except discord.HTTPException as exc:
            raise SkillIngestError("I could not read that file. Upload it again.") from exc
        bundle = await asyncio.to_thread(bundle_from_upload, data, filename=upload.filename)
        return bundle, f"attachment {upload.filename}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view, agent = self._view, self._agent
        try:
            bundle, origin = await self._bundle()
            refusal = await skill_change_refusal(
                interaction, runtime=view.runtime, state=view.state, agent=agent
            )
        except SkillIngestError as exc:
            await interaction.followup.send(f"{exc} Nothing was added.", ephemeral=True)
            return
        except DaimonError as exc:
            request_id = generate_request_id()
            log.exception("agent_setup.add_skill.preview_failed", request_id=request_id)
            await interaction.followup.send(
                render_error(exc, request_id=request_id), ephemeral=True
            )
            return
        if refusal is not None:
            await interaction.followup.send(refusal, ephemeral=True)
            return
        log.info("agent_setup.add_skill.preview", agent_name=agent.name)
        await view.swap_to(
            interaction,
            SkillPreviewView(
                view.state,
                runtime=view.runtime,
                allowed_user_id=view.allowed_user_id,
                details=view.details,
                agent=agent,
                bundle=bundle,
                origin=origin,
            ),
        )


class SkillPreviewView(PanelViewBase):
    """The preview on the panel's message: Add uploads exactly this, Cancel goes back."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
        details: AgentDetails,
        agent: RosterAgent,
        bundle: SkillBundle,
        origin: str,
    ) -> None:
        super().__init__(state, runtime=runtime, allowed_user_id=allowed_user_id)
        self.details, self.agent, self.bundle, self.origin = details, agent, bundle, origin
        container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
        container.add_item(
            discord.ui.TextDisplay(preview_text(bundle.preview, agent_name=agent.name))
        )
        container.add_item(hairline())
        row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        add: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=ADD_LABEL, style=discord.ButtonStyle.success
        )
        add.callback = self._on_add  # type: ignore[method-assign]  # per-instance callback
        cancel: discord.ui.Button[discord.ui.LayoutView] = discord.ui.Button(
            label=CANCEL_LABEL, style=discord.ButtonStyle.secondary
        )
        cancel.callback = self._on_cancel  # type: ignore[method-assign]  # per-instance callback
        row.add_item(add)
        row.add_item(cancel)
        container.add_item(row)
        self.add_item(container)

    def _details_view(self, details: AgentDetails) -> PanelViewBase:
        # Lazy import: Details opens this screen.
        from daimon.adapters.discord.agent_setup.details_view import DetailsView

        return DetailsView(
            self.state,
            runtime=self.runtime,
            allowed_user_id=self.allowed_user_id,
            details=details,
            agent=self.agent,
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self.swap_to(interaction, self._details_view(self.details))

    async def _on_add(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        name, preview = self.agent.name, self.bundle.preview
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(self.state.guild_id))
        try:
            refusal = await skill_change_refusal(
                interaction, runtime=self.runtime, state=self.state, agent=self.agent
            )
            agent = None
            if refusal is None:
                agent = await self.runtime.anthropic.beta.agents.retrieve(self.agent.ma_agent_id)
                refusal = _stamp_refusal(agent.metadata, tenant_id=str(tenant_id), name=name)
            if refusal is not None or agent is None:
                await interaction.followup.send(refusal or BUILT_IN_MESSAGE, ephemeral=True)
                return
            result = await add_agent_skill(
                self.runtime.anthropic,
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                agent=agent,
                agent_name=name,
                bundle=self.bundle,
                origin=self.origin,
                added_by_account_id=self.state.account_id,
            )
        except SkillIngestError as exc:
            await interaction.followup.send(f"{exc} Nothing was added.", ephemeral=True)
            return
        except (DaimonError, anthropic.APIError) as exc:
            request_id = generate_request_id()
            log.exception("agent_setup.add_skill.failed", agent_name=name, request_id=request_id)
            await interaction.followup.send(
                render_error(exc, request_id=request_id), ephemeral=True
            )
            return
        log.info("agent_setup.add_skill.added", agent_name=name, action=result.action)
        done = "already had" if result.action == "unchanged" else "now has"
        await interaction.followup.send(
            f"{name} {done} the skill **{preview.name}**.", ephemeral=True
        )
        try:
            details = await load_details_for(self.runtime, state=self.state, agent=self.agent)
        except (DaimonError, anthropic.APIError):
            details = self.details
        self.state.details = details
        await self.swap_to(interaction, self._details_view(details))


def _stamp_refusal(metadata: dict[str, str], *, tenant_id: str, name: str) -> str | None:
    """Re-read from the agent itself: another server's, built-in or system agents are refused."""
    if metadata.get(MA_METADATA_KEY_TENANT) != tenant_id:
        return f"{name} is not an agent of this server."
    if metadata.get(MA_METADATA_KEY_MANAGED) == "true" or MA_METADATA_KEY_ACCOUNT not in metadata:
        return BUILT_IN_MESSAGE
    return None

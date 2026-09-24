"""The one creation shortcut: a direct form that lands on the new agent's Details.

Everything else about an agent happens in conversation; this form exists because
"make me an agent" is the one step a person should not have to negotiate. It
fires no turn, checks no billing admission and opens no session — a workspace
that cannot afford a turn can still create and inspect an agent.

Submitting returns to Details rather than to the roster, because the honest next
step lives there: a brand-new agent answers nowhere, and Details carries the
sentence that says so plus the exact routing request to hand an admin.
"""

from __future__ import annotations

import uuid

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.details_view import DetailsView
from daimon.adapters.discord.agent_setup.hydrate import load_details_for
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel
from daimon.adapters.discord.agent_setup.write import create_blank_agent, validate_model_id
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.errors import DaimonError
from daimon.core.models_catalog import list_model_choices
from daimon.core.observability import capture_exception_with_scope
from daimon.core.roster import load_roster

import discord

log = structlog.get_logger()


def _capture_new_agent_exception(
    err: BaseException,
    *,
    tenant_id: uuid.UUID | None,
    guild_id: int | None,
    rid: str,
) -> None:
    """Bind tenant context into structlog contextvars and capture to Sentry.

    A modal submit runs outside the mention handler's request-id bind, so
    nothing is in contextvars at this site; bind, capture, then unbind so the
    binding cannot leak past the handler.
    """
    bound: dict[str, str] = {"rid": rid}
    if tenant_id is not None:
        bound["tenant_id"] = str(tenant_id)
    if guild_id is not None:
        bound["guild_id"] = str(guild_id)
    structlog.contextvars.bind_contextvars(**bound)
    try:
        capture_exception_with_scope(err)
    finally:
        structlog.contextvars.unbind_contextvars(*bound)


class NewAgentModal(discord.ui.Modal, title="New agent"):
    """Three fields: name, what it should help with, model. No turn fires."""

    def __init__(
        self,
        state: PanelState,
        *,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__()
        self.state = state
        self.runtime = runtime
        self.allowed_user_id = allowed_user_id
        self._panel_render_seq = state.render_seq
        # Each TextInput's own `label=` is redundant with the wrapping Label's
        # `text=` (Discord's modern Label-wrapped modal fields), but is kept
        # so `scripts/lint_discord_modals.py`'s unconditional missing-label
        # rule passes; discord.py's constructor writes it straight into the
        # underlying component dataclass rather than through the deprecated
        # `TextInput.label` property, so it costs nothing at runtime.
        self.name_label: discord.ui.Label[NewAgentModal] = discord.ui.Label(
            text="Name",
            description="lowercase, dashes ok",
            component=discord.ui.TextInput(
                label="Name", placeholder="churn-explorer", max_length=64
            ),
        )
        self.prompt_label: discord.ui.Label[NewAgentModal] = discord.ui.Label(
            text="What should it help with?",
            description="one or two sentences",
            component=discord.ui.TextInput(
                label="What should it help with?",
                style=discord.TextStyle.paragraph,
                max_length=2000,
                required=False,
            ),
        )
        self.model_label: discord.ui.Label[NewAgentModal] = discord.ui.Label(
            text="Model",
            component=discord.ui.Select(
                options=[
                    discord.SelectOption(
                        label=choice.label,
                        value=choice.id,
                        description=choice.description,
                        default=choice.is_default,
                    )
                    for choice in list_model_choices(default=DEFAULT_AGENT_MODEL)
                ],
                min_values=1,
                max_values=1,
            ),
        )
        self.add_item(self.name_label)
        self.add_item(self.prompt_label)
        self.add_item(self.model_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name_field = self.name_label.component
        assert isinstance(name_field, discord.ui.TextInput), "name field is a TextInput"
        prompt_field = self.prompt_label.component
        assert isinstance(prompt_field, discord.ui.TextInput), "prompt field is a TextInput"
        model_field = self.model_label.component
        assert isinstance(model_field, discord.ui.Select), "model field is a Select"
        new_name = str(name_field.value).strip()
        model_value = model_field.values[0]
        system_value = str(prompt_field.value).strip() or None
        log.info(
            "agent_setup.new.submit",
            new_name=new_name,
            model=model_value,
            has_system=system_value is not None,
        )
        error = validate_model_id(model_value)
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer()
        tenant_id: uuid.UUID | None = None
        try:
            tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)
            created = await create_blank_agent(
                self.runtime,
                tenant_id=tenant_id,
                name=new_name,
                system=system_value,
                model=model_value,
                account_id=self.state.guild_account_id,
            )
            if created.anthropic_id is None:
                raise DaimonError(
                    "Could not confirm the new agent. "
                    "Reopen `/agent-setup` to check before retrying."
                )
            async with self.runtime.sessionmaker() as session:
                roster = await load_roster(
                    session,
                    self.runtime.anthropic,
                    tenant_id=tenant_id,
                    platform="discord",
                    channel_id=str(self.state.channel_id),
                    thread_id=self.state.thread_id,
                    default=self.state.deployment_default,
                )
            if self._panel_render_seq != self.state.render_seq:
                await interaction.followup.send(
                    f"Created **{new_name}**. The setup panel has moved on; run "
                    "`/agent-setup` again to view it.",
                    ephemeral=True,
                )
                return
            agent = next(
                (row for row in roster.rows if row.ma_agent_id == created.anthropic_id), None
            )
            if agent is None:
                raise DaimonError(
                    f"**{new_name}** was created but is not listed yet. "
                    "Reopen `/agent-setup` to see it."
                )
            details = await load_details_for(self.runtime, state=self.state, agent=agent)
            if self._panel_render_seq != self.state.render_seq:
                await interaction.followup.send(
                    f"Created **{new_name}**. The setup panel has moved on; run "
                    "`/agent-setup` again to view it.",
                    ephemeral=True,
                )
                return
            self.state.roster_agents = roster.rows
            self.state.answering = roster.answering
            self.state.select_agent(agent)
            self.state.details = details
            await interaction.edit_original_response(
                view=DetailsView(
                    self.state,
                    runtime=self.runtime,
                    allowed_user_id=self.allowed_user_id,
                    details=details,
                    agent=agent,
                ).bind_render_interaction(interaction, panel=self.state),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (DaimonError, anthropic.APIError) as err:
            rid = generate_request_id()
            log.exception(
                "agent_setup.new.failed",
                new_name=new_name,
                model=model_value,
                err_type=type(err).__name__,
                request_id=rid,
            )
            await interaction.followup.send(render_error(err, request_id=rid), ephemeral=True)
            return
        except Exception as err:
            # Modal-submit boundary: discord.py dispatches on_submit as a bare
            # task, so an unexpected failure here has no other handler. Capture
            # it with tenant context and render, never swallow.
            rid = generate_request_id()
            log.exception(
                "agent_setup.new.failed",
                new_name=new_name,
                model=model_value,
                err_type=type(err).__name__,
                request_id=rid,
            )
            _capture_new_agent_exception(
                err, tenant_id=tenant_id, guild_id=interaction.guild_id, rid=rid
            )
            await interaction.followup.send(render_error(err, request_id=rid), ephemeral=True)
            return
        log.info("agent_setup.new.created", new_name=new_name, model=model_value)

"""AddMcpModal — split out of modals.py to keep that file under 200 LOC (LD-04-03)."""

from __future__ import annotations

import datetime as dt

import structlog
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel
from daimon.adapters.discord.agent_setup.write import call_reconcile_for_panel, mask_tail
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.mcp_merge import get_reserved_mcp_rejection
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_vault import add_external_mcp_credential
from daimon.core.session_context import SessionContext

import discord

_log = structlog.get_logger()


class AddMcpModal(discord.ui.Modal, title="Add MCP server"):
    """Add a URL MCP server. All three fields (Name / URL / Token) required.

    On submit: reduce panel state → reconcile MA → write vault credential.
    Vault write failures are surfaced to the user but do not unwind reconcile
    (the agent spec is already current; re-adding the MCP would conflict on
    the duplicate name). Reconcile failures short-circuit before the vault
    write — orphan credentials are the failure mode we're avoiding.
    """

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
        self.name_in: discord.ui.TextInput[AddMcpModal] = discord.ui.TextInput(
            label="Name", max_length=255
        )
        self.url_in: discord.ui.TextInput[AddMcpModal] = discord.ui.TextInput(
            label="URL", placeholder="https://example.com/mcp", max_length=1024
        )
        self.token_in: discord.ui.TextInput[AddMcpModal] = discord.ui.TextInput(
            label="Auth token", max_length=255
        )
        self.add_item(self.name_in)
        self.add_item(self.url_in)
        self.add_item(self.token_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.name_in).strip()
        url = str(self.url_in).strip()
        token = str(self.token_in).strip()
        if not name or not url or not token:
            await interaction.response.send_message(
                "All three fields (Name, URL, Auth token) are required.",
                ephemeral=True,
            )
            return
        # #142: reject reserved server names and the deployment's own endpoint
        # before defer — no state mutation, no reconcile, no vault write on rejection.
        _public_url = (
            str(self.runtime.settings.mcp.public_url)
            if self.runtime.settings.mcp.public_url is not None
            else None
        )
        _rejection = get_reserved_mcp_rejection(server_name=name, url=url, public_url=_public_url)
        if _rejection is not None:
            await interaction.response.send_message(_rejection, ephemeral=True)
            return
        await interaction.response.defer()
        server_entry = BetaManagedAgentsURLMCPServerParams(name=name, type="url", url=url)
        selected_name = self.state.selected.name if self.state.selected else None
        _log.info(
            "mcp_add.submit",
            mcp_name=name,
            mcp_url=url,
            token_masked=mask_tail(token),
            agent_name=selected_name,
        )
        try:
            tenant_id = await resolve_tenant_for_panel(self.runtime, interaction)
            # 1) Pure reducer: mutate in-memory state. apply_mcp_modal also adds
            #    the matching mcp_toolset entry to spec.tools so the spec is
            #    well-formed for MA's cross-reference validator.
            self.state.apply_mcp_modal(server_entry=server_entry, token_last4=token[-4:])
            # 2) Side-effectful MA write. If a vault-token write is added in
            #    future, it goes AFTER this line — never before (avoid orphan
            #    credentials on reconcile failure).
            outcome = await call_reconcile_for_panel(self.runtime, self.state, tenant_id=tenant_id)
        except Exception as err:
            _log.exception(
                "mcp_add.failed",
                mcp_name=name,
                agent_name=selected_name,
                err_type=type(err).__name__,
            )
            await interaction.followup.send(
                f"Failed to add MCP **{name}**: `{type(err).__name__}: {err}`",
                ephemeral=True,
            )
            return
        _log.info(
            "mcp_add.reconciled",
            mcp_name=name,
            agent_name=selected_name,
            action=outcome.action.value,
            anthropic_id=outcome.anthropic_id,
        )
        # 3) Resolve the live MA agent to derive the per-agent UUID so the
        #    external MCP credential lands in the agent's own vault.
        selected = self.state.selected
        if selected is None:
            await interaction.followup.send(
                f"MCP **{name}** registered on the agent, but no agent is "
                "selected — cannot write vault credential.",
                ephemeral=True,
            )
            return
        ma_agent = await find_agent_by_daimon_tag(
            self.runtime.anthropic,
            tenant_id=tenant_id,
            name=selected.name,
        )
        if ma_agent is None:
            _log.info(
                "mcp_add.agent_not_found_for_vault",
                mcp_name=name,
                agent_name=selected.name,
            )
            await interaction.followup.send(
                f"MCP **{name}** registered on the agent, but could not find "
                f"agent **{selected.name}** on MA — vault credential not written.",
                ephemeral=True,
            )
            return
        agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
        # 4) Resolve mcp settings needed to bootstrap the per-agent vault.
        mcp = self.runtime.settings.mcp
        if mcp.public_url is None or mcp.jwt_secret is None:
            await interaction.followup.send(
                f"MCP **{name}** registered on the agent, but daimon-mcp is "
                "not configured (public_url / jwt_secret missing) — vault "
                "credential not written.",
                ephemeral=True,
            )
            return
        jwt_secret = mcp.jwt_secret.get_secret_value().encode()
        public_url = str(mcp.public_url)
        # 5) Write the static_bearer credential to the per-agent vault so MA
        #    can authenticate against the user's MCP server. Bootstraps the
        #    per-agent vault when it does not yet exist. Failures here are
        #    surfaced to the user but do not unwind reconcile.
        try:
            await add_external_mcp_credential(
                self.runtime.anthropic,
                account_id=self.state.account_id,
                agent_id=agent_uuid,
                jwt_secret=jwt_secret,
                public_url=public_url,
                mcp_server_url=url,
                token=token,
                now=dt.datetime.now(dt.UTC),
                session_context=SessionContext(is_admin=self.state.is_admin),
            )
            _log.info(
                "mcp_add.vault_credential_written",
                mcp_name=name,
                mcp_url=url,
                token_masked=mask_tail(token),
                agent_name=selected_name,
            )
        except Exception as err:
            _log.exception(
                "mcp_add.vault_write_failed",
                mcp_name=name,
                mcp_url=url,
                token_masked=mask_tail(token),
                agent_name=selected_name,
                err_type=type(err).__name__,
            )
            # Surface only the exception class name to the user — never the
            # stringified exception. `str(err)` for SDK / network exceptions
            # may include the request envelope, which has historically been
            # observed to include kwargs (token-leak surface). Operators get
            # the full traceback via `_log.exception` above.
            await interaction.followup.send(
                f"MCP **{name}** registered on the agent, but storing its "
                f"auth token in the per-agent vault failed "
                f"(`{type(err).__name__}`). Tool calls to this server will "
                "401 until re-added.",
                ephemeral=True,
            )
        from daimon.adapters.discord.agent_setup.panel import (
            AgentSetupView,
            _get_thumbnail_url,  # pyright: ignore[reportPrivateUsage]  # module-internal helper
        )

        await interaction.edit_original_response(
            view=AgentSetupView(
                self.state,
                runtime=self.runtime,
                allowed_user_id=self.allowed_user_id,
                thumbnail_url=_get_thumbnail_url(interaction),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

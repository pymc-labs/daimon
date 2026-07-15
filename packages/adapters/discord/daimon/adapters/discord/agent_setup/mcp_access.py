"""Ephemeral MCP config render + Revoke follow-up View for the Talk via MCP button.

Provides:
- render_mcp_config: build the .mcp.json snippet for copy-paste into a coding agent
- _McpAccessView: ephemeral discord.ui.LayoutView carrying the config block + Revoke button

The rendered block follows the shape from CONTEXT l.66-70:
    {
      "daimon-<agent-name>": {
        "url": "<public_url>",
        "headers": { "Authorization": "Bearer <jwt>" }
      }
    }

A `claude mcp add` one-liner is also included for convenience.

Security: the JWT appears only in the ephemeral LayoutView — it is never logged,
never stored in channel history, and never appears in non-ephemeral messages.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import jwt as pyjwt
import structlog
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.agent_setup.tenant import resolve_tenant_for_panel
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token
from daimon.core.stores.mcp_tokens import revoke_mcp_token

import discord

log = structlog.get_logger()


async def send_connect_via_mcp(
    interaction: discord.Interaction,
    *,
    runtime: DiscordRuntime,
    state: PanelState,
    allowed_user_id: int,
) -> None:
    """Mint a per-agent MCP token for the selected agent and reply ephemerally
    with the config block + Revoke view.

    Shared handler for the agent-setup panel's "Connect via MCP" button. Lets a
    coding agent (Claude Code, etc.) drive the selected Daimon agent over MCP.
    The token is scoped to the derived per-agent UUID and attributed to the
    invoker's personal account; it is shown once and never logged.
    """
    selected = state.selected
    if selected is None:
        return
    log.info("agent_setup.connect_via_mcp.click", agent_name=selected.name)

    public_url = (
        str(runtime.settings.mcp.public_url)
        if runtime.settings.mcp.public_url is not None
        else None
    )
    jwt_secret = runtime.settings.mcp.jwt_secret
    assert public_url is not None and jwt_secret is not None, (
        "MCP public_url + jwt_secret required for Connect via MCP; "
        "check DAIMON_MCP__PUBLIC_URL / DAIMON_MCP__JWT_SECRET"
    )

    tenant_id = await resolve_tenant_for_panel(runtime, interaction)
    ma_agent = await find_agent_by_daimon_tag(
        runtime.anthropic,
        tenant_id=tenant_id,
        name=selected.name,
    )
    if ma_agent is None:
        log.info("agent_setup.connect_via_mcp.agent_missing", agent_name=selected.name)
        await interaction.response.send_message(
            f"Could not find agent **{selected.name}** on MA.", ephemeral=True
        )
        return
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))

    async with runtime.sessionmaker.begin() as session:
        token = await mint_agent_mcp_token(
            session,
            account_id=state.account_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            label=selected.name,
            secret=jwt_secret.get_secret_value().encode(),
            now=dt.datetime.now(dt.UTC),
        )

    # Decode jti from the token without verifying signature (we just minted it).
    claims: dict[str, object] = pyjwt.decode(
        token,
        options={"verify_signature": False},
    )  # pyright: ignore[reportAssignmentType]
    jti_str = claims.get("jti")
    assert isinstance(jti_str, str), "minted token must carry a jti claim"
    jti = uuid.UUID(jti_str)

    config_block = render_mcp_config(
        agent_name=selected.name,
        public_url=public_url,
        jwt=token,
    )

    log.info(
        "agent_setup.connect_via_mcp.minted",
        agent_name=selected.name,
        jti=str(jti),
        # Never log the token itself.
    )

    await interaction.response.send_message(
        content=config_block,
        view=_McpAccessView(
            jti=jti,
            runtime=runtime,
            allowed_user_id=allowed_user_id,
        ),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def render_mcp_config(*, agent_name: str, public_url: str, jwt: str) -> str:
    """Build the copyable MCP config message for a per-agent MCP token.

    Returned as plain message ``content`` (NOT a Components V2 TextDisplay) so
    the code blocks are reliably selectable / long-press-copyable on every
    Discord client. Each artifact lives in its own fenced block so one tap
    copies exactly that block — the CLI one-liner first (most users just run
    it), then the ``.mcp.json`` snippet.

    The key name is ``daimon-<agent-name>`` so multiple agents are namespaced in
    the same config file without collision.
    """
    key_name = f"daimon-{agent_name}"
    config = {
        key_name: {
            "url": public_url,
            "headers": {"Authorization": f"Bearer {jwt}"},
        }
    }
    mcp_json_block = json.dumps(config, indent=2)
    cli_oneliner = (
        f'claude mcp add --transport http "{key_name}" '
        f'"{public_url}" '
        f'--header "Authorization: Bearer {jwt}"'
    )
    return (
        f"**Connect a coding agent to `{agent_name}`** — token shown once, copy it now.\n"
        "**Run this:**\n"
        f"```\n{cli_oneliner}\n```\n"
        "**Or paste into `.mcp.json`:**\n"
        f"```json\n{mcp_json_block}\n```"
    )


class _McpAccessView(discord.ui.View):
    """Ephemeral classic View carrying just the Revoke button.

    The config itself is sent as plain message ``content`` (see
    ``render_mcp_config``) so the token is reliably copyable — a classic View
    (unlike a Components V2 LayoutView) composes with ``content=``.

    `interaction_check` gates the Revoke button to the original invoker only
    (same pattern as EditView.interaction_check — the allowed_user_id passed
    in from the handler).
    """

    def __init__(
        self,
        *,
        jti: uuid.UUID,
        runtime: DiscordRuntime,
        allowed_user_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self._jti = jti
        self._runtime = runtime
        self._allowed_user_id = allowed_user_id

        revoke_btn: discord.ui.Button[_McpAccessView] = discord.ui.Button(
            label="Revoke",
            style=discord.ButtonStyle.danger,
        )
        revoke_btn.callback = self._on_revoke  # type: ignore[method-assign]
        self.add_item(revoke_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self._allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.", ephemeral=True
            )
            return False
        return True

    async def _on_revoke(self, interaction: discord.Interaction) -> None:
        log.info("agent_setup.mcp_access.revoke.click", jti=str(self._jti))
        async with self._runtime.sessionmaker.begin() as session:
            row = await revoke_mcp_token(
                session,
                jti=self._jti,
                now=dt.datetime.now(dt.UTC),
            )
        if row is None:
            log.info("agent_setup.mcp_access.revoke.already_revoked", jti=str(self._jti))
            await interaction.response.send_message(
                "Token was already revoked (or not found).", ephemeral=True
            )
            return
        log.info("agent_setup.mcp_access.revoke.done", jti=str(self._jti))
        await interaction.response.edit_message(
            content="Token revoked. Agents using this token will get 401 on next request.",
            view=None,
        )

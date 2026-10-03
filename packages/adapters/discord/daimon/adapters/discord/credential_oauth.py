"""The `mcp_oauth` card's click: spend the request, mint the flow, hand over the link.

No modal, unlike every other kind: the value is a browser sign-in, not a
pasted secret. The requester alone reaches this (the button's
`interaction_check` already matched them), the request row is spent
atomically so a second click gets "already used", and the start link is
sent as an ephemeral only they see. Everything after the link — discovery,
sign-in, storing the grant, attaching the server, editing the card — happens
on the mcp process, which is why nothing here edits the card.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from daimon.adapters.discord.credential_origin import resolve_credential_target
from daimon.adapters.discord.posted_controls import edit_posted_card
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_pins import FormPinRefused
from daimon.core.credential_submit import begin_oauth_submit
from daimon.core.mcp_oauth import INVITE_BUTTON_LABEL, invite_copy, start_url
from daimon.core.posted_controls import NO_LONGER_VALID_MESSAGE
from daimon.core.stores.domain import CredentialRequestRow

import discord

_log = structlog.get_logger()

_UNCONFIGURED = (
    "This deployment cannot sign you in yet. Ask the operator to finish the daimon-mcp "
    "setup, then ask again. Nothing was saved."
)


def build_invite_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button[discord.ui.View](label=INVITE_BUTTON_LABEL, url=url))
    return view


async def start_mcp_oauth_from_click(
    interaction: discord.Interaction,
    *,
    runtime: DiscordRuntime,
    row: CredentialRequestRow,
) -> None:
    """Answer the click with the requester's private sign-in link."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    mcp = runtime.settings.mcp
    app_root_url = mcp.app_root_url
    # A superset of the route mount's predicate (mount needs fernet + app root;
    # this also needs jwt_secret): never hand out a link the routes cannot serve.
    if app_root_url is None or mcp.jwt_secret is None or runtime.turn_deps.fernet is None:
        await interaction.followup.send(_UNCONFIGURED, ephemeral=True)
        return
    agent = await resolve_credential_target(interaction, runtime=runtime, row=row)
    if agent is None:
        return
    now = datetime.now(UTC)
    try:
        consumed, flow = await begin_oauth_submit(
            runtime.sessionmaker, row=row, agent=agent, app_root_url=app_root_url, now=now
        )
    except FormPinRefused as refused:
        await interaction.followup.send(refused.refusal, ephemeral=True)
        return
    if consumed is None or flow is None:
        await interaction.followup.send(NO_LONGER_VALID_MESSAGE, ephemeral=True)
        return
    # The row is spent: the card must stop offering a button that can only
    # answer "already used" from here on, the same moment every other kind
    # flips to received.
    await edit_posted_card(interaction.client, row=consumed, state="received")
    _log.info(
        "credential_button.mcp_oauth.started",
        mcp_server_url=consumed.mcp_server_url,
        agent_id=str(consumed.agent_id),
    )
    await interaction.followup.send(
        invite_copy(server_name=consumed.target, agent_name=consumed.target_name or "the agent"),
        view=build_invite_view(start_url(app_root_url, state=flow.state)),
        ephemeral=True,
    )

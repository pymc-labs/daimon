"""`share_secret_with`: send one of the agent's keys to a person's DMs, revealed once.

The value goes from `agent_files` into its own encrypted share and never into the tool
result. The bot process handles the Reveal button (`SecretShareRevealButton`).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._admin import current_admin
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.setup_target import refuse_helper, require_turn_origin
from daimon.core import secret_shares as words
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import secret_shares as store
from daimon.core.stores.agent_files import get_agent_file
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field


class ShareSecretResult(BaseModel):
    recipient_id: str
    label: str
    expires_at: datetime
    note: str = "Sent to their DMs. Never repeat or ask for the value in chat."


def share_card(text: str, share_id: str) -> tuple[discord.Embed, discord.ui.View]:
    """The DM card: the core's words and one Reveal button the bot process dispatches."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Reveal",
            style=discord.ButtonStyle.primary,
            custom_id=words.build_custom_id(share_id),
        )
    )
    return discord.Embed(color=words.CARD_COLOR, description=text), view


async def share_secret_with_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    label: str,
    key_name: str,
    recipient_id: str | None,
) -> ShareSecretResult:
    """Fail closed at each step; the share is stored only with its card posted."""
    if auth.platform != "discord" or auth.platform_user_id is None:
        raise ToolError("Sharing a secret works in Discord only.")
    origin = refuse_helper(await require_turn_origin(runtime, auth, origin_context_id))
    if origin.sender_foreign:
        raise ToolError("Only a member of this server can ask for a key. Do not retry.")
    asker = auth.platform_user_id
    recipient = recipient_id or asker
    if re.fullmatch(r"[0-9]{1,20}", recipient) is None:
        raise ToolError("recipient_id must be one Discord user ID, not a mention.")
    cleaned = words.clean_label(label)
    if cleaned is None:
        raise ToolError(words.BAD_LABEL)
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id)
    async with runtime.session_factory() as session:
        place = FeaturePlace(auth.tenant_id, "discord")
        if not (await feature_in(session, runtime.settings, place, "secret_sharing")).on:
            raise ToolError(words.OFF)
        # An agent-scoped key never stands in for an admin (as `request_agent_key`).
        if recipient != asker and (
            auth.agent_id is not None or not await current_admin(session, auth)
        ):
            raise ToolError("Only an admin can send a key to someone else. It can go to you.")
        row = await get_agent_file(
            session, tenant_id=auth.tenant_id, agent_id=agent_id, key=key_name
        )
    if row is None:
        raise ToolError(f"This agent has no key named {key_name}.")
    if not words.value_ok(row.content):
        raise ToolError(words.BAD_VALUE)
    async with rest_client(_require_bot_token(runtime)) as client:
        guild = await client.fetch_guild(int(_require_guild_id(auth)))
        try:
            member = await guild.fetch_member(int(recipient))
        except discord.NotFound:
            raise ToolError(words.NOT_A_PERSON) from None
        if member.bot:
            raise ToolError(words.NOT_A_PERSON)
        now = datetime.now(UTC)
        async with runtime.session_factory.begin() as session:
            share = await store.create_share(
                session,
                tenant_id=auth.tenant_id,
                platform="discord",
                sender=asker,
                recipient=recipient,
                label=cleaned,
                value=row.content,
                now=now,
            )
            if share is None:
                raise ToolError(words.TOO_MANY)
            text = words.card_text(
                sender=asker, recipient=recipient, label=cleaned, expires_at=share.expires_at
            )
            embed, view = share_card(text, str(share.id))
            try:
                dm = await member.create_dm()
                sent = await dm.send(embed=embed, view=view)
            except discord.HTTPException:
                raise ToolError(words.dm_closed_text(recipient)) from None
            await store.set_share_card(
                session, share.id, channel_id=str(dm.id), message_id=str(sent.id)
            )
    return ShareSecretResult(recipient_id=recipient, label=cleaned, expires_at=share.expires_at)


def register_secret_share_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def share_secret_with(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        key_name: Annotated[str, Field(description="The agent's key, e.g. TOGGL_API_TOKEN.")],
        label: Annotated[str, Field(description="What the person sees, up to 100 characters.")],
        recipient_id: Annotated[
            str | None,
            Field(description="Discord user ID; omit to send it to the person who asked."),
        ] = None,
    ) -> ShareSecretResult:
        """Send one of this agent's saved keys to a person's Discord DMs, where they can
        reveal it once within a day. Use it when someone asks for a key's value; never
        paste a value in chat. It goes to the person who asked unless an admin names
        someone else. The result never holds the value."""
        return await share_secret_with_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            label=label,
            key_name=key_name,
            recipient_id=recipient_id,
        )

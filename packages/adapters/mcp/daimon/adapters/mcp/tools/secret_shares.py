"""`share_secret_with`: send one of the agent's keys to a person's DMs, revealed once.

The value goes from `agent_files` into its own encrypted share and never into the tool
result. The bot process handles the Reveal and Send buttons (`commands/share_secret.py`).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Final

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._admin import current_admin
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._owner import caller_owns_agent, may_own
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.setup_target import refuse_helper, require_turn_origin
from daimon.core import secret_share_flow as flow
from daimon.core import secret_shares as words
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import secret_shares as store
from daimon.core.stores.agent_files import get_agent_file
from daimon.core.stores.secret_shares import ShareView
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field


class ShareSecretResult(BaseModel):
    recipient_id: str
    label: str
    expires_at: datetime
    note: str = "Sent to their DMs. Never repeat or ask for the value in chat."


_CONFIRM_NOTE: Final = (
    "A Send button is in the asker's DMs; it goes to the recipient only when they click it. "
    "Never repeat or ask for the value in chat."
)
_WHO_MAY: Final = (
    "Only an admin, this agent's owner or whoever saved the key can get it. "
    "Ask an admin or an owner."
)


def share_card(text: str, custom_id: str, label: str) -> tuple[discord.Embed, discord.ui.View]:
    """A DM card: the core's words and one button the bot process dispatches."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=custom_id)
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
    """Fail closed at each step; the share is kept only with its card posted. A key goes to
    someone other than the asker only after the asker clicks Send on a card in their DMs."""
    if auth.platform != "discord" or auth.platform_user_id is None or not may_own(auth):
        raise ToolError("Sharing a secret doesn't work here.")
    origin = refuse_helper(await require_turn_origin(runtime, auth, origin_context_id))
    if origin.sender_foreign:
        raise ToolError("Only a member of this server can ask for a key. Do not retry.")
    if not origin.attended or origin.human_invocation is not True:
        raise ToolError("Only a person in this conversation can ask for a key. Do not retry.")
    asker = auth.platform_user_id
    recipient = recipient_id or asker
    if re.fullmatch(r"[0-9]{1,20}", recipient) is None:
        raise ToolError("recipient_id must be one user ID, not a mention.")
    cleaned = words.clean_label(label)
    if cleaned is None:
        raise ToolError(words.BAD_LABEL)
    ma_agent_id = origin.responder_ma_agent_id
    agent_id = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=ma_agent_id)
    async with runtime.session_factory() as session:
        # A held card goes to the asker's DMs first, so the asker's policy applies too.
        for dm_person in dict.fromkeys((recipient, asker)):
            refusal = await flow.sharing_refusal(
                session,
                runtime.settings,
                tenant_id=auth.tenant_id,
                recipient=dm_person,
                agent_id=ma_agent_id,
            )
            if refusal is not None:
                raise ToolError(refusal)
        admin = await current_admin(session, auth)
        row = await get_agent_file(
            session, tenant_id=auth.tenant_id, agent_id=agent_id, key=key_name
        )
    if row is None:
        raise ToolError(f"This agent has no key named {key_name}.")
    if recipient != asker and not admin:
        raise ToolError("Only an admin can send a key to someone else. It can go to you.")
    if not (
        admin
        or row.last_set_by_account_id == auth.account_id
        or await caller_owns_agent(runtime, auth, ma_agent_id)
    ):
        raise ToolError(_WHO_MAY)
    if not words.value_ok(row.content):
        raise ToolError(words.BAD_VALUE)
    held = recipient != asker
    async with rest_client(_require_bot_token(runtime)) as client:
        guild = await client.fetch_guild(int(_require_guild_id(auth)))
        try:
            member = await guild.fetch_member(int(recipient))
            card_to = await guild.fetch_member(int(asker)) if held else member
        except discord.NotFound:
            raise ToolError(words.NOT_A_PERSON) from None
        if member.bot:
            raise ToolError(words.NOT_A_PERSON)

        async def send(share: ShareView) -> tuple[str, str]:
            tenant, share_id = str(auth.tenant_id), str(share.id)
            if held:
                text = words.confirm_text(
                    recipient=recipient,
                    label=cleaned,
                    key_name=key_name,
                    expires_at=share.expires_at,
                )
                card = share_card(
                    text, words.build_custom_id(tenant, share_id, confirm=True), "Send"
                )
            else:
                text = words.card_text(
                    sender=asker, recipient=recipient, label=cleaned, expires_at=share.expires_at
                )
                card = share_card(text, words.build_custom_id(tenant, share_id), "Reveal")
            dm = await card_to.create_dm()
            sent = await dm.send(embed=card[0], view=card[1])
            return str(dm.id), str(sent.id)

        try:
            share = await flow.offer_share(
                runtime.session_factory,
                send,
                tenant_id=auth.tenant_id,
                sender=asker,
                recipient=recipient,
                label=cleaned,
                value=row.content,
                now=datetime.now(UTC),
                agent_id=ma_agent_id,
                key_name=key_name,
                awaiting_sender=held,
            )
        except discord.HTTPException:
            raise ToolError(words.dm_closed_text(asker if held else recipient)) from None
        except store.SecretShareEncryptionRequiredError:
            raise ToolError(words.NO_KEY) from None
    if share is None:
        raise ToolError(words.TOO_MANY)
    result = ShareSecretResult(recipient_id=recipient, label=cleaned, expires_at=share.expires_at)
    return result.model_copy(update={"note": _CONFIRM_NOTE}) if held else result


def register_secret_share_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def share_secret_with(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        key_name: Annotated[str, Field(description="The agent's key, e.g. TOGGL_API_TOKEN.")],
        label: Annotated[str, Field(description="What the person sees, up to 100 characters.")],
        recipient_id: Annotated[
            str | None,
            Field(description="Their user ID; omit to send it to the person who asked."),
        ] = None,
    ) -> ShareSecretResult:
        """Send one of this agent's saved keys to a person's Discord DMs, where they can
        reveal it once within a day. Use it when someone asks for a key's value; never
        paste a value in chat. Only an admin, the agent's owner or whoever saved the key
        can get it, and only while they are in the conversation. It goes to them; when an
        admin names someone else, it goes once they click Send. The result never holds
        the value."""
        return await share_secret_with_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            label=label,
            key_name=key_name,
            recipient_id=recipient_id,
        )

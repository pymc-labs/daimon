"""Actions on the private GitHub request cards sent to Discord DMs."""

from __future__ import annotations

import uuid

from daimon.adapters.discord.agent_setup.github_card_ui import github_embed
from daimon.adapters.discord.checks import channel_admin_caller, is_member_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_reach import load_target_facts
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_panel import connect_link, safe_github_error, sync_connect_admin
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.stores.github_access_requests import (
    cancel_request,
    dismiss_delivery,
    get_delivery,
    lookup_request,
    set_status,
)
from daimon.core.stores.github_request_actions import (
    approve_connected_request,
    approve_connection_request,
)
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.tenants import get_tenant

import discord


async def update_requester_card(
    client: discord.Client,
    runtime: DiscordRuntime,
    request_id: uuid.UUID,
    *,
    text: str,
    can_cancel: bool,
    link_url: str | None = None,
    skip_account_id: uuid.UUID | None = None,
) -> None:
    """Keep the asker's existing private card in step with an admin decision."""
    async with runtime.sessionmaker() as session:
        request = await lookup_request(session, request_id=request_id)
        if request is None or request.requester_account_id == skip_account_id:
            return
        delivery = await get_delivery(
            session,
            tenant_id=request.tenant_id,
            request_id=request.id,
            recipient_account_id=request.requester_account_id,
        )
    if delivery is None or delivery.message_id is None:
        return
    try:
        user = await client.fetch_user(int(request.requester_platform_user_id))
        dm = await user.create_dm()
        view = discord.ui.View(timeout=None) if can_cancel or link_url else None
        if view is not None and link_url:
            from daimon.adapters.discord.agent_setup.github_home import connect_button

            view.add_item(connect_button(link_url))
        if view is not None and can_cancel:
            view.add_item(
                discord.ui.Button(
                    label="Cancel request",
                    custom_id=f"github_request:{request.id}:cancel",
                )
            )
        state = (
            "danger"
            if "declined" in text.casefold() or "cancelled" in text.casefold()
            else "waiting"
            if can_cancel
            else "success"
        )
        await dm.get_partial_message(int(delivery.message_id)).edit(
            content=None,
            embed=github_embed(text, state=state),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.HTTPException, ValueError):
        return


async def handle_request_card(interaction: discord.Interaction, runtime: DiscordRuntime) -> bool:
    data = interaction.data or {}
    custom_id = str(data.get("custom_id") or "")
    if not custom_id.startswith("github_request:"):
        return False
    parts = custom_id.split(":")
    if len(parts) != 3 or parts[2] not in {"approve", "connect", "decline", "hide", "cancel"}:
        await interaction.response.send_message("This request is unavailable.", ephemeral=True)
        return True
    try:
        request_id = uuid.UUID(parts[1])
    except ValueError:
        await interaction.response.send_message("This request is unavailable.", ephemeral=True)
        return True
    async with runtime.sessionmaker() as session:
        request = await lookup_request(session, request_id=request_id)
        tenant = await get_tenant(session, request.tenant_id) if request else None
        principal = (
            await find_platform_principal(
                session,
                tenant_id=request.tenant_id,
                platform="discord",
                external_id=str(interaction.user.id),
            )
            if request
            else None
        )
        delivery = (
            await get_delivery(
                session,
                tenant_id=request.tenant_id,
                request_id=request_id,
                recipient_account_id=principal.account_id,
            )
            if request and principal
            else None
        )
    if (
        request is None
        or tenant is None
        or principal is None
        or delivery is None
        or delivery.dismissed_at is not None
        or interaction.message is None
        or delivery.message_id != str(interaction.message.id)
    ):
        await interaction.response.send_message("This request is unavailable.", ephemeral=True)
        return True
    action = parts[2]
    if action == "cancel":
        async with runtime.sessionmaker.begin() as session:
            cancelled = await cancel_request(
                session,
                tenant_id=request.tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
        await interaction.response.edit_message(
            content=None,
            embed=github_embed(
                "Request cancelled." if cancelled else "This request is unavailable."
            ),
            view=None,
        )
        return True
    guild = interaction.client.get_guild(int(tenant.external_id))
    if guild is None:
        await interaction.response.send_message("This request is unavailable.", ephemeral=True)
        return True
    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.HTTPException:
            await interaction.response.send_message("This request is unavailable.", ephemeral=True)
            return True
    is_admin = is_member_guild_admin(member, guild_owner_id=guild.owner_id)
    if action == "hide":
        async with runtime.sessionmaker.begin() as session:
            hidden = await dismiss_delivery(
                session,
                tenant_id=request.tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
        await interaction.response.edit_message(
            content=None,
            embed=github_embed("Hidden for you." if hidden else "This request is unavailable."),
            view=None,
        )
        return True
    if not is_admin:
        await interaction.response.send_message(
            "Only a server admin can approve GitHub requests.", ephemeral=True
        )
        return True
    live_agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=request.tenant_id, agent_id=request.agent_id
    )
    if live_agent is None or live_agent.id != request.ma_agent_id:
        await interaction.response.send_message("This agent is unavailable.", ephemeral=True)
        return True
    managed = live_agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    async with runtime.sessionmaker() as session:
        facts = (
            TargetFacts(is_daimon_managed=managed, is_reachable_in_tenant=False)
            if is_admin
            else await load_target_facts(
                session,
                "github_grant",
                tenant_id=request.tenant_id,
                platform="discord",
                agent_names=(request.agent_name, live_agent.name),
                ma_agent_id=str(live_agent.id),
                default=runtime.deployment_default,
                caller=channel_admin_caller(member),
                is_daimon_managed=managed,
                caller_platform_user_id=str(member.id),
            )
        )
    can_manage = decide_operation("github_grant", is_admin=is_admin, target=facts) == "allow"
    if not can_manage or (action == "connect" and not is_admin):
        await interaction.response.send_message(
            "You cannot change this agent's GitHub repos.", ephemeral=True
        )
        return True
    if action == "decline":
        async with runtime.sessionmaker.begin() as session:
            declined = await set_status(
                session,
                tenant_id=request.tenant_id,
                request_id=request_id,
                expected=request.status,
                status="declined",
            )
        await interaction.response.edit_message(
            content=None,
            embed=github_embed(
                "Declined." if declined else "This request is unavailable.", state="danger"
            ),
            view=None,
        )
        if declined:
            await update_requester_card(
                interaction.client,
                runtime,
                request_id,
                text="An admin declined GitHub access for this request.",
                can_cancel=True,
                skip_account_id=principal.account_id,
            )
        return True
    if action == "approve":
        try:
            async with runtime.sessionmaker.begin() as session:
                approved = await approve_connected_request(
                    session,
                    tenant_id=request.tenant_id,
                    request_id=request_id,
                    account_id=principal.account_id,
                )
        except ValueError as error:
            await interaction.response.send_message(safe_github_error(error), ephemeral=True)
            return True
        await interaction.response.edit_message(
            content=None,
            embed=github_embed(
                f"✓ Added. {request.agent_name} is continuing."
                if approved
                else "This request is unavailable.",
                state="success" if approved else "info",
            ),
            view=None,
        )
        if approved:
            await update_requester_card(
                interaction.client,
                runtime,
                request_id,
                text=f"✓ Added. {request.agent_name} is continuing.",
                can_cancel=False,
                skip_account_id=principal.account_id,
            )
        return True
    try:
        async with runtime.sessionmaker.begin() as session:
            await sync_connect_admin(
                session,
                tenant_id=request.tenant_id,
                platform="discord",
                platform_user_id=str(member.id),
                verified_tenant_admin=is_member_guild_admin(member, guild_owner_id=guild.owner_id),
            )
            url = await connect_link(
                session,
                settings=runtime.settings,
                tenant_id=request.tenant_id,
                platform="discord",
                platform_user_id=str(member.id),
                verified_tenant_admin=is_member_guild_admin(member, guild_owner_id=guild.owner_id),
                workspace_label=guild.name,
                requester_label=member.display_name,
            )
            await approve_connection_request(
                session,
                tenant_id=request.tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
    except ValueError as error:
        await interaction.response.send_message(safe_github_error(error), ephemeral=True)
        return True
    from daimon.adapters.discord.agent_setup.github_home import connect_button_view

    view = connect_button_view(url)
    await interaction.response.edit_message(
        content=None,
        embed=github_embed("Waiting for GitHub confirmation.", state="waiting"),
        view=view,
    )
    await update_requester_card(
        interaction.client,
        runtime,
        request_id,
        text="Waiting for GitHub confirmation.",
        can_cancel=True,
        skip_account_id=principal.account_id,
    )
    return True

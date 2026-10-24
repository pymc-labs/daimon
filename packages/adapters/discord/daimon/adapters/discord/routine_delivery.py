"""Discord's `RoutinePoster`: post a routine's result to its destination.

Run by the bot's delivery poller (`daimon.core.routine_delivery`). The
destination is resolved here — a channel, or a thread, which on Discord is a
channel too — and must belong to the routine's own, live guild. A thread's
parent is fetched when Discord's cache does not hold it, because the parent's
category is what category protection is checked against; if it cannot be
resolved, nothing is posted there (fail closed). The tenant's access policy
then decides with the parent channel and category in hand.

The creator is checked first, before anything is resolved or sent: if the
policy cannot be read, or the creator may no longer invoke the agent, nothing
is sent anywhere. Only a cleared result may then fall back: when the
destination cannot be used — protected, gone, or outside the guild — it goes
to the creator by direct message instead, if the tenant's direct-message
policy allows them, so it never silently goes nowhere.
Mentions are disabled on every post: the text is the agent's, and a routine
must not ping anyone on its own.

With agent identity on, the destination post goes through
`DiscordPostTransport`, so it carries the agent's name and face (or, where
webhooks are unavailable, the bot with the agent's name label) and drops the
"from <agent>" wording. The fallback direct message stays Daimon's own notice.
Each destination post is recorded as the agent's (`source='routine'`, the
creator as requester), so a reply to it starts a turn like a reply to an
answer, and only the creator or an admin can have the agent tidy it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.discord.membership import GatewayMembershipResolver
from daimon.adapters.discord.output_delivery import upload_limit, upload_output_file
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.adapters.discord.recruitment_guard import protected
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_identity import AgentIdentity
from daimon.core.agent_post_identity import fallback_name_prefix
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.channel_tidy import record_turn_post
from daimon.core.config import DirectMessagePolicy, RecruitmentSettings
from daimon.core.membership import MembershipResolver
from daimon.core.message_split import split_fenced
from daimon.core.output_delivery import DeliverableFile
from daimon.core.permissions import any_agent_rules, any_own_readers
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    claim_still_current,
    clear_creator,
    delivery_target,
    discord_creator_may_post,
    render_fallback_dm,
    render_fallback_post,
    tenant_dm_still_open,
)
from daimon.core.routine_files import decode_payload, deliver_routine_files, payload_for
from daimon.core.routine_hold import result_is_held
from daimon.core.rule_views import keeps_routine_inside
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import get_routine
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

__all__ = ["make_discord_routine_poster"]

log = structlog.get_logger(__name__)

_DISCORD_MAX_CHARS = 2000

ChannelFetcher = Callable[[int], Awaitable[object]]
#: Open a DM with a member of a guild: `(guild_id, user_id)` → something with
#: `send`. Raises `discord.HTTPException` (or `LookupError`) when it cannot.
DmOpener = Callable[[int, int], Awaitable[discord.abc.Messageable]]
DmPolicyLookup = Callable[[RoutineRow], DirectMessagePolicy]
#: The identity a routine's result posts under: `(row, guild_id)` → identity.
IdentityResolver = Callable[[RoutineRow, str], Awaitable[AgentIdentity]]
AgentNamesResolver = Callable[[RoutineRow], Awaitable[tuple[str | None, ...]]]

_UNRESOLVED = object()


async def _placement(
    channel: object, fetch_channel: ChannelFetcher
) -> tuple[str | None, str | None, int | None, object] | None:
    """(parent_channel_id, category_id, guild_id, permission_source) of a
    postable channel. `permission_source` is the channel whose permission
    overwrites apply: the channel itself, or a thread's parent.

    `None` when the channel is not a text channel or thread, or when a
    thread's parent cannot be resolved (so its category is unknown).
    """
    if isinstance(channel, discord.Thread):
        parent: object = channel.parent
        if parent is None and channel.parent_id:
            try:
                parent = await fetch_channel(channel.parent_id)
            except discord.HTTPException:
                parent = _UNRESOLVED
        if parent is _UNRESOLVED or parent is None:
            return None
        category = getattr(parent, "category_id", None)
        return (
            str(channel.parent_id) if channel.parent_id else None,
            str(category) if category else None,
            channel.guild.id,
            parent,
        )
    if isinstance(channel, discord.TextChannel):
        return (
            None,
            str(channel.category_id) if channel.category_id else None,
            channel.guild.id,
            channel,
        )
    return None


async def creator_may_post(channel: object, permission_source: object, user_id: int) -> bool:
    """Whether the routine's creator, as a member of the channel's guild, may
    post in `channel` — the rule `send_message` applies to a caller.

    Permissions come from `permission_source` (a thread's parent), so an
    uncached parent never breaks the check. A creator who has left the guild,
    or whose membership cannot be read, may not post.
    """
    if not isinstance(channel, discord.Thread | discord.TextChannel) or not isinstance(
        permission_source, discord.TextChannel | discord.ForumChannel
    ):
        return False
    guild = channel.guild
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.HTTPException:
        return False
    perms = permission_source.permissions_for(member)
    is_thread = isinstance(channel, discord.Thread)
    is_private_thread = is_thread and channel.type is discord.ChannelType.private_thread
    is_thread_member = False
    if is_private_thread and not (member.guild_permissions.administrator or perms.manage_threads):
        assert isinstance(channel, discord.Thread)
        try:
            await channel.fetch_member(user_id)
            is_thread_member = True
        except discord.HTTPException:
            is_thread_member = False
    return discord_creator_may_post(
        administrator=member.guild_permissions.administrator,
        view_channel=perms.view_channel,
        send_messages=perms.send_messages,
        is_thread=is_thread,
        send_messages_in_threads=perms.send_messages_in_threads,
        is_private_thread=is_private_thread,
        manage_threads=perms.manage_threads,
        is_thread_member=is_thread_member,
    )


def make_discord_routine_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fetch_channel: ChannelFetcher,
    open_dm: DmOpener,
    dm_policy: DmPolicyLookup,
    recruitment: RecruitmentSettings,
    client: discord.Client | None = None,
    output_client: AsyncAnthropic | None = None,
    resolve_identity: IdentityResolver | None = None,
    membership_resolver: MembershipResolver | None = None,
    resolve_agent_names: AgentNamesResolver | None = None,
) -> Callable[[RoutineRow], Awaitable[DeliveryOutcome]]:
    """A poster bound to the bot's channel lookup (cache first, then REST).

    Without `client` and `resolve_identity` every result posts as the bot.
    """

    membership_resolver = membership_resolver or (
        GatewayMembershipResolver(client) if client is not None else None
    )

    def refusal(row: RoutineRow, reason: str) -> DeliveryOutcome:
        private_reasons = {
            "invoker_not_allowed": "The owner is no longer a verified, allowed server member.",
            "access_policy_unreadable": "The server access settings could not be read.",
            "routine_dms_disabled": "Routine DMs are disabled for this server.",
            "restricted_source": "A restricted source cannot deliver by DM.",
            "agent_not_allowed": "The agent is not allowed to run privately.",
            "destination_unavailable": "The routine server or recipient is unavailable.",
        }
        return DeliveryOutcome(
            status="skipped",
            note=private_reasons.get(reason, reason) if row.destination_kind == "dm" else reason,
        )

    async def creator_still_cleared(row: RoutineRow) -> DeliveryOutcome | None:
        async with sessionmaker() as session:
            current = await get_routine(session, row.id, tenant_id=row.tenant_id)
            if current is None or (current.destination_kind, current.destination_id) != (
                row.destination_kind,
                row.destination_id,
            ):
                return DeliveryOutcome(status="skipped", note="destination_changed")
            if row.destination_kind == "dm" and (
                current.agent_id != row.agent_id
                or current.agent_name != row.agent_name
                or current.updated_at != row.updated_at
            ):
                return DeliveryOutcome(status="skipped", note="private_revision_changed")
            if not claim_still_current(current, row, datetime.now(UTC)):
                return DeliveryOutcome(
                    status="skipped", note="Delivery was interrupted; it will not be retried."
                )
            if row.destination_kind == "dm":
                if (current.destination_kind, current.destination_id) != (
                    "dm",
                    row.created_by_user_id,
                ):
                    return DeliveryOutcome(status="skipped", note="destination_changed")
                if not await dm_enabled(session, tenant_id=row.tenant_id):
                    return DeliveryOutcome(
                        status="skipped", note="Routine DMs are disabled for this server."
                    )
        if row.destination_kind == "dm" and not dm_policy(row).allows(row.created_by_user_id or ""):
            return DeliveryOutcome(
                status="skipped", note="The owner is not allowed to receive DMs."
            )
        cleared = await clear_creator(
            sessionmaker, row, platform="discord", membership_resolver=membership_resolver
        )
        if not isinstance(cleared, TenantAccessPolicy):
            return refusal(row, cleared)
        if row.destination_kind == "dm" and (any_agent_rules(cleared) or any_own_readers(cleared)):
            if resolve_agent_names is None:
                return DeliveryOutcome(
                    status="skipped", note="The routine agent could not be verified."
                )
            names = await resolve_agent_names(row)
            if not names or not authorize(
                cleared,
                subject=Subject(),
                action=Action.RUN_AGENT,
                surface=Surface.ROUTINE,
                agent=AgentRef.of(*names),
                place=Place(),
            ):
                return DeliveryOutcome(
                    status="skipped", note="The agent is not allowed to deliver privately."
                )
        if row.destination_kind == "dm" and keeps_routine_inside(cleared, row):
            return DeliveryOutcome(
                status="skipped", note="A restricted source cannot deliver by DM."
            )
        return None

    async def attach(
        row: RoutineRow,
        destination: discord.abc.Messageable,
        fallback_guard: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        async def may_post() -> bool:
            if fallback_guard is not None and not await fallback_guard():
                return False
            if await creator_still_cleared(row) is not None:
                return False
            if isinstance(destination, discord.TextChannel | discord.Thread):
                placement = await _placement(destination, fetch_channel)
                if placement is None or row.created_by_user_id is None:
                    return False
                parent, category, _guild, permissions = placement
                policy = await clear_creator(
                    sessionmaker, row, platform="discord", membership_resolver=membership_resolver
                )
                return bool(
                    isinstance(policy, TenantAccessPolicy)
                    and authorize(
                        policy,
                        subject=Subject(),
                        action=Action.POST,
                        surface=Surface.ROUTINE,
                        place=Place(
                            channel_id=str(destination.id),
                            parent_channel_id=parent,
                            category_id=category,
                        ),
                    )
                    and await creator_may_post(
                        destination, permissions, int(row.created_by_user_id)
                    )
                )
            return True

        async def notice(text: str) -> None:
            await destination.send(content=text, allowed_mentions=discord.AllowedMentions.none())

        async def upload(file: DeliverableFile) -> None:
            await upload_output_file(destination, file)

        if output_client is None and decode_payload(payload_for(row)).files:
            raise RuntimeError("Generated file delivery is unavailable; it will not be retried.")
        if output_client is not None:
            await deliver_routine_files(
                output_client,
                payload_for(row),
                max_bytes=upload_limit(destination),
                post=upload,
                notice=notice,
                may_post=may_post,
            )

    async def _dm_fallback(
        row: RoutineRow,
        reason: str,
        guild_id: str,
        may_post: Callable[[], Awaitable[bool]],
    ) -> DeliveryOutcome:
        async with sessionmaker() as session:
            if not await dm_enabled(session, tenant_id=row.tenant_id):
                return DeliveryOutcome(
                    status="skipped", note="Routine DMs are disabled for this server."
                )
        creator = row.created_by_user_id
        if (
            creator is None
            or not creator.isdigit()
            or not guild_id.isdigit()
            or not dm_policy(row).allows(creator)
        ):
            return DeliveryOutcome(status="skipped", note=reason)
        try:
            if not await may_post():
                return DeliveryOutcome(status="skipped", note="Fallback DM access changed.")
            dm = await open_dm(int(guild_id), int(creator))
            for chunk in split_fenced(
                render_fallback_dm(row, reason, platform="discord"),
                _DISCORD_MAX_CHARS,
                word_boundary=True,
            ):
                async with sessionmaker() as session:
                    if not await dm_enabled(session, tenant_id=row.tenant_id):
                        return DeliveryOutcome(
                            status="skipped", note="Routine DMs are disabled for this server."
                        )
                if (refused := await creator_still_cleared(row)) is not None:
                    return refused
                if not await may_post():
                    return DeliveryOutcome(status="skipped", note="Fallback DM access changed.")
                await dm.send(content=chunk, allowed_mentions=discord.AllowedMentions.none())
            await attach(row, dm, fallback_guard=may_post)
        except (discord.HTTPException, LookupError) as err:
            log.info(
                "routine.delivery_dm_failed", routine_id=str(row.id), error_type=type(err).__name__
            )
            return DeliveryOutcome(status="skipped", note=reason)
        return DeliveryOutcome(status="delivered", note=f"dm_fallback:{reason}")

    async def _post(row: RoutineRow) -> DeliveryOutcome:
        async with sessionmaker() as session:
            tenant = await get_tenant(session, row.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        if recruitment.is_recruitment_channel(
            tenant.external_id,
            row.source_thread_id or row.source_channel_id or row.channel_id or "",
            row.source_channel_id or row.channel_id,
        ):
            return DeliveryOutcome(status="skipped", note="Source unavailable.")
        # Resolve destination privacy before any possible fallback or result access.
        target = delivery_target(row, platform="discord")
        if row.destination_kind != "dm" and target is not None and recruitment.tenants:
            try:
                resolved = await fetch_channel(int(target.channel_id))
            except (discord.HTTPException, LookupError, ValueError):
                if recruitment.is_recruitment_channel(tenant.external_id, target.channel_id, None):
                    return DeliveryOutcome(status="skipped", note="Source unavailable.")
            else:
                if protected(recruitment, resolved, tenant.external_id):
                    return DeliveryOutcome(status="skipped", note="Source unavailable.")
        # Creator first, before resolving or sending anything: a creator who
        # may no longer invoke the agent, or an unreadable policy, gets
        # nothing — no destination post and no direct message.
        cleared = await clear_creator(
            sessionmaker, row, platform="discord", membership_resolver=membership_resolver
        )
        if not isinstance(cleared, TenantAccessPolicy):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason=cleared)
            return refusal(row, cleared)
        policy = cleared
        # An isolated or shared channel's result never leaves it, not even by DM.
        held = await result_is_held(sessionmaker, row, platform="discord")
        keep_inside = keeps_routine_inside(policy, row) or held
        parent_channel_id: str | None = None

        async def fallback_guard() -> bool:
            if await result_is_held(sessionmaker, row, platform="discord"):
                return False
            if await creator_still_cleared(row) is not None:
                return False
            if not await tenant_dm_still_open(sessionmaker, row, tenant.external_id):
                return False
            creator = row.created_by_user_id
            if creator is None or not dm_policy(row).allows(creator):
                return False
            current_policy = await clear_creator(
                sessionmaker, row, platform="discord", membership_resolver=membership_resolver
            )
            return isinstance(current_policy, TenantAccessPolicy) and not keeps_routine_inside(
                current_policy, row, parent_channel_id=parent_channel_id
            )

        async def fallback(reason: str) -> DeliveryOutcome:
            if keep_inside:
                return DeliveryOutcome(status="skipped", note=reason)
            return await _dm_fallback(row, reason, tenant.external_id, fallback_guard)

        if row.destination_kind == "dm":
            if row.destination_id is None or row.destination_id != row.created_by_user_id:
                return DeliveryOutcome(status="skipped", note="DM recipient must be the owner.")
            if (refused := await creator_still_cleared(row)) is not None:
                return refused
            try:
                dm = await open_dm(int(tenant.external_id), int(row.destination_id))
                text = (
                    f"Routine {row.trigger_message[:60]}\n"
                    f"Run {row.id} / {row.last_fired_at}\n\n" + render_fallback_post(row)
                )
                for chunk in split_fenced(text, _DISCORD_MAX_CHARS, word_boundary=True):
                    if (refused := await creator_still_cleared(row)) is not None:
                        return refused
                    await dm.send(content=chunk, allowed_mentions=discord.AllowedMentions.none())
                await attach(row, dm)
            except (discord.HTTPException, LookupError, ValueError, TypeError) as err:
                log.info(
                    "routine.delivery_dm_failed",
                    routine_id=str(row.id),
                    error_type=type(err).__name__,
                )
                return DeliveryOutcome(
                    status="skipped", note="The DM could not be delivered; it will not be retried."
                )
            return DeliveryOutcome(status="delivered")

        target = delivery_target(row, platform="discord")
        if target is None or not target.channel_id.isdigit():
            return await fallback("destination_unavailable")
        try:
            channel = await fetch_channel(int(target.channel_id))
        except discord.HTTPException:
            return await fallback("destination_unavailable")
        if protected(recruitment, channel, tenant.external_id):
            return DeliveryOutcome(status="skipped", note="Source unavailable.")
        placement = await _placement(channel, fetch_channel)
        if placement is None or str(placement[2]) != tenant.external_id:
            # Not a text channel or thread, its parent is unknown, or it is in
            # another guild.
            return await fallback("destination_unavailable")
        parent_channel_id, category_id, _guild_id, permission_source = placement
        # The parent is known now, also for a thread saved without it.
        held = held or await result_is_held(
            sessionmaker, row, platform="discord", parent_channel_id=parent_channel_id
        )
        keep_inside = keeps_routine_inside(policy, row, parent_channel_id=parent_channel_id) or held
        creator = row.created_by_user_id
        if (
            creator is None
            or not creator.isdigit()
            or not await creator_may_post(channel, permission_source, int(creator))
        ):
            # A routine posts on its creator's behalf: never somewhere they
            # could not post themselves. Their own DM is still fine.
            log.info(
                "routine.delivery_refused", routine_id=str(row.id), reason="creator_cannot_post"
            )
            return await fallback("creator_cannot_post")
        if not authorize(
            policy,
            subject=Subject(),
            action=Action.POST,
            surface=Surface.ROUTINE,
            place=Place(
                channel_id=target.channel_id,
                parent_channel_id=parent_channel_id,
                category_id=category_id,
            ),
        ):
            log.info("routine.delivery_refused", routine_id=str(row.id), reason="protected_channel")
            return await fallback("protected_channel")
        assert isinstance(channel, discord.Thread | discord.TextChannel)

        async def record(sent: discord.Message) -> None:
            # A reply to the result starts a turn only for a recorded post (a
            # webhook post cannot be pinged); the creator is who asked for it.
            await record_turn_post(
                sessionmaker,
                tenant_id=row.tenant_id,
                platform="discord",
                ma_agent_id=row.agent_id,
                channel_id=str(channel.id),
                message_id=str(sent.id),
                requester_platform_user_id=creator,
                source="routine",
                parent_channel_id=parent_channel_id,
            )

        identity = (
            await resolve_identity(row, tenant.external_id)
            if client is not None and resolve_identity is not None
            else None
        )
        if client is None or identity is None or identity.builtin:
            for chunk in split_fenced(
                render_fallback_post(row, platform="discord"),
                _DISCORD_MAX_CHARS,
                word_boundary=True,
            ):
                if (refused := await creator_still_cleared(row)) is not None:
                    return refused
                sent = await channel.send(
                    content=chunk, allowed_mentions=discord.AllowedMentions.none()
                )
                await record(sent)
            await attach(row, channel)
            return DeliveryOutcome(status="delivered")
        # A bot fallback puts the name label on top, so leave room for it.
        limit = _DISCORD_MAX_CHARS - len(fallback_name_prefix(identity.name, ""))
        transport = DiscordPostTransport(
            client,
            channel,
            name=identity.name,
            avatar_url=identity.avatar_url,
            builtin=False,
            identity_enabled=True,
        )
        for index, chunk in enumerate(
            split_fenced(
                render_fallback_post(row, as_agent=True, platform="discord"),
                limit,
                word_boundary=True,
            )
        ):
            if (refused := await creator_still_cleared(row)) is not None:
                return refused
            sent = await transport.send(
                content=chunk,
                allowed_mentions=discord.AllowedMentions.none(),
                _prefix_if_fallback=index == 0,
            )
            await record(sent)
        await attach(row, channel)
        return DeliveryOutcome(status="delivered")

    return _post
